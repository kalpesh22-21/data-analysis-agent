"""Deterministic regex / NER entity detectors for the S5 leakage gate (D58/D17).

The first, cheap layer of the gate: a fixed battery of anchored regexes flagging the entity
families a GLOBAL candidate must never carry. Deliberately over-eager on the hard-entity
families, because a false positive routes to human review, which is safe. No I/O and no model
call — a pure `str -> tuple[EntityHit, ...]` so the same detectors run identically everywhere;
the injected semantic scan is the second layer that catches what patterns cannot.
"""

from __future__ import annotations

import re

from ..candidate.verdicts import EntityHit

# Each detector is (kind, compiled-pattern). Order is fixed so the emitted hits
# are deterministic across runs (the audit trail must be stable). Patterns are
# anchored on word boundaries so a substring inside a longer token never trips
# them (e.g. "generation" must not match a region "NA").
_DETECTORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Employee / person codes: a leading letter + >= 4 digits (E12345, P0007).
    ("employee_code", re.compile(r"\b[A-Za-z]\d{4,}\b")),
    # Department codes: a leading-zero 3-4 digit code (0420, 007) — distinct from
    # a bare year so "2025" is classified as a date below, not a dept code.
    ("dept_code", re.compile(r"\b0\d{2,3}\b")),
    # Dates / years: an ISO date or a bare 19xx/20xx year.
    ("date", re.compile(r"\b(?:19|20)\d{2}(?:-\d{2}-\d{2})?\b")),
    # Person names: two Capitalized words in a row (Jane Doe). Conservative — a
    # sentence-initial "Total Earnings" can trip it, but a false positive on a
    # GLOBAL candidate routes to human review, never a silent leak.
    ("person", re.compile(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b")),
    # Multi-character region tokens — case-INSENSITIVE (QA-Q4). These are
    # unambiguous region names, so case-folding them ('emea' → hit) carries no
    # false-positive risk while closing the lowercase-region gap.
    (
        "region",
        re.compile(r"\b(?:EMEA|APAC|LATAM|NAWEST|NAEAST)\b", re.IGNORECASE),
    ),
    # Short region tokens — case-SENSITIVE on purpose (QA-Q4 note): 'na'/'us'/'eu'
    # are common English fragments (a lowercase-folded 'us'/'eu' would false-positive
    # on ordinary prose), so only their upper-case region form is flagged.
    (
        "region",
        re.compile(r"\b(?:NA|US|EU)\b"),
    ),
)

# Title-cased business labels are common in catalog enums and metric names. The
# deliberately broad two-word detector cannot distinguish them from names, so
# suppress matches containing a domain word. Real names remain covered by this
# regex, with the semantic scanner as the backstop for unusual spellings.
_NON_PERSON_WORDS = frozenset(
    {
        "active", "annual", "earnings", "employee", "hired", "inactive",
        "leave", "not", "on", "retired", "salary", "status", "terminated",
        "total",
    }
)


def _known_business_label(kind: str, span: str) -> bool:
    return kind == "person" and any(
        word.casefold() in _NON_PERSON_WORDS for word in span.split()
    )


def scan_text(field: str, text: str) -> tuple[EntityHit, ...]:
    """Every regex/NER entity hit in *text*, tagged with *field* (its payload location).

    Deterministic order: detectors in declaration order, then match position within each.
    """
    hits: list[EntityHit] = []
    for kind, pattern in _DETECTORS:
        for match in pattern.finditer(text):
            span = match.group(0)
            if _known_business_label(kind, span):
                continue
            hits.append(EntityHit(field=field, kind=kind, span=span))
    return tuple(hits)


def scan_fields(text_by_field: dict[str, str]) -> tuple[EntityHit, ...]:
    """Run `scan_text` over every (field, text) pair, preserving field order."""
    hits: list[EntityHit] = []
    for field, text in text_by_field.items():
        hits.extend(scan_text(field, text))
    return tuple(hits)
