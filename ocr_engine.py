"""Native Windows OCR integration using the ``winocr`` package."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

from PIL import Image


class OcrError(RuntimeError):
    """Raised when the Windows Media OCR API cannot process an image."""


class OcrEngine:
    """Recognise text from Pillow images through Windows' Media OCR API."""

    DEFAULT_LANGUAGE: Final[str] = "en"

    def __init__(self, language: str = DEFAULT_LANGUAGE) -> None:
        self.language = language

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
            import winocr

            # Windows OCR accepts RGB/RGBA image data.  Converting palette and
            # grayscale captures avoids a WinRT bitmap conversion failure.
            prepared = image.convert("RGB") if image.mode not in {"RGB", "RGBA"} else image
            result = winocr.recognize_pil_sync(prepared, lang=self.language)
        except ImportError as exc:
            raise OcrError("winocr is not installed. Install the app requirements first.") from exc
        except Exception as exc:
            raise OcrError(f"Windows OCR failed: {exc}") from exc
        return self.clean_text(self._result_text(result))
