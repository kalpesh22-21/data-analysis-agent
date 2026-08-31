"""Rebuild the accepted SQL from a candidate — the INVERSE of `rewrite_sql_to_template`.

WHY THIS CAN EXIST AT ALL. The rewrite is information-preserving in one direction: a
`role="slot"` literal becomes `{name}` and its value is kept on the entry's `locator`, while
`inline` and `rule` literals stay in the template verbatim. So a candidate carries everything
needed to put the query back together — the template plus one value per slot.

WHY IT IS WANTED. `ValidationSnapshot` used to be written only at DECLINE time, so every
candidate extracted before `build_candidate_envelope` began stamping one has no accepted SQL
and cannot be re-validated — which is what the §C reviser needs. The obvious repair is to reload
the session and rebuild the summary, but sessions expire: measured on the live stack,
10 of 12 sampled sessions were already gone (TTL 168h), while reconstruction reached 23 of 27
candidates from the candidate alone and does not depend on retention at all.

⚠ THE RESULT IS MARKED `reconstructed=True` AND THAT MATTERS. Re-validating a candidate against
SQL derived FROM ITS OWN ENTRIES is circular: every literal in the reconstruction necessarily
has an entry, so the D97 totality walk cannot fail the way it is designed to. Had the extractor
dropped a predicate, the reconstruction would drop it too and the walk would pass anyway.

That is acceptable ONLY for the migration case, and for one specific reason: these candidates
already passed the genuine walk at extraction time, against the real SQL. The reconstruction
records the query as it stood when that happened. Every SUBSEQUENT revision is then checked
properly — remove an entry and the stored SQL still carries that literal, so the walk catches
it. The vacuous check is the redundant first one.

NEVER USE THIS FOR A FRESH CANDIDATE. `build_candidate_envelope` stamps the real snapshot from
the live summary; a reconstruction there would trade a true record for a derived one.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import sqlglot

from data_agent.runtime.blueprint.template import (
    slot_tokens_outside_strings,
    sub_slot_tokens,
)

_logger = logging.getLogger(__name__)

# A value that can stand as a bare SQL number. Anchored and sign-aware; deliberately NOT a
# `float()` call, which accepts "nan"/"inf" and would emit an identifier where a literal
# belongs.
_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")

# Slot types whose bind site is a bare number rather than a quoted literal. Mirrors the reasoning
# in `promotion/replay.py::_sample_value`: the UNIT lives in the template (`INTERVAL {n} MONTH`),
# so the site holds a number.
_BARE_NUMBER_TYPES = frozenset({"relative_window"})

# Slot types that occupy a SET position (`IN {slot}`), so the literal is a tuple.
_LIST_TYPES = frozenset({"list"})


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _render(value: Any, slot_type: str) -> str:
    """One slot's stored literal, rendered back into SQL text.

    Type-driven, because a single quoting rule is wrong in both directions: quoting everything
    turns `IN {depts}` into a comparison against one comma-joined string, and quoting nothing
    turns a department code into a bare identifier that does not resolve.
    """
    text = "" if value is None else str(value)
    if slot_type in _LIST_TYPES:
        # The stored value is the set as it appeared, comma-joined. Rebuilt as a tuple so the
        # `IN` site parses; a single member is still a valid one-element tuple.
        members = [m.strip() for m in text.split(",") if m.strip()] or [text]
        return "(" + ", ".join(_quote(m) for m in members) + ")"
    if slot_type in _BARE_NUMBER_TYPES and _NUMERIC.match(text):
        return text
    return _quote(text)


def _slot_values(payload: dict[str, Any]) -> dict[str, tuple[Any, str]]:
    """Every slot token → (its stored literal, its declared type)."""
    out: dict[str, tuple[Any, str]] = {}
    entries = payload.get("parameterization")
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        slot = entry.get("slot")
        if not isinstance(slot, dict):
            continue
        name = slot.get("name")
        if not isinstance(name, str) or not name:
            continue
        locator = entry.get("locator") if isinstance(entry.get("locator"), dict) else {}
        slot_type = slot.get("type")
        out[name] = (locator.get("value"), slot_type if isinstance(slot_type, str) else "")
    return out


def _substitute(template: str, values: dict[str, tuple[Any, str]], *, bare: bool) -> str:
    """Fill every `{token}`. *bare* renders a numeric-looking value unquoted regardless of type.

    The second pass exists because the stored `locator.value` is "the literal as it APPEARED"
    with its quoting already stripped, so a predicate that was `toYear(d) = 2025` and one that
    was `code = '0420'` arrive identical. Nothing on the entry distinguishes them, so the
    reconstruction tries the quoted reading first and falls back — see `reconstruct_accepted_sql`.
    """

    def _one_named(name: str) -> str:
        value, slot_type = values.get(name, ("", ""))
        text = "" if value is None else str(value)
        if bare and slot_type not in _LIST_TYPES and _NUMERIC.match(text):
            return text
        return _render(value, slot_type)

    # STRING-AWARE. Substituting into a `{x}` that lives inside a string constant
    # produced `''VALUE''`, which does not parse — so the whole candidate came back
    # unrebuildable and the reviser could never act on it.
    return sub_slot_tokens(template, _one_named)


def reconstruct_accepted_sql(payload: dict[str, Any]) -> str | None:
    """The accepted SQL this candidate was generalized from, or `None` if it cannot be rebuilt.

    `None` — never a guess — for a payload with no single-statement template, a slot token with
    no stored value, or a substitution that will not parse. A snapshot built on SQL that does not
    parse would replace "cannot re-validate" with a decline blaming the reviewer.

    TWO SUBSTITUTIONS ARE TRIED, quoted then bare-numeric, and the parse decides. The stored
    value has already lost its quoting, so `2025` and `'0420'` are indistinguishable on the
    entry; guessing once and failing would give up on every numeric predicate.
    """
    generalization = payload.get("generalization")
    if not isinstance(generalization, dict):
        return None
    template = generalization.get("sql_template")
    if not isinstance(template, str) or not template.strip():
        return None

    values = _slot_values(payload)
    missing = [
        name
        for name in slot_tokens_outside_strings(template)
        if name not in values
    ]
    if missing:
        # A token with no entry means the plan and the template disagree — exactly what the
        # totality walk exists to catch. Refuse rather than invent a value for it.
        _logger.info(
            "reconstruct: template references slot(s) %s with no parameterization entry",
            sorted(missing),
        )
        return None

    for bare in (False, True):
        candidate = _substitute(template, values, bare=bare)
        try:
            parsed = sqlglot.parse_one(
                candidate, dialect="clickhouse", error_level=sqlglot.ErrorLevel.RAISE
            )
        except Exception:
            continue
        if parsed is not None:
            return candidate
    _logger.info("reconstruct: no substitution of the template parses as ClickHouse SQL")
    return None
