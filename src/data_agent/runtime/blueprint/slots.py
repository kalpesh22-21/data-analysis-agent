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

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .models import SlotSpec

# Deictic / relative period words — never resolved to a concrete period here
# (D49: fuzzy NL is never guessed); they route to `AskUser` (or, in Slice B, to a
# latest-settled-period probe). Kept lowercase for case-insensitive matching.
_DEICTIC_PERIOD_TOKENS: frozenset[str] = frozenset(
    {"latest", "current", "last", "recent", "this", "next", "previous", "prior"}
)


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


__all__ = ["AskUser", "OmitSlot", "SlotBinding", "SlotResolution", "resolve_slot"]
