"""Prepare speech while retaining exact offsets into the displayed text."""
from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass
from xml.sax.saxutils import escape

_BULLET_PREFIX = re.compile(r"^\s*(?:(?:[•◦▪‣⁃∙·●○■□◆◇▶►*]|[-–—])\s+|(?:\d{1,3}|[A-Za-z])[.)]\s+)")
_TRAILING_PAUSE = re.compile(r'''[.,!?…;:]["'”’\])}]*$''')
_WORD = re.compile(r"\w+(?:[\-'’]\w+)*", re.UNICODE)
_SENTENCE_END = re.compile(r"[.!?…]+[\"'”’\])}]*\s*$")
_ABBREVIATIONS = frozenset({'mr', 'mrs', 'ms', 'dr', 'prof', 'sr', 'jr', 'st', 'vs', 'etc'})


@dataclass(frozen=True)
class SpeechWordSpan:
    spoken_start: int
    spoken_end: int
    source_start: int
    source_end: int


@dataclass(frozen=True)
class SpeechSentenceSpan:
    spoken_start: int
    spoken_end: int
    source_start: int
    source_end: int


@dataclass(frozen=True)
class SpeechPause:
    """Additional silence before a spoken word, anchored in the source."""

    spoken_offset: int
    source_offset: int
    milliseconds: int


@dataclass(frozen=True)
class SpeechSynthesisInput:
    text: str
    native_to_spoken: tuple[int, ...]
    spoken_to_native: tuple[int, ...]


