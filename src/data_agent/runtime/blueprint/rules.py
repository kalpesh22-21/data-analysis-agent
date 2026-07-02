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

# D67 concept-subset selection (locked decision: margin/gap-cut + low-confidence
# floor). `resolveValues` returns a ranked (value, score) list DESCENDING by
# score, where score = 0.7·cosine(concept, value) + 0.3·norm_log_freq
# (ranking.rank). A rule must bind the SUBSET of codes the concept actually names
# — NOT the whole ranked domain. Binding the full domain is the correctness gap
# this fixes: `earnings` → {EARN, DEDUCTION} nets the -150 DEDUCTION into an
# "earnings" total (7200 instead of the true 7350) and mislabels it "verified".
#
# Gap cut: walk the descending scores and cut after the FIRST significant gap —
# `score[i] - score[i+1] > gap_threshold` binds the prefix `[0..i]`. A concept
# that names one code opens a large gap after it (earnings: EARN 0.75 vs
# DEDUCTION 0.35 → a 0.40 gap → bind {EARN}); a genuinely multi-code concept
# clusters (VAC/SICK/PERSONAL all ~0.7, then a 0.45 gap before the noise → keep
# the cluster); a uniformly-relevant domain (A 0.70, B 0.69, C 0.68) has no
# significant gap → bind ALL (no false narrowing).
#
# _GAP_THRESHOLD default 0.15 (absolute): comfortably below the ~0.3-0.4 blended
# gap a single-code concept opens (the 0.7·Δcosine term dominates — a clearly
# unrelated sibling sits ≥0.3 cosine away, ≈0.21 in blended score) yet well above
# the few-hundredths score jitter within a genuine same-meaning cluster.
# Provisional; the canonical value is `RuntimeSettings.resolve_via_gap_threshold`
# and should be tuned on real Phase-0 traffic.
_GAP_THRESHOLD = 0.15

# _MIN_CONFIDENCE default 0.3 (absolute floor on the TOP score): below it the
# concept matches NO code well enough to guess a filter (a top ~0.3 blended score
# implies cosine ≈0.3 even at full freq weight — a weak semantic match) → the
# fast path must NOT silently narrow → raw-loop fallback (the Slice-C S2 decision:
# a raw-loop downgrade, never an unanswerable confirm pause). A freq-only degrade
# already falls back before this floor is read, so the top score here is a
# genuine blended semantic score. Provisional; the canonical value is
# `RuntimeSettings.resolve_via_min_confidence`; tune on Phase-0 traffic.
_MIN_CONFIDENCE = 0.3


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
    the template binder renders as a typed AST `IN (…)` list (§3.4 step 3).

    Carries shape-only selection telemetry (`selected_count`/`dropped_count`/
    `top_score`/`cut_gap`) so the executor can emit a rule-resolution observation
    for tuning `resolve_via_gap_threshold`/`min_confidence` on real traffic — NO
    resolved code strings, only counts + aggregate scores (D25 posture)."""

    binds: str
    values: list[str]
    provenance: frozenset[tuple[str, str]] | None
    selected_count: int = 0
    dropped_count: int = 0
    top_score: float | None = None
    cut_gap: float | None = None


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
    reason: str  # "degraded" | "empty" | "denied" | "low_confidence"
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
    gap_threshold: float = _GAP_THRESHOLD,
    min_confidence: float = _MIN_CONFIDENCE,
) -> RuleExpansion:
    """Expand one dynamic rule via the D67 `resolve()` hook (§3.4). NO model
    round-trip — the typed programmatic entry point only. Applies the §3.4
    degrade/empty policy plus concept-subset selection (locked decision):

      - inner denial/error   -> `RuleFallback("denied")` (raw loop, denial verbatim)
      - `degraded`           -> `RuleFallback("degraded")` (freq-only ranking, S2 raw loop)
      - empty value set      -> `RuleFallback("empty")` (never a dropped filter)
      - top score < floor    -> `RuleFallback("low_confidence")` (no confident match, raw loop)
      - a clean set          -> `RuleBinding` of the GAP-CUT prefix (typed IN-list, D10)

    Concept-subset selection: the ranked `resolveValues` output is NOT bound
    wholesale — `_select_subset` binds the top prefix up to the first significant
    score gap (`> gap_threshold`), so `earnings` binds {EARN} not {EARN,
    DEDUCTION}. A uniformly-relevant domain (no significant gap) binds in full.
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
    if getattr(outcome, "degraded", False):
        # Freq-only ranking (embeddings unavailable): scores are log-freq
        # artifacts, NOT semantic — the gap cut and confidence floor below would
        # be meaningless. Honest-call S2: downgrade to the raw loop rather than
        # silently filter on a guessed code set (never an unanswerable pause).
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
    top_score = values[0].score
    if top_score < min_confidence:
        # The TOP code is only a weak semantic match → the concept confidently
        # names NO code in this domain. Do not guess a filter — raw-loop fallback
        # (S2: a downgrade, never an unanswerable confirm pause).
        return RuleFallback(
            rule_id=rule.rule_id,
            reason="low_confidence",
            provenance=getattr(outcome, "provenance", None),
        )
    selected = _select_subset(values, gap_threshold, min_confidence)
    dropped = len(values) - len(selected)
    # The gap AT the boundary (whatever caused the cut — the gap threshold or the
    # sub-floor drop) — shape-only tuning signal, `None` when the whole domain binds.
    cut_gap = (
        round(values[len(selected) - 1].score - values[len(selected)].score, 4)
        if dropped
        else None
    )
    return RuleBinding(
        binds=rule.binds,
        values=[v.value for v in selected],
        provenance=getattr(outcome, "provenance", None),
        selected_count=len(selected),
        dropped_count=dropped,
        top_score=top_score,
        cut_gap=cut_gap,
    )


def _select_subset(
    values: list[Any], gap_threshold: float, min_confidence: float
) -> list[Any]:
    """Return the ranked prefix the rule binds (D67), applying, in order:

    1. **Gap cut** — cut after position `i` when `score[i] - score[i+1] >
       gap_threshold`, binding the prefix `[0..i]`; with no significant gap the
       whole (uniformly-relevant) list is the candidate. A single value binds
       itself (no gap to walk).
    2. **Sub-floor drop (S1)** — drop any candidate whose score is below
       `min_confidence`. A gradual decline (e.g. [0.35, 0.25, 0.12], gaps 0.10 /
       0.13, none significant) would otherwise gap-cut ALL and silently filter on
       sub-floor codes the confidence floor exists to reject. Because scores are
       descending, this trims a trailing prefix. The caller has already asserted
       `values[0].score >= min_confidence`, so at least the top always survives
       (the `or values[:1]` is defensive and never actually fires).

    *values* is the `resolveValues` output, already sorted DESCENDING by score."""
    cut = len(values)
    for i in range(len(values) - 1):
        if values[i].score - values[i + 1].score > gap_threshold:
            cut = i + 1
            break
    candidate = values[:cut]
    confident = [v for v in candidate if v.score >= min_confidence]
    return confident or values[:1]


__all__ = [
    "ResolvedRule",
    "RuleBinding",
    "RuleExpansion",
    "RuleFallback",
    "expand_rule",
    "parse_rule",
]
