"""Correction / confirmation lexicons for `accepted_signal` inference (D99, §2.1).

Matching is WORD-BOUNDARY, not bare substring: "yesterday" must NOT hit "yes", and
confirmations additionally pass a NEGATION guard, so a confirmation phrase preceded by a
negator in the same clause is not a confirmation. The lexicons encode the D99 §2.3 asymmetry:
over-matching a CORRECTION is SAFE (it costs one un-learned blueprint, recoverable via
recurrence), while over-matching a CONFIRMATION manufactures a candidate off a challenged
answer. So corrections are broad and aggressive, and confirmations are guarded.
"""

from __future__ import annotations

import re
from functools import lru_cache

CORRECTION_PHRASES: frozenset[str] = frozenset(
    {
        # original set
        "no,",
        "actually",
        "i meant",
        "that's wrong",
        "that is wrong",
        "not",
        "should be",
        "incorrect",
        "wrong",
        "instead",
        # HIGH-3 broadening: a challenged answer must never read as accepted.
        "can't be right",
        "cannot be right",
        "isn't right",
        "is not right",
        "doesn't look right",
        "does not look right",
        "doesn't seem right",
        "seems off",
        "seems wrong",
        "too high",
        "too low",
        "off by",
        "try again",
        "redo",
        "that's not",
        "that is not",
        "don't think",
        "do not think",
        "not quite",
        "nope",
    }
)

CONFIRMATION_PHRASES: frozenset[str] = frozenset(
    {
        "yes",
        "correct",
        "perfect",
        "exactly",
        "that's right",
        "that is right",
        "thanks, that",
        "looks right",
        "great, thanks",
    }
)

# Negators that void a following confirmation within the same clause (HIGH-2).
_NEGATORS: frozenset[str] = frozenset(
    {
        "no",
        "not",
        "never",
        "hardly",
        "don't",
        "dont",
        "doesn't",
        "doesnt",
        "isn't",
        "isnt",
        "aren't",
        "arent",
        "wasn't",
        "wasnt",
        "can't",
        "cant",
        "cannot",
        "won't",
        "wont",
    }
)

# Clause delimiters — a negator only voids a confirmation in the SAME clause, so a
# preceding correction clause ("that's wrong, but this looks right") does not
# suppress a genuinely-un-negated confirmation clause.
_CLAUSE_DELIMS: tuple[str, ...] = (",", ";", " but ", " however ", " though ")


def normalize(text: str) -> str:
    """Lowercase + collapse internal whitespace + strip — the canonical form both
    lexicons are matched against."""
    return " ".join(text.lower().split())


@lru_cache(maxsize=512)
def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Compile a word-boundary regex for *phrase*: `\\b` is applied only at edges
    that are word characters (so `"no,"` → `\\bno,` and `"yes"` → `\\byes\\b`)."""
    escaped = re.escape(phrase)
    prefix = r"\b" if (phrase[:1].isalnum() or phrase[:1] == "_") else ""
    suffix = r"\b" if (phrase[-1:].isalnum() or phrase[-1:] == "_") else ""
    return re.compile(prefix + escaped + suffix)


def matches_any(text: str, phrases: frozenset[str]) -> bool:
    """True iff the normalized *text* contains any phrase as a WORD-BOUNDARY match.

    Used for CORRECTION matching, with no negation guard: a negated correction is still safely
    treated as a correction (§2.3).
    """
    normalized = normalize(text)
    return any(_phrase_pattern(phrase).search(normalized) for phrase in phrases)


def _negated_before(normalized: str, index: int) -> bool:
    """True iff a negator word appears in the clause immediately preceding
    *index* (up to the nearest clause delimiter)."""
    clause_start = 0
    for delim in _CLAUSE_DELIMS:
        pos = normalized.rfind(delim, 0, index)
        if pos != -1:
            clause_start = max(clause_start, pos + len(delim))
    segment = normalized[clause_start:index]
    return any(_phrase_pattern(neg).search(segment) for neg in _NEGATORS)


def matches_confirmation(text: str) -> bool:
    """True iff *text* contains an UN-NEGATED confirmation phrase.

    Word-boundary plus the negation guard, so a confirmation preceded by a negator in the same
    clause does not count.
    """
    normalized = normalize(text)
    for phrase in CONFIRMATION_PHRASES:
        for match in _phrase_pattern(phrase).finditer(normalized):
            if not _negated_before(normalized, match.start()):
                return True
    return False
