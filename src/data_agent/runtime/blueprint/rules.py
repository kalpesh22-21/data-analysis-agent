"""blueprint/rules.py — D67 `resolve_via` rule expansion (runblueprint-design §3.4).

A `uses_rules` entry is either **static** (a fixed SQL boolean authored directly
into the template — the executor takes NO runtime action; it is just SQL) or
**resolved/dynamic** (`resolve_via: "resolveValues(FieldId, 'employee status')"`).
This module wires the dynamic case: it parses `resolve_via` into `(column,
concept)`, calls the typed `ResolveValuesComposite.resolve()` hook (the D67
programmatic entry point — NO model round-trip, D77 built it for exactly this),
and turns the returned client-scoped code set into a typed AST `IN`-list binding
that `template.bind_template` folds into the node SQL (never string-interpolated,
D10 — the same F1 boundary as slots).

Rule shape the executor accepts (a `uses_rules` list entry):

    {"id": "active_status",
     "resolve_via": "resolveValues(FieldId, 'employee status')",
     "table": "dbpcm_warehouse.paf",         # the table resolveValues probes
     "binds": "field_codes"}                  # the {field_codes} template placeholder

`binds` names the `{placeholder}` token the resolved IN-list fills; when absent it
is derived from a `predicate` field's single `{token}` (the `04-blueprints.md`
`predicate: "FieldId IN ({field_codes})"` shape). A malformed/static rule yields
`None` from `parse_rule` (the executor treats it as inert — no runtime expansion).

Pure parsing here; the async `expand_rules` does the `resolve()` calls + the
degrade/empty policy (§3.4 steps 3-4). Kept out of `executor.py` so the DAG walk
stays readable and this policy is unit-testable in isolation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

# `resolveValues(Column, 'concept')` / `resolveValues(Column, "concept")` — the
# authored `resolve_via` micro-syntax (D67). Whitespace-tolerant; the concept is
# a quoted string (single or double). Anything else → not a resolvable rule.
_RESOLVE_VIA = re.compile(
    r"""^\s*resolveValues\s*\(\s*
        ([A-Za-z_][A-Za-z0-9_]*)\s*,\s*      # column
        (['"])(.+?)\2                        # 'concept' | "concept"
        \s*\)\s*$""",
    re.VERBOSE,
)
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# A resolved-rule top-margin below which the fast path will NOT silently filter on
# the guessed client code set — it pauses to confirm (D66c/D85, §3.4 step 4).
_LOW_MARGIN = 0.1


@dataclass(frozen=True)
class ResolvedRule:
    """One dynamic `resolve_via` rule, parsed and ready to expand (§3.4)."""

    rule_id: str
    table: str
    column: str
    concept: str
    binds: str  # the {placeholder} the resolved IN-list fills


@dataclass(frozen=True)
class RuleBinding:
    """The expanded code set for one rule — a `{placeholder} → [codes]` binding
    the template binder renders as a typed AST `IN (…)` list (§3.4 step 3)."""

    binds: str
    values: list[str]
    provenance: frozenset[tuple[str, str]] | None


@dataclass(frozen=True)
class RuleFallback:
    """A degrade/low-margin, empty resolve, or an inner denial → fall back to the
    raw loop (§3.4: an empty resolved-rule set is NEVER a silently-dropped filter;
    a degraded resolve is NEVER silently filtered on a guessed code set).

    Honest-call (reviewer S2): a degraded/low-margin resolve DOWNGRADES to a
    raw-loop fallback rather than pausing to "confirm the code set" — there is no
    channel for the model to feed a confirmed set back into a re-invocation (it
    would just re-resolve → re-degrade → re-pause the same question, an
    unfulfillable UX). The raw loop is strictly better and honest until a real
    confirm channel exists. `error_code`/`user_message`/`retryable` carry an inner
    DENIAL through verbatim (§5.4); a degrade/empty carries `provenance` only."""

    rule_id: str
    reason: str  # "degraded" | "empty" | "denied"
    provenance: frozenset[tuple[str, str]] | None = None
    error_code: str | None = None
    user_message: str | None = None
    retryable: bool | None = None


RuleExpansion = RuleBinding | RuleFallback


class _ResolveHook(Protocol):
    """The D67 typed hook — `ResolveValuesComposite.resolve` (resolve_values.py)."""

    async def resolve(
        self,
        *,
        table: str,
        column: str,
        concept: str,
        period: Any,
        credentials: Any,
    ) -> Any: ...


def parse_rule(raw: Any) -> ResolvedRule | None:
    """Parse one `uses_rules` entry → a `ResolvedRule`, or `None` when the entry
    is static / malformed (no runtime expansion; the executor leaves it to the
    authored SQL). Pure."""
    if not isinstance(raw, dict):
        return None
    resolve_via = raw.get("resolve_via")
    if not isinstance(resolve_via, str):
        return None
    match = _RESOLVE_VIA.match(resolve_via)
    if match is None:
        return None
    column, _quote, concept = match.group(1), match.group(2), match.group(3)
    table = raw.get("table")
    if not isinstance(table, str) or not table:
        return None
    binds = raw.get("binds")
    if not isinstance(binds, str) or not binds:
        # Derive from a `predicate: "FieldId IN ({field_codes})"` single-token.
        predicate = raw.get("predicate")
        tokens = _PLACEHOLDER.findall(predicate) if isinstance(predicate, str) else []
        if len(tokens) != 1:
            return None  # ambiguous / no placeholder → cannot bind deterministically
        binds = tokens[0]
    rule_id = raw.get("id")
    return ResolvedRule(
        rule_id=rule_id if isinstance(rule_id, str) and rule_id else binds,
        table=table,
        column=column,
        concept=concept,
        binds=binds,
    )


async def expand_rule(
    rule: ResolvedRule,
    *,
    resolve_hook: _ResolveHook,
    credentials: Any,
    low_margin: float = _LOW_MARGIN,
) -> RuleExpansion:
    """Expand one dynamic rule via the D67 `resolve()` hook (§3.4). NO model
    round-trip — the typed programmatic entry point only. Applies the §3.4
    degrade/empty policy:

      - inner denial/error         -> `RuleFallback("denied")` (raw loop, denial verbatim)
      - `degraded` or low margin   -> `RuleFallback("degraded")` (raw loop, S2 honest-call)
      - empty value set            -> `RuleFallback("empty")` (never a dropped filter)
      - a clean set                -> `RuleBinding` (typed IN-list, D10)
    """
    outcome = await resolve_hook.resolve(
        table=rule.table,
        column=rule.column,
        concept=rule.concept,
        period=None,
        credentials=credentials,
    )
    if getattr(outcome, "status", "ok") != "ok":
        # Inner denial/error passes through VERBATIM (S6c/§5.4) — raw-loop
        # fallback; the model sees the REAL denial code, not a generic UNSUPPORTED.
        return RuleFallback(
            rule_id=rule.rule_id,
            reason="denied",
            provenance=getattr(outcome, "provenance", None),
            error_code=getattr(outcome, "error_code", None),
            user_message=getattr(outcome, "user_message", None),
            retryable=getattr(outcome, "retryable", None),
        )
    values = list(getattr(outcome, "values", []) or [])
    if getattr(outcome, "degraded", False) or _low_margin(outcome, low_margin):
        # Honest-call S2: do not silently filter on a guessed code set, and do not
        # pause to a confirm the model cannot answer — downgrade to the raw loop.
        return RuleFallback(
            rule_id=rule.rule_id,
            reason="degraded",
            provenance=getattr(outcome, "provenance", None),
        )
    if not values:
        # Empty → the rule matches nothing; per §3.4 fall back to the raw loop
        # rather than silently drop the filter (which would over-return).
        return RuleFallback(
            rule_id=rule.rule_id,
            reason="empty",
            provenance=getattr(outcome, "provenance", None),
        )
    return RuleBinding(
        binds=rule.binds,
        values=[v.value for v in values],
        provenance=getattr(outcome, "provenance", None),
    )


def _low_margin(outcome: Any, low_margin: float) -> bool:
    """True iff the resolve is ambiguous by top-margin — a small gap between the
    #1 and #2 code means the fast path must confirm rather than guess (§3.4)."""
    margin = getattr(outcome, "top_margin", None)
    values = getattr(outcome, "values", []) or []
    # A single unambiguous value (no margin to compute) is NOT low-margin.
    if len(values) < 2 or margin is None:
        return False
    return margin < low_margin


__all__ = [
    "ResolvedRule",
    "RuleBinding",
    "RuleExpansion",
    "RuleFallback",
    "expand_rule",
    "parse_rule",
]
