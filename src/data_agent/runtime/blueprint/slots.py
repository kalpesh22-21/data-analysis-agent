"""blueprint/slots.py — the D49 deterministic per-type slot resolvers (§3.2).

The hand-off is fixed (04-blueprints §Slot filling): the MODEL proposes raw
values; the RUNTIME validates, normalizes, and binds them. These resolvers are
**pure code — NO LLM runs inside `runBlueprint`** (D49, the hard invariant). A
genuinely ambiguous / multi-match / no-match / fuzzy value is NEVER guessed — it
returns an `AskUser` signal the executor turns into a pause.

Slice-A posture (§Q7): resolvers are pure and Layer-1-exhaustive. Where a resolver
needs the warehouse domain (an entity-existence check, the valid period-key set),
the domain is PASSED IN (`domain=`) — Slice B supplies it from a scope-enforced
`runQuery` probe. `domain=None` means "no domain available" → the resolver binds
the normalized value without an existence check (the probe is the executor's job),
never fabricating a match. This keeps the resolvers pure + deterministic here and
lets the executor wire the real probe later without changing this contract.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .models import RELATIVE_WINDOW_CEILING, RELATIVE_WINDOW_FLOOR, SlotSpec

# Deictic / relative period words — never resolved to a concrete period here
# (D49: fuzzy NL is never guessed); they route to `AskUser` (or, in Slice B, to a
# latest-settled-period probe). Kept lowercase for case-insensitive matching.
_DEICTIC_PERIOD_TOKENS: frozenset[str] = frozenset(
    {"latest", "current", "last", "recent", "this", "next", "previous", "prior"}
)

# `relative_window` safety bounds (D49) — the shared floor/ceiling from `models`.
# `n` binds as an `INTERVAL {n} <unit>` literal; the ceiling is a HARD cap so an
# absurd `INTERVAL 999999 MONTH` can neither be authored nor bound. A slot's
# `min_value`/`max_value` may narrow the window but never widen past the ceiling
# (H1a: the resolver clamps `hi` to it even if a poisoned/legacy spec declares more).
_RELATIVE_WINDOW_MIN = RELATIVE_WINDOW_FLOOR
_RELATIVE_WINDOW_MAX = RELATIVE_WINDOW_CEILING
# A `period_range` bound — an ISO calendar date, optionally with a `T`-time. Bound
# as a TYPED string literal (F1/D10), never date-arithmeticed here (resolver-pure,
# consistent with `_resolve_period`). `\A…\Z`-anchored (L1) so a trailing newline
# can never slip past the fence; calendar validity is checked separately (L2).
_ISO_DATE = re.compile(r"\A\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2})?\Z")


@dataclass(frozen=True)
class PeriodRange:
    """An explicit `{start, end}` window for a `period_range` slot. Both bounds are
    ISO date/datetime strings; the executor expands them to `{name}_start`/
    `{name}_end` bind sites via `expand_binding` (each a TYPED literal, F1/D10)."""

    start: str
    end: str


@dataclass(frozen=True)
class SlotBinding:
    """A resolved slot value, ready for F1 typed-literal binding (`template.py`).

    `value` is a `str | int | float | bool | list` — the RAW resolved value; the
    template binder gives it its SQL type. `resolved_from` records the fill path
    for observability (never the value itself in spans, D25)."""

    name: str
    value: Any
    resolved_from: str = "direct"  # "direct" | "domain" | "enum" | "list"


@dataclass(frozen=True)
class OmitSlot:
    """An absent OPTIONAL slot — no binding; the node's `optional_pattern`
    applies at template-assembly time (presence rule 1, 04-blueprints)."""

    name: str


@dataclass(frozen=True)
class AskUser:
    """The pause signal — a slot that cannot be resolved deterministically (D49).

    `reason` is a stable machine tag; `question`/`options` are the model-facing
    clarification. NEVER carries an LLM call — this is the whole point (§3.2)."""

    slot: str
    reason: str  # "missing" | "no_match" | "multi_match" | "fuzzy" | "invalid"
    question: str
    options: list[str] = field(default_factory=list)


SlotResolution = SlotBinding | OmitSlot | AskUser


def _normalize(raw: Any) -> Any:
    """Trim surrounding whitespace on strings; pass non-strings through."""
    return raw.strip() if isinstance(raw, str) else raw


def _is_valid_iso_calendar(value: str) -> bool:
    """True iff *value* (already ISO-SHAPE-matched) is a REAL calendar date/datetime
    (L2). `fromisoformat` rejects impossible dates like `2026-13-99` / `2026-02-30`.
    Pure validation — no arithmetic, no wall-clock read (D49 resolver-purity)."""
    try:
        if "T" in value:
            _dt.datetime.fromisoformat(value)
        else:
            _dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_absent(raw: Any) -> bool:
    return raw is None or (isinstance(raw, str) and raw.strip() == "")


def _domain_matches(value: str, domain: Iterable[str]) -> list[str]:
    """Exact-then-case-insensitive membership. An exact hit wins alone; otherwise
    every case-insensitive match is returned so a multi-match → `AskUser`."""
    domain_list = list(domain)
    if value in domain_list:
        return [value]
    lowered = value.casefold()
    return [d for d in domain_list if d.casefold() == lowered]


def resolve_slot(
    raw: Any,
    spec: SlotSpec,
    *,
    domain: Iterable[str] | None = None,
) -> SlotResolution:
    """Resolve one model-proposed *raw* value against its `SlotSpec` (§3.2).

    Returns exactly one of `SlotBinding` (bound), `OmitSlot` (absent optional), or
    `AskUser` (deterministic resolution impossible → pause, never a guess).
    """
    # 1. Presence (04-blueprints rule 1).
    if _is_absent(raw):
        if spec.required:
            return AskUser(
                slot=spec.name,
                reason="missing",
                question=f"What value should I use for '{spec.name}'?",
            )
        return OmitSlot(name=spec.name)

    # The windowed-period types resolve BEFORE the string/entity container guard
    # (line below): a `period_range` value is legitimately a dict/list, which that
    # guard would otherwise reject as "not a single value" (F1 windowed-slot gap).
    if spec.type == "relative_window":
        return _resolve_relative_window(raw, spec)
    if spec.type == "period_range":
        return _resolve_period_range(raw, spec)

    if spec.type in ("string", "entity"):
        # A CONTAINER (list/dict/tuple) handed to a scalar slot is not a value —
        # stringifying it would bind a Python repr like "[1, 2]" (review FIX 4a).
        # Ask rather than fabricate. (List ELEMENTS resolved via `_resolve_list`
        # keep their coercion — a different, list-typed path.)
        if isinstance(raw, (list, tuple, dict)):
            return AskUser(
                slot=spec.name,
                reason="invalid",
                question=f"'{spec.name}' expects a single value, not a list or object.",
            )
        return _resolve_scalar(raw, spec, domain, resolved_from="domain")
    if spec.type == "enum":
        return _resolve_enum(raw, spec)
    if spec.type in ("period", "as_of_date"):
        return _resolve_period(raw, spec, domain)
    if spec.type == "list":
        return _resolve_list(raw, spec, domain)
    # `models.SLOT_TYPES` is the closed set the parse layer enforces, so this is
    # unreachable for a parsed spec — fail-closed rather than bind blindly.
    return AskUser(
        slot=spec.name,
        reason="invalid",
        question=f"I can't resolve the '{spec.name}' value automatically.",
    )


def _resolve_scalar(
    raw: Any, spec: SlotSpec, domain: Iterable[str] | None, *, resolved_from: str
) -> SlotResolution:
    value = _normalize(raw)
    if not isinstance(value, str):
        # A string/entity slot handed a non-string (number/bool/list) — coerce to
        # its string form; the template binder still binds it as a typed literal.
        return SlotBinding(name=spec.name, value=str(value), resolved_from="direct")
    if domain is None:
        # No domain available (Slice-A pure path / no probe) → bind the normalized
        # value directly. The executor's scope-enforced query still gates it.
        return SlotBinding(name=spec.name, value=value, resolved_from="direct")
    matches = _domain_matches(value, domain)
    if len(matches) == 1:
        return SlotBinding(name=spec.name, value=matches[0], resolved_from=resolved_from)
    if not matches:
        return AskUser(
            slot=spec.name,
            reason="no_match",
            question=f"I couldn't find '{value}' for '{spec.name}'. Which did you mean?",
            options=sorted(domain),
        )
    return AskUser(
        slot=spec.name,
        reason="multi_match",
        question=f"'{value}' matches several values for '{spec.name}'. Which did you mean?",
        options=sorted(matches),
    )


def _resolve_enum(raw: Any, spec: SlotSpec) -> SlotResolution:
    value = _normalize(raw)
    allowed = spec.enum_values or ()
    if isinstance(value, str) and value in allowed:
        return SlotBinding(name=spec.name, value=value, resolved_from="enum")
    # Case-insensitive recovery — a single tolerant match binds; ambiguity asks.
    if isinstance(value, str):
        ci = [v for v in allowed if v.casefold() == value.casefold()]
        if len(ci) == 1:
            return SlotBinding(name=spec.name, value=ci[0], resolved_from="enum")
    return AskUser(
        slot=spec.name,
        reason="no_match",
        question=f"'{raw}' is not a valid value for '{spec.name}'. Choose one:",
        options=list(allowed),
    )


def _resolve_period(raw: Any, spec: SlotSpec, domain: Iterable[str] | None) -> SlotResolution:
    """Phase-1 temporal resolution (§3.2): explicit-named + `askUser` on fuzzy.

    A period value is a WAREHOUSE DOMAIN ENTITY, not a calendar date (04-blueprints
    §Temporal). Explicit-named maps NL → a valid period key in *domain*; a
    deictic/relative token ("latest"/"last period") is NEVER guessed here — it
    routes to `AskUser` (Slice B resolves latest-settled via a probe). Multi-match
    (e.g. bi-weekly "May") and no-match both → `AskUser` (a single `period` slot
    pins one period). Period-range/period-dimension resolution is a carried OQ.
    """
    value = _normalize(raw)
    if isinstance(value, str) and value.casefold() in _DEICTIC_PERIOD_TOKENS:
        return AskUser(
            slot=spec.name,
            reason="fuzzy",
            question=f"Which specific period do you mean for '{spec.name}'?",
            options=sorted(domain) if domain is not None else [],
        )
    if domain is None:
        # No period domain available in this (pure) path — cannot map NL → a valid
        # key, and we never parse with `to_date`. Ask rather than guess (D49).
        return AskUser(
            slot=spec.name,
            reason="fuzzy",
            question=f"Which specific period do you mean for '{spec.name}'?",
        )
    if not isinstance(value, str):
        value = str(value)
    matches = _domain_matches(value, domain)
    if len(matches) == 1:
        return SlotBinding(name=spec.name, value=matches[0], resolved_from="domain")
    if not matches:
        return AskUser(
            slot=spec.name,
            reason="no_match",
            question=f"I couldn't find the period '{value}' for '{spec.name}'.",
            options=sorted(domain),
        )
    return AskUser(
        slot=spec.name,
        reason="multi_match",
        question=f"'{value}' maps to several periods for '{spec.name}'. Which one?",
        options=sorted(matches),
    )


def _resolve_list(raw: Any, spec: SlotSpec, domain: Iterable[str] | None) -> SlotResolution:
    """A `list`/`IN` slot — each element resolved like a scalar; binds as a list
    (the template binder renders it as a sqlglot `IN (…)` tuple). Any element that
    multi-/no-matches → `AskUser` (never a partial IN set on a guess)."""
    elements = raw if isinstance(raw, (list, tuple)) else [raw]
    if not elements:
        return AskUser(
            slot=spec.name,
            reason="no_match",
            question=f"'{spec.name}' needs at least one value.",
        )
    resolved: list[Any] = []
    for element in elements:
        outcome = _resolve_scalar(element, spec, domain, resolved_from="list")
        if isinstance(outcome, AskUser):
            return outcome  # surface the first ambiguous element's clarification
        if isinstance(outcome, SlotBinding):
            resolved.append(outcome.value)
    return SlotBinding(name=spec.name, value=resolved, resolved_from="list")


def _resolve_relative_window(raw: Any, spec: SlotSpec) -> SlotResolution:
    """A trailing "last N <unit>" window (§3.2, the windowed-period gap): resolve
    the raw value to a BOUNDED integer `n`, bound as an `INTERVAL {n} <unit>` number
    literal (F1/D10 — never interpolated). Pure code, no LLM (D49).

    Accepts ONLY an `int` or a PURE-DIGIT string (whole string is digits after a
    strip). ANY trailing non-digit text — "6 months", "6 weeks", "6; DROP" — is
    REJECTED (H2): the unit lives in the TEMPLATE (`INTERVAL {n} MONTH`), so taking
    a leading integer and dropping the trailing "weeks" would silently bind a WEEK
    count as MONTHS (a wrong-answer unit mismatch). A float, non-numeric, or any
    trailing text → `AskUser`. `n` must fall in `[lo, hi]` where `lo = spec.min_value
    or 1`, `hi = min(spec.max_value or 120, 120)` — the ceiling is a HARD cap the
    resolver clamps to even if a spec declares more (H1a); `n >= 1` always."""
    value = _normalize(raw)
    if isinstance(value, bool):
        # A bool is an int subclass — never a window count. Ask rather than bind 1/0.
        return AskUser(
            slot=spec.name,
            reason="invalid",
            question=f"'{spec.name}' expects a whole number of periods (e.g. 6).",
        )
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and value.isdigit():
        # PURE digits only (H2). `str.isdigit()` is True iff every char is a decimal
        # digit and the string is non-empty — so "6 months"/"6.5"/"6; DROP"/"" all
        # fall through to the AskUser below (no leading-integer extraction).
        n = int(value)
    else:
        # A float, a non-numeric string, or a phrase with a trailing unit/text — the
        # unit is the template's job; a plain number of the template's own unit only.
        return AskUser(
            slot=spec.name,
            reason="invalid",
            question=f"'{spec.name}' expects a plain whole number of periods (e.g. 6), no unit.",
        )
    lo = spec.min_value if spec.min_value is not None else _RELATIVE_WINDOW_MIN
    hi = spec.max_value if spec.max_value is not None else _RELATIVE_WINDOW_MAX
    lo = max(lo, _RELATIVE_WINDOW_MIN)  # n >= 1 always (a 0/negative window is no filter)
    hi = min(hi, _RELATIVE_WINDOW_MAX)  # H1a: the ceiling is HARD even if max_value > it
    if not (lo <= n <= hi):
        return AskUser(
            slot=spec.name,
            reason="invalid",
            question=f"'{spec.name}' must be a whole number between {lo} and {hi}.",
        )
    # An int → the existing number-literal bind path (`exp.Literal.number`).
    return SlotBinding(name=spec.name, value=n, resolved_from="direct")


def _resolve_period_range(raw: Any, spec: SlotSpec) -> SlotResolution:
    """An explicit `{start, end}` window (§3.2, the windowed-period gap). Resolves
    to a `PeriodRange`; the executor expands it to `{name}_start`/`{name}_end` TYPED
    string literals (F1/D10). Pure — NO relative→concrete date arithmetic (a deictic
    word asks, consistent with `_resolve_period`).

    Accepts a dict with string `start`/`end` keys, or a 2-element `[start, end]`
    list/tuple. Both bounds must match a strict ISO date/datetime shape and
    `start < end` (a same-shape ISO string compare is valid). A non-dict/list, a
    missing key, a malformed/deictic bound, or mismatched shapes → `AskUser`."""
    if isinstance(raw, dict):
        start = raw.get("start")
        end = raw.get("end")
    elif isinstance(raw, (list, tuple)) and len(raw) == 2:
        start, end = raw[0], raw[1]
    else:
        return AskUser(
            slot=spec.name,
            reason="invalid",
            question=(
                f"'{spec.name}' needs explicit start and end dates "
                "(e.g. {\"start\": \"2026-01-01\", \"end\": \"2026-03-31\"})."
            ),
        )
    start = _normalize(start)
    end = _normalize(end)
    if not isinstance(start, str) or not isinstance(end, str):
        return AskUser(
            slot=spec.name,
            reason="invalid",
            question=f"'{spec.name}' needs explicit start and end dates as ISO strings.",
        )
    if not _ISO_DATE.match(start) or not _ISO_DATE.match(end):
        # Deictic/relative words ("last month") or a malformed date — never guessed.
        return AskUser(
            slot=spec.name,
            reason="fuzzy",
            question=(
                f"'{spec.name}' needs explicit start and end dates in YYYY-MM-DD form "
                "(e.g. 2026-01-01), not relative words."
            ),
        )
    # L2: the regex proves the SHAPE, not calendar validity — "2026-13-99" matches
    # `\d{4}-\d{2}-\d{2}` but is not a real date. `fromisoformat` parses (and REJECTS)
    # it; this is VALIDATION, not date arithmetic, so the resolver stays pure (no
    # relative→concrete math). A ValueError → ask rather than bind an impossible date.
    if not _is_valid_iso_calendar(start) or not _is_valid_iso_calendar(end):
        return AskUser(
            slot=spec.name,
            reason="invalid",
            question=f"'{spec.name}' start/end must be real calendar dates (e.g. 2026-01-31).",
        )
    # A string compare is a valid ordering ONLY for same-shaped ISO strings; if one
    # bound carries a `T`-time and the other does not, the compare is unsound → ask.
    if ("T" in start) != ("T" in end):
        return AskUser(
            slot=spec.name,
            reason="invalid",
            question=f"'{spec.name}' start and end must be the same date shape (both date, or both datetime).",
        )
    if not (start < end):
        return AskUser(
            slot=spec.name,
            reason="invalid",
            question=f"'{spec.name}' start date must be before its end date.",
        )
    return SlotBinding(name=spec.name, value=PeriodRange(start, end), resolved_from="direct")


def slot_token_names(spec: SlotSpec) -> set[str]:
    """The set of `{token}` names *spec* may legally bind in a template — the
    ANTI-DRIFT rule every downstream seam (executor bind sites, corpus-loader gates)
    calls instead of reimplementing. A `period_range` occupies TWO tokens
    (`{name}_start`, `{name}_end`); every other type occupies one (`{name}`)."""
    if spec.type == "period_range":
        return {f"{spec.name}_start", f"{spec.name}_end"}
    return {spec.name}


def expand_binding(spec: SlotSpec, value: Any) -> dict[str, Any]:
    """Expand a resolved slot *value* into its `{token}: value` bind map — the
    companion to `slot_token_names`. A `period_range` (value is a `PeriodRange`)
    expands to both bounds as separate string bindings; every other type binds its
    single value under `{name}`. The executor calls this so a `PeriodRange` NEVER
    reaches `bind_template` (which only knows str/number/bool/list literals)."""
    if spec.type == "period_range" and isinstance(value, PeriodRange):
        return {f"{spec.name}_start": value.start, f"{spec.name}_end": value.end}
    return {spec.name: value}


__all__ = [
    "AskUser",
    "OmitSlot",
    "PeriodRange",
    "SlotBinding",
    "SlotResolution",
    "expand_binding",
    "resolve_slot",
    "slot_token_names",
]
