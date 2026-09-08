"""Prepare speech while retaining exact offsets into the displayed text."""
from __future__ import annotations

import re
from dataclasses import dataclass

_BULLET_PREFIX = re.compile(r"^\s*(?:(?:[•◦▪‣⁃∙·●○■□◆◇▶►*]|[-–—])\s+|(?:\d{1,3}|[A-Za-z])[.)]\s+)")
_TRAILING_PAUSE = re.compile(r'''[.,!?…;:]["'”’\])}]*$''')
_WORD = re.compile(r"\w+(?:[\-'’]\w+)*", re.UNICODE)


@dataclass(frozen=True)
class SpeechWordSpan:
    spoken_start: int
    spoken_end: int
    source_start: int
    source_end: int


@dataclass(frozen=True)
class SpeechDocument:
    source_text: str
    spoken_text: str
    words: tuple[SpeechWordSpan, ...]

    def from_source(self, offset: int) -> SpeechDocument:
        """Speak a suffix, preserving absolute offsets into the full editor."""
        word = next((w for w in self.words if w.source_end > offset), None)
        if word is None:
            return SpeechDocument(self.source_text, '', ())
        shift = word.spoken_start
        return SpeechDocument(self.source_text, self.spoken_text[shift:], tuple(
            SpeechWordSpan(w.spoken_start - shift, w.spoken_end - shift, w.source_start, w.source_end)
            for w in self.words if w.spoken_start >= shift
        ))


def prepare_for_speech(text: str) -> SpeechDocument:
    """Carry source positions through list removal and whitespace formatting."""
    source = str(text or '')
    output: list[str] = []
    origins: list[int | None] = []
    segment: list[str] = []
    positions: list[int | None] = []

    def flush() -> None:
        if not segment:
            return
        if output:
            output.append(' ')
            origins.append(None)
        value = ''.join(segment)
        output.extend(segment)
        origins.extend(positions)
        if not _TRAILING_PAUSE.search(value):
            output.append('.')
            origins.append(None)
        segment.clear()
        positions.clear()

    cursor = 0
    for raw_line in source.splitlines(keepends=True):
        line = raw_line.rstrip('\r\n')
        if not line.strip():
            flush()
            cursor += len(raw_line)
            continue
        bullet = _BULLET_PREFIX.match(line)
        content_start = bullet.end() if bullet else 0
        if bullet:
            flush()
        for token in re.finditer(r'\S+', line[content_start:]):
            if segment:
                segment.append(' ')
                positions.append(None)
            start = cursor + content_start + token.start()
            segment.extend(token.group())
            positions.extend(range(start, start + len(token.group())))
        if bullet:
            flush()
        cursor += len(raw_line)
    flush()
    spoken = ''.join(output)
    words = []
    for word in _WORD.finditer(spoken):
        mapped = [i for i in origins[word.start():word.end()] if i is not None]
        if mapped:
            words.append(SpeechWordSpan(word.start(), word.end(), mapped[0], mapped[-1] + 1))
    return SpeechDocument(source, spoken, tuple(words))


def format_for_speech(text: str) -> str:
    return prepare_for_speech(text).spoken_text


def native_offset_map(text: str) -> tuple[int, ...]:
    """Translate Windows UTF-16 boundaries to Python character positions."""
    result = []
    for index, char in enumerate(text):
        result.extend([index] * (2 if ord(char) > 0xFFFF else 1))
    return tuple([*result, len(text)])


__all__ = ['SpeechDocument', 'SpeechWordSpan', 'format_for_speech', 'prepare_for_speech']
