"""Convert preserved OCR layout into natural plain-text speech pauses."""

from __future__ import annotations

import re


_BULLET_PREFIX = re.compile(
    r"^\s*(?:(?:[•◦▪‣⁃∙·●○■□◆◇▶►*]|[-–—])\s+|(?:\d{1,3}|[A-Za-z])[.)]\s+)"
)
_TRAILING_PAUSE = re.compile(r'''[.,!?…;:]["'”’\])}]*$''')


def _compact_line(value: str) -> str:
    return re.sub(r"[\t \f\v]+", " ", value).strip()


def _with_terminal_pause(value: str) -> str:
    value = value.strip()
    if not value or _TRAILING_PAUSE.search(value):
        return value
    return f"{value}."


def format_for_speech(text: str) -> str:
    """Return speech-ready text while leaving the displayed OCR text untouched.

    Ordinary OCR line wrapping is joined with spaces. Blank-line blocks and
    list items receive sentence-style punctuation so Windows voices pause even
    though they normally treat newlines and bullet symbols as plain whitespace.
    """

    normalised = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not normalised.strip():
        return ""

    spoken_segments: list[str] = []
    blocks = re.split(r"\n[\t ]*\n+", normalised.strip())
    for block in blocks:
        wrapped_lines: list[str] = []

        def flush_wrapped_lines() -> None:
            if not wrapped_lines:
                return
            joined = " ".join(wrapped_lines)
            paused = _with_terminal_pause(joined)
            if paused:
                spoken_segments.append(paused)
            wrapped_lines.clear()

        for raw_line in block.split("\n"):
            line = _compact_line(raw_line)
            if not line:
                continue
            bullet = _BULLET_PREFIX.match(line)
            if bullet is None:
                wrapped_lines.append(line)
                continue

            flush_wrapped_lines()
            item = line[bullet.end() :].strip()
            paused = _with_terminal_pause(item)
            if paused:
                spoken_segments.append(paused)

        flush_wrapped_lines()

    return " ".join(spoken_segments)


__all__ = ["format_for_speech"]
