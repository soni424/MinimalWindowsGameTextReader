"""Bounded offline OCR candidates with grammatical and phrase evidence."""
from __future__ import annotations
from dataclasses import dataclass
from functools import lru_cache
import math
import re

TOKEN = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
CONFUSIONS = (('o', 'a'), ('a', 'o'), ('l', 'i'), ('i', 'l'),
              ('rn', 'm'), ('m', 'rn'), ('cl', 'd'), ('d', 'cl'),
              ('c', 'e'), ('e', 'c'), ('vv', 'w'), ('w', 'vv'), ('h', 'b'), ('b', 'h'))
# Each pattern requires a grammatical/phrase cue; these are not global substitutions.
GRAMMAR = (
    (r"\b(?:I|you|we|they|I'll|you'll|we'll|they'll|don't|doesn't|didn't|must|will|would|could|should)\s+(?P<t>hove)\b", 'have'),
    (r"\b(?:go|going|be|come|coming|get|head|went)\s+(?P<t>bock)\b", 'back'),
    (r"\b(?P<t>moke)(?=\s+(?:it|sure|a|an|the|this|that|me|you)\b)", 'make'),
    (r"\b(?:he|she|it|land|world|town|place)\s+(?P<t>hos)\b", 'has'),
    (r"\b(?:take|become|not|was|is|in|for|and|there's|there’s|lot)\s+(?P<t>o)(?=\s+\w)", 'a'),
    (r"\b(?:I|you|he|she|we|they)\s+(?P<t>foiled)(?=,\s*but\b[^!?]{0,100}\bsucceed\b)", 'failed'),
    (r"\b(?:I'm|I’m|I am|you're|you are|she is|he is)\s+(?:so\s+|very\s+)?(?P<t>scored)(?=[,!.?])", 'scared'),
    (r"\b(?:you|I|we|they)\s+(?P<t>soy)(?=\s+(?:that|this|it|so|something|anything)\b)", 'say'),
    (r"\bkick\s+(?:your|his|her|their|my)\s+(?P<t>oss)\b", 'ass'),
    (r"\b(?P<t>ond)(?=\s+(?:much|more|then|also)\b)", 'and'),
    (r"\b(?P<t>wont)(?=\s+to\s+(?:go|be|see|know|live|leave|stay)\b)", 'want'),
    (r"\b(?P<t>l)(?=\s*(?:\.{3}|…))", 'I'),
    (r"\b(?P<t>1)(?=\s*(?:\.{3}|…)\s*(?:want|wont|wish|hope)\b)", 'I'),
    (r"\b(?:that's|that’s|that is|it's|it is|find|found|sent)\s+(?P<t>on)(?=\s+(?:order|Angel|enemy|item|answer|opportunity)\b)", 'an'),
    (r"\b(?P<t>comp)(?=\s+out\b)", 'camp'),
    (r"\b(?:vines|apples|pears|fruit),?\s+(?P<t>gropes)\b", 'grapes'),
    (r"\b(?:painful|harsh|cruel)\s+(?P<t>realit)\b", 'reality'),
    (r"\b(?P<t>dont)(?=\s+(?:have|give|know|want|leave|blame|talk)\b)", "don't"),
    (r"\b(?P<t>doesnt)(?=\s+(?:have|know|want|need)\b)", "doesn't"),
)
RULES = tuple((re.compile(pattern, re.IGNORECASE), replacement) for pattern, replacement in GRAMMAR)


@dataclass(frozen=True)
class Candidate:
    start: int
    end: int
    replacement: str
    reason: str
    confidence: float
    automatic: bool = True


@lru_cache(maxsize=4096)
def visual_candidates(word: str) -> frozenset[str]:
    if len(word) > 48:
        return frozenset()
    candidates = set()
    for before, after in CONFUSIONS:
        for match in re.finditer(re.escape(before), word):
            candidates.add(word[:match.start()] + after + word[match.end():])
    # Duplicated or missing edge letters are considered, never accepted alone.
    for i in range(1, len(word)):
        if word[i] == word[i - 1]:
            candidates.add(word[:i] + word[i + 1:])
    for letter in 'abcdefghijklmnopqrstuvwxyz':
        candidates.add(letter + word)
        candidates.add(word + letter)
    return frozenset(candidates)


def visual_cost(original: str, candidate: str) -> float:
    """Favor common glyph errors over speculative insertion/deletion repairs."""
    if candidate == original:
        return 0.0
    for before, after in CONFUSIONS:
        for match in re.finditer(re.escape(before), original):
            if original[:match.start()] + after + original[match.end():] == candidate:
                return 0.35 if (before, after) in (('o', 'a'), ('a', 'o'), ('l', 'i'), ('rn', 'm')) else 0.6
    return 1.25 if abs(len(original) - len(candidate)) <= 1 else 2.0


