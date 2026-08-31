"""A `period` slot must sample as a DATE, because that is where S4 binds it.

Found on the live stack: a reviewer clicked Approve and got
`approve_blocked_replay:probe_unavailable` with nothing else to go on. The replay had bound
`toDate('__replay_sample_pay_period_end_start__')` and ClickHouse answered
`Code: 38 ... Cannot parse Date from String`, which `golden_replay` catches into that opaque
reason (`replay.py` swallows the probe exception with no log line — see the module note).

The type gloss calls a `period` "a warehouse pay-period key, NOT a calendar date", and on that
reading the old string sample looked right. The corpus disagrees: every live blueprint with a
period slot binds it inside a date function. An ISO date satisfies both readings, so the fix is
the sample rather than the vocabulary.
"""

from __future__ import annotations

import pytest

from data_agent.learning.promotion.replay import _SAMPLE_DATE, _sample_bindings, _sample_value
from data_agent.runtime.blueprint.models import SLOT_TYPES
from data_agent.runtime.blueprint.template import bind_template


@pytest.mark.parametrize("slot_type", ["as_of_date", "period", "period_range"])
def test_every_date_shaped_slot_samples_as_a_parseable_date(slot_type: str) -> None:
    """All three land in date contexts; all three must bind to something `toDate()` accepts."""
    assert _sample_value("when", slot_type) == _SAMPLE_DATE


def test_a_period_sample_survives_todate_in_a_bound_template() -> None:
    """The exact shape that failed in production: the slot inside `toDate(...)`."""
    template = "SELECT 1 FROM t WHERE d >= toDate({start}) AND d < toDate({end})"
    bound = bind_template(
        template, _sample_bindings({"start", "end"}, {"start": "period", "end": "period"})
    )
    assert "__replay_sample_" not in bound
    assert bound.count(f"'{_SAMPLE_DATE}'") == 2


def test_non_date_types_keep_their_string_token() -> None:
    """The change is scoped to the date-shaped types — an entity slot still samples as a
    synthetic string, which is what makes the value obviously non-real in a query log."""
    for slot_type in ("string", "entity", "enum"):
        assert _sample_value("who", slot_type) == "__replay_sample_who__"


def test_every_declared_slot_type_produces_a_bindable_sample() -> None:
    """The guard that would have caught this class: no slot type may sample to something that
    cannot be bound. It does not know which types are dates — only that the vocabulary and the
    sampler cannot drift apart silently."""
    for slot_type in sorted(SLOT_TYPES):
        value = _sample_value("x", slot_type)
        assert value not in (None, ""), slot_type
        bound = bind_template("SELECT 1 FROM t WHERE c = {x}", {"x": value})
        assert "{x}" not in bound, slot_type
