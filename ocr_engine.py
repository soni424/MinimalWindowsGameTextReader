"""Native Windows OCR integration using the ``winocr`` package."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from typing import Callable, Final, Protocol

from PIL import Image


class OcrError(RuntimeError):
    """Raised when the Windows Media OCR API cannot process an image."""


class _OcrSession(Protocol):
    def recognise(self, image: Image.Image) -> object: ...

    def close(self) -> None: ...


class _WinOcrSession:
    """One Windows Media OCR engine and event loop retained by the OCR worker."""

    def __init__(self, language: str) -> None:
        try:
            import winocr
        except ImportError as exc:
            raise OcrError("winocr is not installed. Install the app requirements first.") from exc
        language_value = winocr.Language(language)
        if not winocr.OcrEngine.is_language_supported(language_value):
            raise OcrError(f"Windows OCR language {language!r} is not installed.")
        engine = winocr.OcrEngine.try_create_from_language(language_value)
        if engine is None:
            raise OcrError(f"Windows could not initialize OCR language {language!r}.")
        self._module = winocr
        self._engine = engine
        self._loop = asyncio.new_event_loop()

    def recognise(self, image: Image.Image) -> object:
        module = self._module
        prepared = image if image.mode == "RGBA" else image.convert("RGBA")
        writer = module.DataWriter()
        bitmap = None
        try:
            writer.write_bytes(prepared.tobytes())
            buffer = writer.detach_buffer()
            bitmap = module.SoftwareBitmap.create_copy_from_buffer(
                buffer,
                module.BitmapPixelFormat.RGBA8,
                prepared.width,
                prepared.height,
            )
            operation = self._engine.recognize_async(bitmap)
            # Keep the native result object: extracting its ``text`` property is
            # faster and smaller than recursively serializing every word/box.
            return self._loop.run_until_complete(module.to_coroutine(operation))
        finally:
            if bitmap is not None and hasattr(bitmap, "close"):
                try:
                    bitmap.close()
                except Exception:
                    pass
            if hasattr(writer, "close"):
                try:
                    writer.close()
                except Exception:
                    pass

    def close(self) -> None:
        if not self._loop.is_closed():
            self._loop.close()


class OcrEngine:
    """Recognise text from Pillow images through Windows' Media OCR API."""

    DEFAULT_LANGUAGE: Final[str] = "en"

    def __init__(
        self,
        language: str = DEFAULT_LANGUAGE,
        *,
        session_factory: Callable[[str], _OcrSession] | None = None,
    ) -> None:
        self.language = language
        self._session_factory = session_factory or _WinOcrSession
        self._session: _OcrSession | None = None

    def _get_session(self) -> _OcrSession:
        if self._session is None:
            self._session = self._session_factory(self.language)
        return self._session

    def warm_up(self) -> None:
        """Create the native OCR engine on its persistent worker before first use."""
        self._get_session()

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            finally:
                self._session = None

    @staticmethod
    def clean_text(text: object) -> str:
        """Normalise whitespace while retaining intentional OCR line boundaries."""
        if text is None:
            return ""
        lines = []
        for line in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            compact = re.sub(r"[\t \f\v]+", " ", line).strip()
            if compact:
                lines.append(compact)
        return "\n".join(lines)

    @classmethod
    def _result_text(cls, result: object) -> str:
        """Extract readable text from the result returned by ``winocr``.

        Recent ``winocr`` releases return a mapping containing the complete
        OCR layout (lines, words, and bounding rectangles), rather than a
        plain string.  Passing that mapping through ``str`` made the speech
        engine read its Python representation aloud.  Prefer the full-text
        value and fall back to the recognised lines when it is unavailable.
        """
        if isinstance(result, str):
            return result

        if isinstance(result, Mapping):
            text = result.get("text")
            if isinstance(text, str):
                return text
            lines = result.get("lines")
        else:
            text = getattr(result, "text", None)
            if isinstance(text, str):
                return text
            lines = getattr(result, "lines", None)

        if not isinstance(lines, (list, tuple)):
            return ""

        extracted_lines: list[str] = []
        for line in lines:
            if isinstance(line, Mapping):
                line_text = line.get("text")
                words = line.get("words")
            else:
                line_text = getattr(line, "text", None)
                words = getattr(line, "words", None)

            if isinstance(line_text, str):
                extracted_lines.append(line_text)
                continue

            if isinstance(words, (list, tuple)):
                word_text = [
                    word.get("text") if isinstance(word, Mapping) else getattr(word, "text", None)
                    for word in words
                ]
                extracted_lines.append(" ".join(word for word in word_text if isinstance(word, str)))

        return "\n".join(extracted_lines)

    def recognise(self, image: Image.Image) -> str:
        """Return Windows OCR text for a Pillow image.

        ``recognize_pil_sync`` is supplied by ``winocr`` and directly wraps the
        Windows Media OCR API.  It avoids third-party OCR models and their font
        interpretation differences.
        """
        if not isinstance(image, Image.Image):
            raise TypeError("OCR requires a Pillow Image instance.")
        if image.width < 1 or image.height < 1:
            return ""
        try:
            result = self._get_session().recognise(image)
        except OcrError:
            raise
        except Exception as exc:
            raise OcrError(f"Windows OCR failed: {exc}") from exc
        return self.clean_text(self._result_text(result))
