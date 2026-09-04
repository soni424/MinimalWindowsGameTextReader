"""Convert preserved OCR layout into natural plain-text speech pauses."""

from __future__ import annotations

import re
from dataclasses import dataclass


_BULLET_PREFIX = re.compile(
    r"^\s*(?:(?:[•◦▪‣⁃∙·●○■□◆◇▶►*]|[-–—])\s+|(?:\d{1,3}|[A-Za-z])[.)]\s+)"
)
_TRAILING_PAUSE = re.compile(r'''[.,!?…;:]["'”’\])}]*$''')
_WORD = re.compile(r"\w+(?:[\-'’]\w+)*", re.UNICODE)


@dataclass(frozen=True)
class SpeechWordSpan:
    """Map one spoken word back to the text displayed in the reader."""

    spoken_start: int
    spoken_end: int
    source_start: int
    source_end: int


@dataclass(frozen=True)
class SpeechDocument:
    """Speech-ready text plus stable source positions for live highlighting."""

    source_text: str
    spoken_text: str
    words: tuple[SpeechWordSpan, ...]


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


def prepare_for_speech(text: str) -> SpeechDocument:
    """Format text for speech and retain positions for every preserved word.

    Layout formatting only removes list prefixes, compacts whitespace, and adds
    pause punctuation.  A forward alignment is therefore sufficient and, unlike
    a character rewrite map, remains easy to audit for game-specific text.
    """

    source_text = str(text or "")
    spoken_text = format_for_speech(source_text)
    if not spoken_text:
        return SpeechDocument(source_text, "", ())

    source_words = list(_WORD.finditer(source_text))
    source_index = 0
    mapped: list[SpeechWordSpan] = []
    for spoken_word in _WORD.finditer(spoken_text):
        while source_index < len(source_words):
            source_word = source_words[source_index]
            source_index += 1
            if source_word.group(0) != spoken_word.group(0):
                continue
            mapped.append(
                SpeechWordSpan(
                    spoken_word.start(),
                    spoken_word.end(),
                    source_word.start(),
                    source_word.end(),
                )
            )
            break

    return SpeechDocument(source_text, spoken_text, tuple(mapped))


__all__ = ["SpeechDocument", "SpeechWordSpan", "format_for_speech", "prepare_for_speech"]
