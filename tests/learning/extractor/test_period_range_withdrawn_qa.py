"""KNOWN LIMITATION: `period_range` is withdrawn — S4 cannot emit its two-token form.

## The finding

`runtime/blueprint/models.py::SLOT_TYPES` carries `period_range`, the runtime executes
it (`bp-hires-in-range`), and plan §3a widened the extractor's mirror to match. But the
mirror was never the only thing in the way. `generalize/rewrite.py::
rewrite_sql_to_template` stamps exactly ONE placeholder per matched literal, named after
the slot:

    literal.replace(exp.Placeholder(this=name))          # rewrite.py:139

The runtime's date-range grammar is TWO tokens — `{name}_start` and `{name}_end`
(`runtime/blueprint/slots.py::slot_token_names`) — and nothing in the extractor or the
generalize package knows that grammar exists (`grep -rn '_start' src/data_agent/learning
/{extractor,generalize}` returns nothing). So both shapes a model can emit dead-end at
landing, and `tests/learning/generalize/test_period_range_s4_rewrite_gap_qa.py` proves both by
driving the real rewriter and the real landing gates. THIS file owns only the
extractor-side consequence: the type is known, withheld from the prompt, and declined.

## Why WITHDRAWN rather than documented

Because golden replay reports `passed=True` for both dead-end shapes. The unit suite's
probe never executes the SQL, so the loop's own quality gate green-lights a template
ClickHouse rejects. A silent dead end four stages downstream is strictly worse than an
honest decline at extraction — the same argument that made `binds_to` type-dependent
rather than merely offered, applied to the type itself.

So: the MIRROR stays at parity (that was the actual drift defect, and its parity test is
what makes re-enabling a one-line change), and `UNSUPPORTED_SLOT_TYPES` withholds the
type from the prompt enum and declines it at validation.

## What re-enabling actually costs — it is not a suffix in the rewriter

`parameterization` is one entry per literal predicate, each carrying its own `slot`.
There is no way to express "these two predicates are the two BOUNDS of one range". That
needs a payload shape, a rewriter change, and a rule deciding WHICH bound each predicate
is. That last one is the dangerous part: infer it wrong and a date filter is silently
inverted, which is the D56 wrong-answer class. Tracked in the plan doc.

## Contrast

`relative_window` — the other type this slice added — is NOT withdrawn. It is one token,
S4 emits it correctly, and QA verified it clears the whole path against real ClickHouse.

Delete this file when the rewriter learns the grammar. The tripwire that fires first is
`test_period_range_s4_rewrite_gap_qa::test_every_template_token_s4_emits_is_a_declared_
bind_token`, which is parametrized over the offered enum — so re-enabling the type
without teaching S4 its grammar fails there immediately.

Slug: PA-period-range-withdrawn.
"""

from __future__ import annotations

import pytest

from data_agent.learning.extractor.models import (
    DOMAINLESS_SLOT_TYPES,
    SLOT_TYPES,
    UNSUPPORTED_SLOT_TYPES,
    Decline,
    ExtractedCandidate,
)
from data_agent.learning.extractor.schema import SLOT_TYPE_ENUM, build_extractor_tool
from data_agent.learning.extractor.validation import REASON_BAD_ROLE, to_candidate
from data_agent.runtime.blueprint.models import SLOT_TYPES as RUNTIME_SLOT_TYPES

from .helpers import blueprint_raw, make_summary, make_tool_call, param_slot

_RANGE_SQL = (
    "SELECT COUNT(employee_code) AS hires FROM dbpcm_warehouse.employee "
    "WHERE most_recent_hire_date >= '2025-01-01' "
    "AND most_recent_hire_date < '2025-07-01'"
)


def _range_param(name: str, value: str, *, binds_to: str | None = None) -> dict:
    return param_slot(
        "most_recent_hire_date", name=name, slot_type="period_range", value=value,
        table="dbpcm_warehouse.employee", binds_to=binds_to,
    )


