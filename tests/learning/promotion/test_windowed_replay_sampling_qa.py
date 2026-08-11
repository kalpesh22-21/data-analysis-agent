"""Golden replay must sample the WINDOWED slot types type-correctly (plan §3a fallout).

## Why this file exists

`learning/extractor/models.py::SLOT_TYPES` was a mirror of the runtime's set and had
drifted narrower — missing `relative_window` and `period_range` (D41/D49). Plan §3a
widened it, which made three canon blueprints (`bp-hires-per-month`,
`bp-hires-projection`, `bp-hires-in-range`) extractable for the first time.

Widening a vocabulary changes what flows downstream, and golden replay is the next
thing that keys off a slot's declared `type`. It was written when six types existed and
its sampler had a single default branch — "every other type ⇒ a synthetic STRING token"
— which is wrong for both new types and wrong in a way no unit test could see, because
neither type could reach it:

  * `relative_window` binds as a NUMBER (`INTERVAL {n} MONTH`; the unit lives in the
    template, which is the whole point of the type). A string there is a ClickHouse
    parse error at the probe, surfacing as `passed=False` — i.e. the candidate holds
    forever, blocked by the promotion guard, with a reason pointing at the warehouse.
  * `period_range` occupies TWO bind sites (`{name}_start`/`{name}_end`) while
    `_slot_types` was keyed by slot NAME, so BOTH halves missed the map entirely and
    were sampled as untyped strings compared against a date column.

Both are fixed (`_slot_types` now keys by TOKEN via the runtime's own
`slot_token_names`, and `_sample_value` handles both types). These tests pin the fix,
and the two "structure" tests below pin WHY it is shaped this way rather than as a
hand-written `_start`/`_end` suffix rule — that grammar already exists in three places
and a fourth copy is the mirror-drift failure this codebase keeps paying for.

Slug: PA-windowed-replay-sampling.
"""

from __future__ import annotations

import copy
from dataclasses import replace

from data_agent.learning.promotion.replay import _sample_value, _slot_types, golden_replay
from data_agent.runtime.blueprint.slots import slot_token_names

from .helpers import FakeWarehouseProbe, make_blueprint_candidate

_WINDOW_SQL = (
    "SELECT toStartOfMonth(most_recent_hire_date) AS total_earnings, "
    "COUNT(DISTINCT employee_code) AS hires FROM dbpcm_warehouse.employee "
    "WHERE most_recent_hire_date >= now() - INTERVAL {window_months} MONTH "
    "GROUP BY total_earnings"
)
_RANGE_SQL = (
    "SELECT toStartOfMonth(most_recent_hire_date) AS total_earnings, "
    "COUNT(DISTINCT employee_code) AS hires FROM dbpcm_warehouse.employee "
    "WHERE most_recent_hire_date >= {hire_window_start} "
    "AND most_recent_hire_date < {hire_window_end} GROUP BY total_earnings"
)


def _windowed_candidate(*, name: str, slot_type: str, template: str):
    """The frozen S4 fixture re-pointed at a windowed template + a single windowed
    slot. A windowed slot declares NO `binds_to` — the runtime `SlotSpec.parse` refuses
    one, and the extractor now enforces that at emit."""
    env = make_blueprint_candidate()
    payload = copy.deepcopy(env.payload)
    payload["parameterization"] = [
        {
            "locator": {"table": "dbpcm_warehouse.employee", "column": name, "value": "6"},
            "role": "slot",
            "slot": {"name": name, "type": slot_type, "binds_to": None, "required": True},
        }
    ]
    payload["generalization"]["sql_template"] = template
    payload["generalization"]["node_templates"] = []
    return replace(env, payload=payload)


# --- the sampler ----------------------------------------------------------------


def test_a_relative_window_samples_a_bare_integer():
    """The type's contract: the UNIT lives in the template, so the bind site is a
    number literal. A string here is `INTERVAL '__replay_sample_x__' MONTH`."""
    value = _sample_value("window_months", "relative_window")
    assert isinstance(value, int) and not isinstance(value, bool)
    assert value >= 1  # the resolver floors at 1; 0/negative is "no filter"


def test_a_period_range_bound_samples_a_date():
    assert _sample_value("hire_window_start", "period_range") == "2020-01-01"


def test_the_other_types_are_unchanged():
    assert _sample_value("d", "as_of_date") == "2020-01-01"
    assert _sample_value("d", "list") == ["__replay_sample_d__"]
    assert _sample_value("d", "entity") == "__replay_sample_d__"
    assert _sample_value("d", "") == "__replay_sample_d__"


# --- the type map is keyed by BIND TOKEN, not by slot name ----------------------


def test_a_period_range_registers_both_of_its_bind_tokens():
    """The invisible half of the bug. `_sample_bindings` looks values up by what
    `referenced_slots(template)` found in the SQL — i.e. by TOKEN — so a name-keyed map
    missed both halves of a `period_range` and silently fell through to the string
    default."""
    payload = {
        "parameterization": [
            {"role": "slot", "slot": {"name": "hire_window", "type": "period_range"}}
        ]
    }
    assert _slot_types(payload) == {
        "hire_window_start": "period_range",
        "hire_window_end": "period_range",
    }


def test_the_token_expansion_is_the_runtime_helper_not_a_local_copy():
    """`slot_token_names` is the anti-drift rule the executor, the corpus loader and the
    binder all call. A fourth hand-written `_start`/`_end` copy here is exactly the
    mirror drift that produced this file's parent bug."""
    from data_agent.runtime.blueprint.models import SlotSpec

    assert set(_slot_types(
        {"parameterization": [{"role": "slot", "slot": {"name": "w", "type": "period_range"}}]}
    )) == slot_token_names(SlotSpec(name="w", type="period_range"))


def test_a_scalar_slot_still_registers_exactly_one_token():
    payload = {"parameterization": [{"role": "slot", "slot": {"name": "dept", "type": "entity"}}]}
    assert _slot_types(payload) == {"dept": "entity"}


def test_a_malformed_slot_entry_is_skipped_not_raised():
    """`_slot_types` reads a rehydrated, model-authored plan on the FAIL-CLOSED
    promotion path; a raise here escapes `golden_replay`'s documented never-raises
    contract into the cron guard."""
    payload = {
        "parameterization": [
            "not a dict",
            {"role": "slot", "slot": None},
            {"role": "slot", "slot": {"name": 5, "type": "entity"}},
            {"role": "slot", "slot": {"name": "ok", "type": ["entity"]}},
        ]
    }
    assert _slot_types(payload) == {"ok": ""}


# --- end to end through the real binder -----------------------------------------


async def test_a_relative_window_template_binds_and_replays():
    probe = FakeWarehouseProbe()
    env = _windowed_candidate(
        name="window_months", slot_type="relative_window", template=_WINDOW_SQL
    )
    outcome = await golden_replay(env, probe=probe)
    assert outcome.passed, outcome.reason
    assert "INTERVAL 1 MONTH" in outcome.replay_sql
    assert "__replay_sample" not in outcome.replay_sql


async def test_a_period_range_template_binds_both_bounds_as_dates():
    probe = FakeWarehouseProbe()
    env = _windowed_candidate(
        name="hire_window", slot_type="period_range", template=_RANGE_SQL
    )
    outcome = await golden_replay(env, probe=probe)
    assert outcome.passed, outcome.reason
    assert outcome.replay_sql.count("'2020-01-01'") == 2
    assert "__replay_sample" not in outcome.replay_sql
    assert outcome.sampled_slots == ("hire_window_end", "hire_window_start")