def synthesis_input(spoken: str, pauses: tuple[SpeechPause, ...], backend: str,
                    language: str = 'en-US') -> SpeechSynthesisInput:
    """Escape prose and keep UTF-16 cue/seek offsets across injected markup."""
    if not pauses:
        reverse = [0]
        for char in spoken:
            reverse.append(reverse[-1] + (2 if ord(char) > 0xFFFF else 1))
        return SpeechSynthesisInput(spoken, native_offset_map(spoken), tuple(reverse))
    if backend not in ('winrt', 'sapi'):
        raise ValueError('Unsupported speech markup backend')
    if not re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*', language):
        language = 'en-US'
    pieces: list[str] = []
    native_to_spoken: list[int] = []
    spoken_to_native = [0] * (len(spoken) + 1)
    native_count = 0

    def emit(value: str, position: int) -> None:
        nonlocal native_count
        pieces.append(value)
        units = len(value.encode('utf-16-le')) // 2
        native_to_spoken.extend([position] * units)
        native_count += units

    if backend == 'winrt':
        emit(f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="{language}">', 0)
    boundaries = {pause.spoken_offset: pause.milliseconds for pause in pauses
                  if 0 < pause.spoken_offset < len(spoken)}
    for index, char in enumerate(spoken):
        if index in boundaries:
            delay = boundaries[index]
            emit(f'<break time="{delay}ms"/>' if backend == 'winrt'
                 else f'<silence msec="{delay}"/>', index)
        spoken_to_native[index] = native_count
        emit(escape(char), index)
    spoken_to_native[-1] = native_count
    if backend == 'winrt':
        emit('</speak>', len(spoken))
    native_to_spoken.append(len(spoken))
    return SpeechSynthesisInput(''.join(pieces), tuple(native_to_spoken), tuple(spoken_to_native))


@dataclass(frozen=True)
class SpeechDocument:
    source_text: str
    spoken_text: str
    words: tuple[SpeechWordSpan, ...]
    sentences: tuple[SpeechSentenceSpan, ...] = ()
    pauses: tuple[SpeechPause, ...] = ()

    def from_source(self, offset: int) -> SpeechDocument:
        """Speak a suffix, preserving absolute offsets into the full editor."""
        word = next((w for w in self.words if w.source_end > offset), None)
        if word is None:
            return SpeechDocument(self.source_text, '', ())
        shift = word.spoken_start
        remaining = tuple(
            SpeechWordSpan(w.spoken_start - shift, w.spoken_end - shift, w.source_start, w.source_end)
            for w in self.words if w.spoken_start >= shift
        )
        return SpeechDocument(self.source_text, self.spoken_text[shift:], remaining,
                              _sentence_spans(self.spoken_text[shift:], remaining),
                              tuple(SpeechPause(p.spoken_offset - shift, p.source_offset, p.milliseconds)
                                    for p in self.pauses if p.spoken_offset > shift))


_STRUCTURE_LIST = re.compile(r"^(\s*)(?:[-+*•◦▪‣⁃∙·●○■□◆◇▶►]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)")
_STRUCTURE_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_STRUCTURE_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")


def structural_pauses(source: str, words: tuple[SpeechWordSpan, ...]) -> tuple[SpeechPause, ...]:
    """Map visual boundaries to the next spoken word; never pause at a wrap."""
    if not words:
        return ()
    events: list[tuple[int, int]] = []
    cursor = 0
    previous_list_depth: int | None = None
    pending = 0
    for raw_line in source.splitlines(keepends=True):
        line = raw_line.rstrip('\r\n')
        stripped = line.strip()
        if not stripped:
            pending = max(pending, 350)
        elif _STRUCTURE_RULE.match(line):
            pending = max(pending, 700)
            previous_list_depth = None
        else:
            heading = _STRUCTURE_HEADING.match(line)
            item = _STRUCTURE_LIST.match(line)
            if heading:
                pending = max(pending, 700)
            elif item:
                depth = len(item.group(1).expandtabs(4))
                pending = max(pending, 180 if depth == previous_list_depth else 350)
                previous_list_depth = depth
            elif previous_list_depth is not None:
                previous_list_depth = None
            if pending:
                events.append((cursor, pending))
                pending = 0
            if heading:
                pending = max(pending, 350)
        cursor += len(raw_line)
    by_spoken: dict[int, SpeechPause] = {}
    source_starts = [word.source_start for word in words]
    for source_offset, milliseconds in events:
        index = bisect_left(source_starts, source_offset)
        if index == 0 or index >= len(words):
            continue
        word = words[index]
        old = by_spoken.get(word.spoken_start)
        if old is None or milliseconds > old.milliseconds:
            by_spoken[word.spoken_start] = SpeechPause(word.spoken_start, word.source_start, milliseconds)
    return tuple(by_spoken[key] for key in sorted(by_spoken))


def _sentence_spans(spoken: str, words: tuple[SpeechWordSpan, ...]) -> tuple[SpeechSentenceSpan, ...]:
    if not words:
        return ()
    result: list[SpeechSentenceSpan] = []
    first = 0
    for index, word in enumerate(words):
        next_word = words[index + 1] if index + 1 < len(words) else None
        between = spoken[word.spoken_end:next_word.spoken_start] if next_word else spoken[word.spoken_end:]
        terminal = bool(_SENTENCE_END.search(between))
        if (terminal and next_word is not None and between == '.'
                and spoken[word.spoken_start:word.spoken_end].isdigit()
                and spoken[next_word.spoken_start:next_word.spoken_end].isdigit()):
            terminal = False
        if terminal and between.lstrip().startswith('.') and spoken[word.spoken_start:word.spoken_end].casefold() in _ABBREVIATIONS:
            terminal = False
        if next_word is None or terminal:
            start_word = words[first]
            result.append(SpeechSentenceSpan(start_word.spoken_start, word.spoken_end,
                                             start_word.source_start, word.source_end))
            first = index + 1
    return tuple(result)


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
        if _STRUCTURE_RULE.match(line):
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
    mapped_words = tuple(words)
    return SpeechDocument(source, spoken, mapped_words, _sentence_spans(spoken, mapped_words),
                          structural_pauses(source, mapped_words))


def format_for_speech(text: str) -> str:
    return prepare_for_speech(text).spoken_text


def native_offset_map(text: str) -> tuple[int, ...]:
    """Translate Windows UTF-16 boundaries to Python character positions."""
    result = []
    for index, char in enumerate(text):
        result.extend([index] * (2 if ord(char) > 0xFFFF else 1))
    return tuple([*result, len(text)])


__all__ = ['SpeechDocument', 'SpeechPause', 'SpeechSentenceSpan', 'SpeechSynthesisInput',
           'SpeechWordSpan', 'format_for_speech', 'prepare_for_speech', 'synthesis_input']
