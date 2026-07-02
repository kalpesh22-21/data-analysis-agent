"""Layer-1: the D67 `resolve_via` rule parser + expander (runblueprint-design §3.4).

Pure parse + the degrade/empty policy — no infra. The expansion path uses a
scripted `resolve()` hook double (NO model round-trip, by construction).
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.blueprint.rules import (
    ResolvedRule,
    RuleBinding,
    RuleFallback,
    expand_rule,
    parse_rule,
)
from data_agent.runtime.composite.ranking import ResolvedValue
from data_agent.runtime.composite.resolve_values import ResolveOutcome

_E = "dbpcm_warehouse.employee"


def test_parse_rule_explicit_binds() -> None:
    rule = parse_rule(
        {
            "id": "active",
            "resolve_via": "resolveValues(FieldId, 'employee status')",
            "table": "db.paf",
            "binds": "field_codes",
        }
    )
    assert rule == ResolvedRule(
        rule_id="active", table="db.paf", column="FieldId", concept="employee status", binds="field_codes"
    )


def test_parse_rule_derives_binds_from_predicate() -> None:
    rule = parse_rule(
        {
            "resolve_via": 'resolveValues(FieldId, "status")',
            "table": "db.paf",
            "predicate": "FieldId IN ({field_codes})",
        }
    )
    assert rule is not None
    assert rule.binds == "field_codes"
    assert rule.rule_id == "field_codes"  # falls back to the binds name


def test_parse_rule_static_or_malformed_is_none() -> None:
    assert parse_rule({"predicate": "RegisterType = 'EARN'"}) is None  # static — no resolve_via
    assert parse_rule({"resolve_via": "notResolveValues(x)"}) is None  # wrong function
    assert parse_rule({"resolve_via": "resolveValues(FieldId, 'x')"}) is None  # no table
    assert parse_rule("nope") is None
    assert (
        parse_rule({"resolve_via": "resolveValues(FieldId, 'x')", "table": "t", "predicate": "a IN ({p}) OR b IN ({q})"})
        is None
    )  # ambiguous — two placeholders, no explicit binds


def _rule() -> ResolvedRule:
    return ResolvedRule(rule_id="r", table=_E, column="StatusCode", concept="active", binds="codes")


class _Hook:
    def __init__(self, outcome: ResolveOutcome) -> None:
        self._outcome = outcome
        self.calls = 0

    async def resolve(self, *, table, column, concept, period, credentials) -> ResolveOutcome:
        self.calls += 1
        return self._outcome


def _values(*vals: str) -> list[ResolvedValue]:
    return [ResolvedValue(value=v, description=None, score=0.9, freq=10) for v in vals]


def _scored(*pairs: tuple[str, float]) -> list[ResolvedValue]:
    """Build a ranked (value, score) list DESCENDING, as `resolveValues` returns."""
    return [ResolvedValue(value=v, description=None, score=s, freq=10) for v, s in pairs]


async def test_expand_clean_binding() -> None:
    hook = _Hook(ResolveOutcome(status="ok", values=_values("A", "B"), top_margin=0.5))
    out = await expand_rule(_rule(), resolve_hook=hook, credentials=None)
    assert isinstance(out, RuleBinding)
    assert out.binds == "codes"
    assert out.values == ["A", "B"]
    assert hook.calls == 1


async def test_expand_degraded_is_fallback_not_pause() -> None:
    # S2 honest-call: a degraded resolve DOWNGRADES to a raw-loop fallback (never a
    # pause the model cannot answer, never a silent filter on a guessed set).
    hook = _Hook(ResolveOutcome(status="ok", values=_values("A", "B"), degraded=True, top_margin=0.5))
    out = await expand_rule(_rule(), resolve_hook=hook, credentials=None)
    assert isinstance(out, RuleFallback)
    assert out.reason == "degraded"


# --- D67 concept-subset selection (margin/gap-cut + low-confidence floor) -----


async def test_expand_earnings_gap_cut_binds_top_code_only() -> None:
    # The load-bearing case: [EARN 0.75, DEDUCTION 0.35] — the 0.40 gap is
    # significant (> 0.15) → bind {EARN} ONLY, never the whole domain (binding
    # DEDUCTION too nets its deductions into an "earnings" total: the D67 bug).
    hook = _Hook(ResolveOutcome(status="ok", values=_scored(("EARN", 0.75), ("DEDUCTION", 0.35))))
    out = await expand_rule(_rule(), resolve_hook=hook, credentials=None)
    assert isinstance(out, RuleBinding)
    assert out.values == ["EARN"]


async def test_expand_multi_code_cluster_keeps_the_cluster() -> None:
    # A genuinely multi-code concept: the cluster VAC/SICK/PERSONAL (~0.7) is
    # tight; the 0.45 gap before WORK 0.21 is the first significant one → bind the
    # whole cluster, drop only the noise below the gap.
    hook = _Hook(
        ResolveOutcome(
            status="ok",
            values=_scored(("VAC", 0.71), ("SICK", 0.69), ("PERSONAL", 0.66), ("WORK", 0.21)),
        )
    )
    out = await expand_rule(_rule(), resolve_hook=hook, credentials=None)
    assert isinstance(out, RuleBinding)
    assert out.values == ["VAC", "SICK", "PERSONAL"]


async def test_expand_uniform_domain_binds_all_no_false_narrowing() -> None:
    # A uniformly-relevant domain [A 0.70, B 0.69, C 0.68] has no significant gap
    # → bind ALL. (This replaces the old top-margin fallback: a small #1-#2 gap is
    # NOT ambiguity here, it is a real multi-code match — no false narrowing.)
    hook = _Hook(ResolveOutcome(status="ok", values=_scored(("A", 0.70), ("B", 0.69), ("C", 0.68))))
    out = await expand_rule(_rule(), resolve_hook=hook, credentials=None)
    assert isinstance(out, RuleBinding)
    assert out.values == ["A", "B", "C"]


async def test_expand_gradual_decline_drops_sub_floor_tail() -> None:
    # S1: a gradual decline [0.35, 0.25, 0.12] has NO significant gap (gaps 0.10,
    # 0.13 ≤ 0.15), so the gap cut alone would bind ALL — including 0.25 and 0.12,
    # both below the 0.3 floor. The sub-floor drop trims them → bind ONLY {A}.
    hook = _Hook(ResolveOutcome(status="ok", values=_scored(("A", 0.35), ("B", 0.25), ("C", 0.12))))
    out = await expand_rule(_rule(), resolve_hook=hook, credentials=None)
    assert isinstance(out, RuleBinding)
    assert out.values == ["A"]
    assert out.dropped_count == 2  # B and C were sub-floor
    assert out.selected_count == 1


async def test_expand_sub_floor_drop_keeps_above_floor_prefix() -> None:
    # A sub-floor value sits after two above-floor codes with no significant gap
    # [0.45, 0.40, 0.28] (gaps 0.05, 0.12) → gap cut binds all three, the sub-floor
    # drop removes only the 0.28 tail → bind {A, B}, drop {C}.
    hook = _Hook(ResolveOutcome(status="ok", values=_scored(("A", 0.45), ("B", 0.40), ("C", 0.28))))
    out = await expand_rule(_rule(), resolve_hook=hook, credentials=None)
    assert isinstance(out, RuleBinding)
    assert out.values == ["A", "B"]
    assert out.dropped_count == 1


async def test_expand_binding_carries_shape_only_telemetry() -> None:
    # The gap-cut binding exposes shape-only selection telemetry (counts + scores,
    # NO code strings) for tuning the cutoffs on real traffic.
    hook = _Hook(ResolveOutcome(status="ok", values=_scored(("EARN", 0.75), ("DEDUCTION", 0.35))))
    out = await expand_rule(_rule(), resolve_hook=hook, credentials=None)
    assert isinstance(out, RuleBinding)
    assert out.selected_count == 1
    assert out.dropped_count == 1
    assert out.top_score == 0.75
    assert out.cut_gap == 0.40  # the significant gap at the cut boundary


async def test_expand_low_confidence_top_is_fallback() -> None:
    # The top code is only a weak match (0.22 < the 0.3 floor) → the concept names
    # no code confidently → raw-loop fallback, never a guessed filter.
    hook = _Hook(ResolveOutcome(status="ok", values=_scored(("A", 0.22), ("B", 0.05))))
    out = await expand_rule(_rule(), resolve_hook=hook, credentials=None)
    assert isinstance(out, RuleFallback)
    assert out.reason == "low_confidence"


async def test_expand_single_confident_value_binds() -> None:
    hook = _Hook(ResolveOutcome(status="ok", values=_scored(("A", 0.8))))
    out = await expand_rule(_rule(), resolve_hook=hook, credentials=None)
    assert isinstance(out, RuleBinding)  # a single confident code binds itself
    assert out.values == ["A"]


async def test_expand_empty_is_fallback() -> None:
    hook = _Hook(ResolveOutcome(status="ok", values=[], top_margin=None))
    out = await expand_rule(_rule(), resolve_hook=hook, credentials=None)
    assert isinstance(out, RuleFallback)
    assert out.reason == "empty"


async def test_expand_denial_is_fallback_with_provenance() -> None:
    prov: Any = frozenset({(_E, "StatusCode")})
    hook = _Hook(
        ResolveOutcome(
            status="denied",
            values=[],
            provenance=prov,
            error_code="COLUMN_SCOPE_VIOLATION",
            user_message="that column is out of scope",
            retryable=False,
        )
    )
    out = await expand_rule(_rule(), resolve_hook=hook, credentials=None)
    assert isinstance(out, RuleFallback)
    assert out.reason == "denied"
    assert out.provenance == prov
    # S6c: the inner denial code/message pass through verbatim.
    assert out.error_code == "COLUMN_SCOPE_VIOLATION"
    assert out.user_message == "that column is out of scope"
