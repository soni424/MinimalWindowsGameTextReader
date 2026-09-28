"""Safe Markdown rendering and source-mapped prose for the Reader.

The editor's source remains authoritative. Neither rendering nor speech changes it.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import bleach
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.tasklists import tasklists_plugin

from speech_text import (SpeechDocument, SpeechWordSpan, _sentence_spans,
                         ordered_marker_chars, structural_pauses)


_WORD = re.compile(r"\w+(?:[\-'’]\w+)*", re.UNICODE)
_PAUSE = re.compile(r'''[.,!?…;:]["'”’\])}]*$''')
_LIST = re.compile(r"^\s*(?:[-+*]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)")
_ORDERED_LIST = re.compile(r"^\s*(\d{1,3})[.)]\s+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_QUOTE = re.compile(r"^\s*>\s?")
_FOOTNOTE = re.compile(r"^\s*\[\^[^]]+\]:\s*")
_REFERENCE = re.compile(r"^\s*\[(?!\^)[^]]+\]:\s+\S+")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?$")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_DANGEROUS_BLOCK = re.compile(r'<(script|style|iframe|object|form)\b[^>]*>.*?</\1\s*>', re.I | re.S)
_RAW_INPUT = re.compile(r'<(?:input|img)\b[^>]*>', re.I)
_MARKDOWN_MARKER = re.compile(r"(?m)^\s{0,3}(?:#{1,6}\s|>\s|[-+*]\s|\d+[.)]\s|\|.+\|\s*$)|\*\*[^*]+\*\*|\[[^]]+\]\([^)]+\)|```|\$\$|\[\^[^]]+\]:")
_TAGS = frozenset({
    'p', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'em', 'del', 's',
    'blockquote', 'ul', 'ol', 'li', 'input', 'table', 'thead', 'tbody', 'tr',
    'th', 'td', 'pre', 'code', 'hr', 'a', 'span', 'div', 'sup', 'sub', 'section',
    'details', 'summary', 'kbd',
})
_ATTRS = {
    '*': ['class'],
    'a': ['href', 'title', 'id'],
    'span': ['data-image-index', 'data-tex'],
    'div': ['data-tex'],
    'ol': ['start'], 'li': ['id', 'value'],
    'input': ['type', 'checked', 'disabled'],
    'th': ['align'], 'td': ['align'],
}


def looks_like_markdown(text: str) -> bool:
    """Require an unambiguous construct before switching tabs after paste."""
    return bool(_MARKDOWN_MARKER.search(text or ''))


def _safe_remote_image(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password


@dataclass(frozen=True)
class RenderedMarkdown:
    html: str
    images: tuple[str, ...]


def render_markdown(source: str) -> RenderedMarkdown:
    """Render into sanitized body HTML; remote images remain inert placeholders."""
    images: list[str] = []
    md = (MarkdownIt('commonmark', {'html': True, 'linkify': False})
          .enable(['table', 'strikethrough'])
          .use(dollarmath_plugin)
          .use(footnote_plugin)
          .use(tasklists_plugin))

    def image_rule(_renderer, tokens, index, _options, _env):
        token = tokens[index]
        alt = token.content or 'Image'
        url = token.attrGet('src') or ''
        if not _safe_remote_image(url):
            return f'<span class="md-image-blocked">[Image: {html.escape(alt)} — unavailable]</span>'
        number = len(images)
        images.append(url)
        return (f'<span class="md-image" data-image-index="{number}">'
                f'[Image: {html.escape(alt)} — load images to view]</span>')

    def math_inline(_renderer, tokens, index, _options, _env):
        return f'<span class="math-inline" data-tex="{html.escape(tokens[index].content, quote=True)}"></span>'

    def math_block(_renderer, tokens, index, _options, _env):
        return f'<div class="math-block" data-tex="{html.escape(tokens[index].content, quote=True)}"></div>'

    md.add_render_rule('image', image_rule)
    md.add_render_rule('math_inline', math_inline)
    md.add_render_rule('math_block', math_block)
    rendered = md.render(_RAW_INPUT.sub('', _DANGEROUS_BLOCK.sub('', str(source or ''))))
    cleaned = bleach.clean(rendered, tags=_TAGS, attributes=_ATTRS,
                           protocols=['http', 'https', 'mailto'], strip=True)
    return RenderedMarkdown(cleaned, tuple(images))


def _inline_chars(line: str, offset: int) -> list[tuple[str, int]]:
    """Extract visible inline prose without losing repeated-word positions."""
    out: list[tuple[str, int]] = []
    i = 0
    while i < len(line):
        part = line[i:]
        if part.startswith('<!--'):
            end = line.find('-->', i + 4)
            i = len(line) if end < 0 else end + 3
            continue
        if part.startswith('<'):
            end = line.find('>', i + 1)
            if end >= 0:
                i = end + 1
                continue
        if part.startswith('`'):
            marker = re.match(r'`+', part).group()
            end = line.find(marker, i + len(marker))
            if end >= 0:
                i = end + len(marker)
                continue
        if part.startswith('$'):
            end = line.find('$', i + 1)
            if end >= 0:
                i = end + 1
                continue
        if part.startswith('![', 0) or part.startswith('[', 0):
            image = part.startswith('![')
            label_start = i + (2 if image else 1)
            label_end = line.find(']', label_start)
            if label_end >= 0 and line.startswith('(', label_end + 1):
                url_end = line.find(')', label_end + 2)
                if url_end >= 0:
                    out.extend(_inline_chars(line[label_start:label_end], offset + label_start))
                    i = url_end + 1
                    continue
            if label_end >= 0 and line.startswith('[', label_end + 1):
                reference_end = line.find(']', label_end + 2)
                if reference_end >= 0:
                    out.extend(_inline_chars(line[label_start:label_end], offset + label_start))
                    i = reference_end + 1
                    continue
            if part.startswith('[^') and label_end >= 0:
                i = label_end + 1
                continue
        if part.startswith('\\') and i + 1 < len(line):
            i += 1
        if line[i] not in '*_~|':
            out.append((line[i], offset + i))
        i += 1
    return out


def prepare_markdown_for_speech(source: str) -> SpeechDocument:
    """Speak rendered prose while mapping every retained word to raw Markdown."""
    source = str(source or '')
    masked = _DANGEROUS_BLOCK.sub(
        lambda match: re.sub(r'[^\r\n]', ' ', match.group()), source)
    output: list[str] = []
    origins: list[int | None] = []
    segment: list[tuple[str, int]] = []
    in_fence = False
    in_math = False

    def flush() -> None:
        nonlocal segment
        if not segment:
            return
        trimmed = ''.join(c for c, _ in segment).strip()
        if not trimmed:
            segment = []
            return
        if output:
            output.append(' ')
            origins.append(None)
        for token in re.finditer(r'\S+', ''.join(c for c, _ in segment)):
            token_text = token.group()
            if output and output[-1] not in (' ',) and not re.fullmatch(r'[.,!?…;:]+', token_text):
                output.append(' ')
                origins.append(None)
            for char, origin in segment[token.start():token.end()]:
                output.append(char)
                origins.append(origin)
        while output and output[-1] == ' ':
            output.pop(); origins.pop()
        if not _PAUSE.search(''.join(output)):
            output.append('.')
            origins.append(None)
        segment = []

    cursor = 0
    for raw_line in masked.splitlines(keepends=True):
        line = raw_line.rstrip('\r\n')
        stripped = line.strip()
        if stripped.startswith('```') or stripped.startswith('~~~'):
            flush()
            in_fence = not in_fence
            cursor += len(raw_line)
            continue
        if in_fence:
            cursor += len(raw_line)
            continue
        if stripped.startswith('$$'):
            flush()
            in_math = not (in_math or stripped.endswith('$$') and len(stripped) > 2)
            cursor += len(raw_line)
            continue
        if in_math:
            cursor += len(raw_line)
            continue
        if not stripped or _RULE.match(line) or _TABLE_RULE.match(line) or _REFERENCE.match(line):
            flush()
            cursor += len(raw_line)
            continue
        heading = _HEADING.match(line)
        bullet = _LIST.match(line)
        footnote = _FOOTNOTE.match(line)
        quote = _QUOTE.match(line)
        table = '|' in line and line.count('|') >= 2
        if heading or bullet or footnote or table:
            flush()
        prefix = heading or bullet or footnote or quote
        start = prefix.end() if prefix else 0
        if segment:
            segment.append((' ', cursor + start))
        numbered = _ORDERED_LIST.match(line) if bullet else None
        if numbered:
            segment.extend(ordered_marker_chars(numbered, cursor))
            segment.append((' ', cursor + start))
        segment.extend(_inline_chars(line[start:], cursor + start))
        if heading or bullet or footnote or table:
            flush()
        cursor += len(raw_line)
    flush()
    spoken = ''.join(output)
    mapped_words = []
    for word in _WORD.finditer(spoken):
        positions = [p for p in origins[word.start():word.end()] if p is not None]
        if positions:
            mapped_words.append(SpeechWordSpan(word.start(), word.end(), positions[0], positions[-1] + 1))
    words = tuple(mapped_words)
    return SpeechDocument(source, spoken, words, _sentence_spans(spoken, words),
                          structural_pauses(source, words))


__all__ = ['RenderedMarkdown', 'looks_like_markdown', 'render_markdown', 'prepare_markdown_for_speech']