def _decline_for(parameterization: list[dict]) -> Decline | ExtractedCandidate:
    """Both bounds are always supplied: D97 totality wants one entry per literal
    predicate and `_RANGE_SQL` has two, so a single-entry plan would decline
    `totality_violation` and prove nothing about the type."""
    raw = blueprint_raw(parameterization=parameterization)
    summary = make_summary(tool_calls=(make_tool_call(ref="tc1", sql=_RANGE_SQL),))
    return to_candidate(raw, summary, known_rules=frozenset())


# --- the withdrawal itself -------------------------------------------------------


def test_the_type_is_known_and_offered_for_guarded_between_ranges():
    """The mirror stays at PARITY — the drift defect is fixed and stays fixed. What is
    withheld is the PROMPT ENUM, which is a different statement: "this type exists and
    we cannot carry it yet", not "this type does not exist"."""
    assert "period_range" in SLOT_TYPES
    assert "period_range" in RUNTIME_SLOT_TYPES
    assert "period_range" in UNSUPPORTED_SLOT_TYPES
    assert "period_range" in SLOT_TYPE_ENUM


def test_the_tool_schema_offers_the_guarded_range_type_and_locator():
    import json

    schema = json.dumps(build_extractor_tool())
    assert "period_range" in schema
    assert "between_range" in schema


def test_relative_window_is_still_offered():
    """The contrast that makes the withdrawal a scalpel rather than a retreat: the other
    type this slice added is one token, S4 emits it, and it clears the whole path."""
    assert "relative_window" in SLOT_TYPE_ENUM
    assert "relative_window" not in UNSUPPORTED_SLOT_TYPES


def test_a_period_range_slot_is_declined_with_the_real_reason():
    """Reachable by a replayed candidate, a hand-fed payload or a non-enforcing model.
    The decline names S4, not a symptom, and tells the model what to do instead."""
    out = _decline_for(
        [_range_param("hire_window", "2025-01-01"), _range_param("hire_window", "2025-07-01")]
    )
    assert isinstance(out, Decline)
    assert out.reason == REASON_BAD_ROLE
    assert "S4 cannot yet generalize" in out.detail
    assert "two separate as_of_date/period slots" in out.detail


def test_the_unsupported_check_runs_before_the_binds_to_rules():
    """Ordering matters for the DECLINE REASON. A `period_range` carrying a `binds_to`
    violates both rules; the one worth telling a prompt-tuner about is the type."""
    bind = "dbpcm_warehouse.employee.most_recent_hire_date"
    out = _decline_for(
        [
            _range_param("hire_window", "2025-01-01", binds_to=bind),
            _range_param("hire_window", "2025-07-01", binds_to=bind),
        ]
    )
    assert isinstance(out, Decline)
    assert "S4 cannot yet generalize" in out.detail
    assert "must not declare binds_to" not in out.detail
    assert "must not declare binds_to" not in out.detail


@pytest.mark.parametrize("slot_type", sorted(SLOT_TYPES - UNSUPPORTED_SLOT_TYPES))
def test_every_offered_type_still_extracts(slot_type: str) -> None:
    """The withdrawal must be a scalpel: exactly one type removed, nothing else."""
    kwargs = {"binds_to": None} if slot_type in DOMAINLESS_SLOT_TYPES else {}
    if slot_type == "enum":
        pytest.skip("an enum slot needs enum_values; covered in test_validation.py")
    raw = blueprint_raw(
        parameterization=[
            param_slot("window_months", slot_type=slot_type, value="6",
                       table="dbpcm_warehouse.employee", **kwargs)
        ]
    )
    summary = make_summary(
        tool_calls=(make_tool_call(
            ref="tc1",
            sql="SELECT COUNT(employee_code) AS hires FROM dbpcm_warehouse.employee "
                "WHERE window_months = 6",
        ),)
    )
    assert isinstance(to_candidate(raw, summary, known_rules=frozenset()), ExtractedCandidate)
