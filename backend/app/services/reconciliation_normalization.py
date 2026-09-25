"""
Deterministic normalization for Stage 5C matching (SECTIONS 13/14/33/34).

No AI, no fuzzy/embedding similarity, no network calls - every function
here is a pure, reproducible string transform. Conservative by design
(SECTION 13): normalization never merges two references that a human
reviewer would consider genuinely different (e.g. "ABC-123" is NOT
treated as equal to "ABC123" - only whitespace/case are normalized,
never separator characters, since removing them could falsely conflate
distinct references).
"""
import re

_WHITESPACE_RE = re.compile(r"\s+")
_NARRATION_PUNCTUATION_RE = re.compile(r"[.,;:!?'\"()\[\]{}]")


def normalize_reference(value: str | None) -> str:
    """
    SECTION 13: uppercase + trimmed + internal whitespace collapsed to a
    single space. Deliberately does NOT strip hyphens, slashes, or other
    separators - "ABC-123" and "ABC123" are only ever treated as
    equivalent if they are byte-identical after this normalization,
    which they are not.
    """
    if not value:
        return ""
    return _WHITESPACE_RE.sub(" ", value.strip()).upper()


def normalize_narration(value: str | None) -> str:
    """
    SECTION 14: lowercase + trimmed + collapsed whitespace + a small,
    explicit set of clearly-cosmetic punctuation removed. Deliberately
    conservative - this is not a general-purpose text cleaner; it exists
    only to let two narrations that differ solely in punctuation/casing/
    spacing be compared as equivalent, never to merge narrations that
    differ in actual wording.
    """
    if not value:
        return ""
    text = value.strip().lower()
    text = _NARRATION_PUNCTUATION_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def tokenize_narration(normalized_value: str) -> frozenset:
    """Splits an already-normalized narration into a token set for deterministic overlap comparison."""
    if not normalized_value:
        return frozenset()
    return frozenset(t for t in normalized_value.split(" ") if t)
