"""Deterministic regex / NER entity detectors for the S5 leakage gate (D58/D17).

The first, cheap layer of the gate: a fixed battery of anchored regexes that flag
the entity families a GLOBAL (entity-free) candidate must never carry — employee
codes, department codes, person names, dates/years, and known region tokens. It
is deliberately over-eager on the hard-entity families (a false positive routes to
human review, which is safe) but never scans the entity-BEARING targets
(`user_knowledge`); the gate decides applicability, not this module.

No I/O, no model call — a pure `str -> tuple[EntityHit, ...]` function so the same
detectors run identically in the consumer, in tests, and (later) in an offline
audit sweep. The injected LLM semantic scan (see `scanner.py`) is the second layer
that catches what these patterns cannot.
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
    # Known region tokens as whole words.
    (
        "region",
        re.compile(r"\b(?:EMEA|APAC|LATAM|NAWEST|NAEAST|NA|US|EU)\b"),
    ),
)


def scan_text(field: str, text: str) -> tuple[EntityHit, ...]:
    """Return every regex/NER entity hit in *text*, tagged with *field* (the
    payload location it came from — audit trail). Deterministic order: detectors
    in declaration order, then match position within each detector."""
    hits: list[EntityHit] = []
    for kind, pattern in _DETECTORS:
        for match in pattern.finditer(text):
            hits.append(EntityHit(field=field, kind=kind, span=match.group(0)))
    return tuple(hits)


def scan_fields(text_by_field: dict[str, str]) -> tuple[EntityHit, ...]:
    """Run `scan_text` over every (field, text) pair, preserving field order."""
    hits: list[EntityHit] = []
    for field, text in text_by_field.items():
        hits.extend(scan_text(field, text))
    return tuple(hits)
