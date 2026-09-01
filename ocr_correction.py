"""Conservative offline post-processing for game-dialogue OCR text."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from importlib.resources import files
from threading import Lock
from typing import Any, Final, Mapping

try:
    from symspellpy import SymSpell, Verbosity
except ImportError:  # The rule-based and custom stages still work without it.
    SymSpell = None  # type: ignore[assignment]
    Verbosity = None  # type: ignore[assignment]


@dataclass(frozen=True)
class ReplacementRule:
    """One literal user-authored replacement rule."""

    original: str
    replacement: str
    enabled: bool = True
    case_sensitive: bool = False
    whole_word: bool = True


@dataclass(frozen=True)
class CorrectionOptions:
    """Immutable settings snapshot for one correction pass."""

    enabled: bool = True
    strength: str = "conservative"
    replacements: tuple[ReplacementRule, ...] = ()
    protected_words: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "CorrectionOptions":
        """Create an immutable correction snapshot from validated app settings."""
        value = value if isinstance(value, Mapping) else {}
        strength = str(value.get("strength", "conservative")).strip().lower()
        if strength not in {"conservative", "balanced", "strong"}:
            strength = "conservative"
        rules: list[ReplacementRule] = []
        raw_rules = value.get("replacements", ())
        if isinstance(raw_rules, (list, tuple)):
            for raw_rule in raw_rules:
                if not isinstance(raw_rule, Mapping):
                    continue
                original = raw_rule.get("original")
                replacement = raw_rule.get("replacement")
                if isinstance(original, str) and original and isinstance(replacement, str):
                    rules.append(
                        ReplacementRule(
                            original=original,
                            replacement=replacement,
                            enabled=raw_rule.get("enabled", True) is True,
                            case_sensitive=raw_rule.get("case_sensitive", False) is True,
                            whole_word=raw_rule.get("whole_word", True) is True,
                        )
                    )
        raw_protected = value.get("protected_words", ())
        protected = tuple(item for item in raw_protected if isinstance(item, str) and item) if isinstance(raw_protected, (list, tuple)) else ()
        return cls(
            enabled=value.get("enabled", True) is True,
            strength=strength,
            replacements=tuple(rules),
            protected_words=protected,
        )


@dataclass(frozen=True)
class TextCorrection:
    """A traceable change accepted by the correction engine."""

    start: int
    end: int
    original: str
    replacement: str
    reason: str
    confidence: float
    source: str = "automatic"


@dataclass(frozen=True)
class CorrectionResult:
    """Raw and corrected forms plus diagnostics for one OCR result."""

    raw_text: str
    corrected_text: str
    corrections: tuple[TextCorrection, ...]
    elapsed_ms: float

    @property
    def changed(self) -> bool:
        return self.raw_text != self.corrected_text


@dataclass(frozen=True)
class _ContextRule:
    pattern: re.Pattern[str]
    replacement: str
    reason: str
    confidence: float


class OcrCorrector:
    """Correct likely OCR errors while preserving uncertain game terminology."""

    _WORD_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?<![\w’'])[A-Za-z][A-Za-z'’-]{2,}(?![\w’'])")
    _ALNUM_WORD_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?<![\w’'])(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,}(?![\w’'])")

    _CONSERVATIVE_RULES: Final[tuple[_ContextRule, ...]] = (
        _ContextRule(
            re.compile(r"\bI (?:definitely\s+|clearly\s+|just\s+|actually\s+|really\s+)?(?P<target>sow)(?=\s+(?:it|that|this|him|her|them)\b)", re.IGNORECASE),
            "saw",
            "a/o confusion in a perception-verb context",
            0.98,
        ),
        _ContextRule(
            re.compile(r"\bThere (?:was|is) (?P<target>o)(?=\s+[A-Za-z])", re.IGNORECASE),
            "a",
            "a/o confusion in an article position",
            0.99,
        ),
        _ContextRule(
            re.compile(r"\bIn (?P<target>o)(?=\s+(?:second|moment|minute|hour|day|week|while|flash|heartbeat)\b)", re.IGNORECASE),
            "a",
            "a/o confusion in a time phrase",
            0.99,
        ),
        _ContextRule(
            re.compile(r"\b(?P<target>oll)(?=\s+of\b)", re.IGNORECASE),
            "all",
            "a/o confusion in the phrase 'all of'",
            0.99,
        ),
        _ContextRule(
            re.compile(r"\bsent (?P<target>on)(?=\s+[A-Z][A-Za-z'’-]*(?:\s+for\b|[.!?,;:]))"),
            "an",
            "a/o confusion in an article position after 'sent'",
            0.96,
        ),
        _ContextRule(
            re.compile(r"\b(?P<target>[l1])(?=\s+(?:am|was|will|would|can|could|have|had|do|did|saw|think|know|need|want|remember|believe)\b)", re.IGNORECASE),
            "I",
            "I/l/1 confusion in a first-person pronoun position",
            0.99,
        ),
        _ContextRule(
            re.compile(r"\b(?P<target>5he)(?=\s+(?:is|was|will|would|can|could|has|had|said|went|did|does|looks|seems)\b)", re.IGNORECASE),
            "She",
            "S/5 confusion in a pronoun position",
            0.98,
        ),
        _ContextRule(
            re.compile(r"\b(?P<target>tbe)\b", re.IGNORECASE),
            "the",
            "h/b confusion in the common word 'the'",
            0.98,
        ),
        _ContextRule(
            re.compile(r"\b(?P<target>tbis)\b", re.IGNORECASE),
            "this",
            "h/b confusion in the common word 'this'",
            0.98,
        ),
        _ContextRule(
            re.compile(r"\b(?P<target>witb)\b", re.IGNORECASE),
            "with",
            "h/b confusion in the common word 'with'",
            0.98,
        ),
    )

    def __init__(self) -> None:
        self._symspell = None
        self._symspell_lock = Lock()

    def warm_up(self) -> None:
        """Load the optional dictionary ahead of the first balanced correction."""
        self._get_symspell()

    def correct(self, raw_text: str, options: CorrectionOptions | None = None) -> CorrectionResult:
        """Return a conservative corrected form without mutating *raw_text*."""
        started = time.perf_counter()
        raw = str(raw_text or "")
        settings = options or CorrectionOptions()
        if not settings.enabled or not raw:
            return CorrectionResult(raw, raw, (), (time.perf_counter() - started) * 1000.0)

        text = raw
        raw_map = [(index, index + 1) for index in range(len(raw))]
        traced: list[TextCorrection] = []

        for rule in settings.replacements:
            if not rule.enabled or not rule.original:
                continue
            edits = self._replacement_corrections(text, rule)
            text, raw_map, stage_trace = self._apply_stage(text, raw_map, edits)
            traced.extend(stage_trace)

        protected = self._protected_ranges(text, settings)
        contextual = self._contextual_corrections(text, protected)
        text, raw_map, stage_trace = self._apply_stage(text, raw_map, contextual)
        traced.extend(stage_trace)

        if settings.strength in {"balanced", "strong"}:
            protected = self._protected_ranges(text, settings)
            statistical = self._statistical_corrections(text, protected, settings.strength)
            text, raw_map, stage_trace = self._apply_stage(text, raw_map, statistical)
            traced.extend(stage_trace)
            spacing = self._spacing_corrections(text)
            text, raw_map, stage_trace = self._apply_stage(text, raw_map, spacing)
            traced.extend(stage_trace)
        traced.sort(key=lambda item: (item.start, item.end, 0 if item.source == "custom" else 1))
        return CorrectionResult(raw, text, tuple(traced), (time.perf_counter() - started) * 1000.0)

    def _get_symspell(self):
        if self._symspell is not None:
            return self._symspell
        if SymSpell is None:
            return None
        with self._symspell_lock:
            if self._symspell is None:
                engine = SymSpell(max_dictionary_edit_distance=2, prefix_length=7)
                data = files("symspellpy")
                if not engine.load_dictionary(str(data / "frequency_dictionary_en_82_765.txt"), 0, 1):
                    return None
                engine.load_bigram_dictionary(str(data / "frequency_bigramdictionary_en_243_342.txt"), 0, 2)
                self._symspell = engine
        return self._symspell

    def _statistical_corrections(
        self,
        text: str,
        protected: list[tuple[int, int]],
        strength: str,
    ) -> list[TextCorrection]:
        engine = self._get_symspell()
        if engine is None or Verbosity is None:
            return []

        proposed: list[TextCorrection] = []
        max_distance = 2 if strength == "strong" else 1
        for match in self._ALNUM_WORD_PATTERN.finditer(text):
            start, end = match.span()
            if any(start < protected_end and end > protected_start for protected_start, protected_end in protected):
                continue
            original = match.group(0)
            # A single digit embedded in a word is often 0/O, 1/I, 5/S, 8/B, 6/G, or 2/Z.
            if sum(character.isdigit() for character in original) != 1:
                continue
            lookup = engine.lookup(
                original.lower(),
                Verbosity.TOP,
                max_edit_distance=1,
                include_unknown=True,
            )
            suggestion = lookup[0] if lookup else None
            if suggestion and suggestion.distance == 1 and suggestion.count >= 10_000 and suggestion.term.isalpha():
                proposed.append(
                    TextCorrection(
                        start,
                        end,
                        original,
                        self._transfer_case(original, suggestion.term),
                        "dictionary-supported letter/digit OCR correction",
                        0.92,
                    )
                )
        for match in self._WORD_PATTERN.finditer(text):
            start, end = match.span()
            if any(start < protected_end and end > protected_start for protected_start, protected_end in protected):
                continue
            original = match.group(0)
            # Mixed capitals are often item IDs or deliberately styled game terms.
            if original.isupper() or (not original.islower() and not original.istitle()):
                continue

            lookup = engine.lookup(
                original.lower(),
                Verbosity.TOP,
                max_edit_distance=max_distance,
                include_unknown=True,
            )
            suggestion = lookup[0] if lookup else None
            if suggestion and suggestion.distance and suggestion.distance <= max_distance and suggestion.count >= 10_000:
                replacement = self._transfer_case(original, suggestion.term)
                proposed.append(
                    TextCorrection(
                        start,
                        end,
                        original,
                        replacement,
                        "dictionary-supported OCR character correction",
                        0.91 if suggestion.distance == 1 else 0.82,
                    )
                )
                continue

            # Only split long, lower-case tokens; title-cased unknowns are commonly names.
            if len(original) >= 7 and original.islower():
                segmented = engine.word_segmentation(original.lower())
                replacement = segmented.corrected_string
                parts = replacement.split()
                if (
                    len(parts) == 2
                    and all(len(part) >= 2 for part in parts)
                    and segmented.distance_sum == 1
                    and replacement.replace(" ", "") == original.lower()
                ):
                    proposed.append(
                        TextCorrection(
                            start,
                            end,
                            original,
                            replacement,
                            "dictionary-supported missing space",
                            0.90,
                        )
                    )

        return proposed

    @staticmethod
    def _spacing_corrections(text: str) -> list[TextCorrection]:
        proposed: list[TextCorrection] = []
        for match in re.finditer(r"[ \t]+(?=[,.;:!?])", text):
            proposed.append(TextCorrection(match.start(), match.end(), match.group(0), "", "removed space before punctuation", 0.99))
        for match in re.finditer(r"(?<=[,;:!?])(?=[A-Za-z])", text):
            proposed.append(TextCorrection(match.start(), match.end(), "", " ", "restored space after punctuation", 0.97))
        for match in re.finditer(r"[ \t]{2,}", text):
            proposed.append(TextCorrection(match.start(), match.end(), match.group(0), " ", "collapsed extra spaces", 0.99))
        return sorted(proposed, key=lambda item: item.start)

    @staticmethod
    def _replacement_corrections(text: str, rule: ReplacementRule) -> list[TextCorrection]:
        escaped = re.escape(rule.original)
        if rule.whole_word:
            escaped = rf"(?<![\w’']){escaped}(?![\w’'])"
        flags = 0 if rule.case_sensitive else re.IGNORECASE
        return [
            TextCorrection(
                match.start(),
                match.end(),
                match.group(0),
                rule.replacement,
                f"custom replacement: {rule.original}",
                1.0,
                "custom",
            )
            for match in re.finditer(escaped, text, flags)
        ]

    @staticmethod
    def _protected_ranges(text: str, options: CorrectionOptions) -> list[tuple[int, int]]:
        terms = [term for term in options.protected_words if term]
        terms.extend(rule.replacement for rule in options.replacements if rule.enabled and rule.replacement)
        ranges: list[tuple[int, int]] = []
        for term in sorted(set(terms), key=len, reverse=True):
            pattern = rf"(?<![\w’']){re.escape(term)}(?![\w’'])"
            ranges.extend(match.span() for match in re.finditer(pattern, text, re.IGNORECASE))
        return ranges

    def _contextual_corrections(self, text: str, protected: list[tuple[int, int]] | None = None) -> list[TextCorrection]:
        protected = protected or []
        proposed: list[TextCorrection] = []
        for rule in self._CONSERVATIVE_RULES:
            for match in rule.pattern.finditer(text):
                start, end = match.span("target")
                if any(start < protected_end and end > protected_start for protected_start, protected_end in protected):
                    continue
                original = text[start:end]
                replacement = self._transfer_case(original, rule.replacement)
                proposed.append(
                    TextCorrection(start, end, original, replacement, rule.reason, rule.confidence)
                )

        accepted: list[TextCorrection] = []
        occupied_until = -1
        for correction in sorted(proposed, key=lambda item: (item.start, -item.confidence)):
            if correction.start >= occupied_until:
                accepted.append(correction)
                occupied_until = correction.end
        return accepted

    @staticmethod
    def _transfer_case(original: str, replacement: str) -> str:
        if original.isupper():
            return replacement.upper()
        if original[:1].isupper():
            return replacement.capitalize()
        return replacement

    @staticmethod
    def _apply_stage(
        text: str,
        raw_map: list[tuple[int, int]],
        corrections: list[TextCorrection],
    ) -> tuple[str, list[tuple[int, int]], list[TextCorrection]]:
        if not corrections:
            return text, raw_map, []
        parts: list[str] = []
        mapped: list[tuple[int, int]] = []
        traced: list[TextCorrection] = []
        cursor = 0
        for correction in corrections:
            parts.append(text[cursor:correction.start])
            mapped.extend(raw_map[cursor:correction.start])
            parts.append(correction.replacement)
            if correction.start < correction.end and raw_map:
                raw_start = raw_map[correction.start][0]
                raw_end = raw_map[correction.end - 1][1]
            else:
                raw_start = raw_end = raw_map[correction.start - 1][1] if correction.start else 0
            mapped.extend([(raw_start, raw_end)] * len(correction.replacement))
            traced.append(
                TextCorrection(
                    raw_start,
                    raw_end,
                    correction.original,
                    correction.replacement,
                    correction.reason,
                    correction.confidence,
                    correction.source,
                )
            )
            cursor = correction.end
        parts.append(text[cursor:])
        mapped.extend(raw_map[cursor:])
        return "".join(parts), mapped, traced
