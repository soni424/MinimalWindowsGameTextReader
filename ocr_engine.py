"""Native Windows OCR integration using the ``winocr`` package."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from statistics import median
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
        lines: list[str] = []
        pending_blank = False
        for line in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            compact = re.sub(r"[\t \f\v]+", " ", line).strip()
            if compact:
                if pending_blank and lines:
                    lines.append("")
                lines.append(compact)
                pending_blank = False
            elif lines:
                pending_blank = True
        return "\n".join(lines)

    @staticmethod
    def _value(item: object, name: str) -> object | None:
        return item.get(name) if isinstance(item, Mapping) else getattr(item, name, None)

    @staticmethod
    def _items(value: object | None) -> list[object]:
        if value is None or isinstance(value, (str, bytes, bytearray, Mapping)):
            return []
        try:
            return list(value)  # type: ignore[arg-type]
        except TypeError:
            return []

    @classmethod
    def _rect(cls, value: object | None) -> tuple[float, float, float, float] | None:
        if value is None:
            return None
        try:
            x = float(cls._value(value, "x"))
            y = float(cls._value(value, "y"))
            width = float(cls._value(value, "width"))
            height = float(cls._value(value, "height"))
        except (TypeError, ValueError):
            return None
        if width <= 0 or height <= 0:
            return None
        return x, y, width, height

    @classmethod
    def _line_details(
        cls, line: object
    ) -> tuple[str, tuple[float, float, float, float] | None]:
        line_text = cls._value(line, "text")
        words = cls._value(line, "words")
        word_items = cls._items(words)
        if not isinstance(line_text, str):
            word_text = [cls._value(word, "text") for word in word_items]
            line_text = " ".join(word for word in word_text if isinstance(word, str))

        rects = [
            rect
            for rect in (
                cls._rect(cls._value(word, "bounding_rect")) for word in word_items
            )
            if rect is not None
        ]
        if not rects:
            direct = cls._rect(cls._value(line, "bounding_rect"))
            return str(line_text or ""), direct

        left = min(rect[0] for rect in rects)
        top = min(rect[1] for rect in rects)
        right = max(rect[0] + rect[2] for rect in rects)
        bottom = max(rect[1] + rect[3] for rect in rects)
        return str(line_text or ""), (left, top, right - left, bottom - top)

    @staticmethod
    def _block_breaks(
        details: list[tuple[str, tuple[float, float, float, float] | None]],
    ) -> set[int]:
        """Return line indexes after which the visual gap starts a new block."""

        heights = [bounds[3] for _text, bounds in details if bounds is not None]
        if not heights:
            return set()
        typical_height = float(median(heights))
        measured_gaps: list[float] = []
        pair_gaps: dict[int, float] = {}
        for index, ((_text, current), (_next_text, following)) in enumerate(
            zip(details, details[1:])
        ):
            if current is None or following is None:
                continue
            gap = following[1] - (current[1] + current[3])
            if gap < 0:
                continue
            measured_gaps.append(gap)
            pair_gaps[index] = gap
        if not measured_gaps:
            return set()

        if len(measured_gaps) <= 2:
            threshold = max(12.0, typical_height * 1.75)
        else:
            typical_gap = float(median(measured_gaps))
            threshold = max(
                12.0,
                typical_height * 1.5,
                typical_gap + typical_height * 0.5,
            )
        return {index for index, gap in pair_gaps.items() if gap >= threshold}

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

        text = cls._value(result, "text")
        lines = cls._value(result, "lines")
        line_items = cls._items(lines)
        if line_items:
            details = [cls._line_details(line) for line in line_items]
            details = [(line, bounds) for line, bounds in details if line.strip()]
            if details:
                breaks = cls._block_breaks(details)
                parts: list[str] = []
                for index, (line, _bounds) in enumerate(details):
                    parts.append(line)
                    if index < len(details) - 1:
                        parts.append("\n\n" if index in breaks else "\n")
                return "".join(parts)

        return text if isinstance(text, str) else ""

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
