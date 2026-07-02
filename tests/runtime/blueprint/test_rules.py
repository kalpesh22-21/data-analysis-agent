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


async def test_expand_low_margin_is_fallback() -> None:
    hook = _Hook(ResolveOutcome(status="ok", values=_values("A", "B"), top_margin=0.01))
    out = await expand_rule(_rule(), resolve_hook=hook, credentials=None)
    assert isinstance(out, RuleFallback)
    assert out.reason == "degraded"


async def test_expand_single_value_is_not_low_margin() -> None:
    hook = _Hook(ResolveOutcome(status="ok", values=_values("A"), top_margin=None))
    out = await expand_rule(_rule(), resolve_hook=hook, credentials=None)
    assert isinstance(out, RuleBinding)  # a single unambiguous code binds


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
