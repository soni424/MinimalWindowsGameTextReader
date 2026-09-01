"""Thread-safe text state for OCR history and speech replay."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from ocr_correction import CorrectionResult


@dataclass
class ReaderTextState:
    """Keep OCR source, final output, replay text, and live speech independent."""

    raw_ocr_text: str = ""
    corrected_ocr_text: str = ""
    last_successful_text: str = ""
    currently_spoken_text: str = ""
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False, compare=False)

    def accept_success(self, result: CorrectionResult) -> bool:
        """Store a completed OCR result and return whether it is replayable."""

        with self._lock:
            self.raw_ocr_text = result.raw_text
            self.corrected_ocr_text = result.corrected_text
            final_text = result.corrected_text.strip()
            if final_text:
                self.last_successful_text = final_text
                return True
            return False

    def begin_speech(self, text: str) -> None:
        with self._lock:
            self.currently_spoken_text = text.strip()

    def end_speech(self, text: str = "") -> None:
        """Clear only the utterance that actually ended, preserving newer speech."""

        with self._lock:
            if not text or self.currently_spoken_text == text.strip():
                self.currently_spoken_text = ""

    def clear_history(self) -> None:
        with self._lock:
            self.raw_ocr_text = ""
            self.corrected_ocr_text = ""
            self.last_successful_text = ""

    @property
    def can_read_again(self) -> bool:
        with self._lock:
            return bool(self.last_successful_text)


__all__ = ["ReaderTextState"]