def analyze(text: str, dictionary, protected: list[tuple[int, int]], strength: str) -> list[Candidate]:
    proposals: list[Candidate] = []
    def blocked(start, end):
        return any(start < b and end > a for a, b in protected)
    def add(start, end, replacement, reason, confidence=0.98, automatic=True, preserve_case=True):
        if blocked(start, end) or text[start:end] == replacement:
            return
        original = text[start:end]
        if replacement != 'I' and preserve_case:
            if original.isupper():
                replacement = replacement.upper()
            elif original.istitle():
                replacement = replacement[0].upper() + replacement[1:]
        proposals.append(Candidate(start, end, replacement, reason, confidence, automatic))

    for pattern, replacement in RULES:
        for match in pattern.finditer(text):
            a, b = match.span('t')
            add(a, b, replacement, 'grammar and OCR character-confusion context')

    # A previous explicit address is evidence, not a global dictionary of names.
    anchors = []
    for match in re.finditer(r"\b([A-Z][a-z]{2,})\s*,\s*(?:do you|you|why|please)\b", text):
        anchors.append((match.group(1), match.start()))
    for match in re.finditer(r"\b(?:Thank you|thanks)[,.…\s]+(?P<name>[A-Z][a-z]{2,})\b", text, re.IGNORECASE):
        a, b = match.span('name')
        original = text[a:b]
        candidates = {name for name, pos in anchors if pos < a and name.lower() in visual_candidates(original.lower())}
        if len(candidates) == 1 and not any(name.casefold() == original.casefold() for name, _ in anchors):
            add(a, b, candidates.pop(), 'earlier address in this passage identifies the name', 0.97)

    # Strongly missing material is offered for review, not inserted into speech.
    fragments = (
        (r"\b(?:see|visit)\s+(?P<t>t\s+t\s+ace)\b", 'that place', 'several letters are missing; verify the proposed phrase'),
        (r"\blike\s+(?P<t>Ing)(?=\s+the\s+lottery\b)", 'winning', 'incomplete word in a familiar phrase; verify against the game'),
    )
    for pattern, replacement, reason in fragments:
        for match in re.finditer(pattern, text):
            add(*match.span('t'), replacement, reason, 0.65, False, preserve_case=False)

    # Short line-split words can be rejoined only when the pieces are not both words.
    if dictionary is not None:
        words = dictionary.words
        for match in re.finditer(r"\b([a-z]{2,})[-]?\n[ \t]*([a-z]{2,})\b", text):
            first, second = match.group(1), match.group(2)
            joined = first + second
            before = TOKEN.findall(text[max(0, match.start() - 30):match.start()])
            after = TOKEN.findall(text[match.end():match.end() + 30])
            context = (before and after
                       and dictionary.bigrams.get(before[-1].lower() + ' ' + joined, 0) > 10000
                       and dictionary.bigrams.get(joined + ' ' + after[0].lower(), 0) > 10000)
            if joined in words and words[joined] >= 10000 and (first not in words or second not in words):
                add(*match.span(), joined, 'line-split word and neighboring phrase evidence', 0.97 if context else 0.7,
                    bool(context) and strength != 'conservative')

    if dictionary is not None:
        tokens = list(TOKEN.finditer(text))
        for index, match in enumerate(tokens):
            a, b = match.span()
            word = match.group()
            if not word.islower() or not 3 <= len(word) <= 48 or blocked(a, b):
                continue
            if any(a < p.end and b > p.start for p in proposals):
                continue
            before = tokens[index - 1].group().lower() if index else ''
            after = tokens[index + 1].group().lower() if index + 1 < len(tokens) else ''
            # Single OCR wraps are context; paragraphs, lists and sentence ends are not.
            boundary = r"[.!?…:;•]|\n\s*\n"
            if index and re.search(boundary, text[tokens[index - 1].end():a]):
                before = ''
            if index + 1 < len(tokens) and re.search(boundary, text[b:tokens[index + 1].start()]):
                after = ''
            def evidence(candidate):
                left = dictionary.bigrams.get(before + ' ' + candidate, 0)
                right = dictionary.bigrams.get(candidate + ' ' + after, 0)
                return math.log1p(left) + math.log1p(right), left, right
            original_score, _, _ = evidence(word)
            alternatives = [c for c in visual_candidates(word) if dictionary.words.get(c, 0) >= 10000]
            if strength in ('balanced', 'strong') and word not in dictionary.words:
                from symspellpy import Verbosity
                alternatives.extend(s.term for s in dictionary.lookup(word, Verbosity.CLOSEST, max_edit_distance=1 if strength == 'balanced' else 2)[:6])
            ranked = sorted(((evidence(c)[0] - visual_cost(word, c), c) for c in set(alternatives)), reverse=True)[:6]
            if not ranked:
                continue
            best_score, best = ranked[0]
            margin = best_score - max(original_score, ranked[1][0] if len(ranked) > 1 else 0)
            _, left, right = evidence(best)
            if margin < 5 or best == word or not (left > 1000 and right > 1000):
                continue
            # Real-word substitutions remain reviewable without a grammar rule.
            auto = (word not in dictionary.words and strength != 'conservative'
                    and left > 10000 and right > 10000 and margin >= 8)
            add(a, b, best, 'neighboring phrase frequencies and visual similarity', 0.94 if auto else 0.72, auto)

    # Review alternatives for legitimate words with inherently ambiguous meaning.
    for pattern, replacement in (
        (r"\bpond\s+in\s+(?:a|o)\s+(?P<t>cove)\b", 'cave'),
        (r"\bmachine\s+(?P<t>ports)\b", 'parts'),
    ):
        for match in re.finditer(pattern, text, re.IGNORECASE):
            a, b = match.span('t')
            if not any(a < p.end and b > p.start for p in proposals):
                add(a, b, replacement, 'both readings are valid words; check the original', 0.65, False)
    accepted = []
    for proposal in sorted(proposals, key=lambda p: (not p.automatic, -p.confidence, p.start)):
        if not any(proposal.start < p.end and proposal.end > p.start for p in accepted):
            accepted.append(proposal)
    return sorted(accepted, key=lambda p: p.start)
