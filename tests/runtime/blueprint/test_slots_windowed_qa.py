"""QA Layer-1: the windowed-period slot resolvers (§3.2, D41/D49).

`relative_window` (a bounded trailing integer "last N months") and `period_range`
(an explicit {start,end}). Both resolvers are PURE code — NO LLM, NO
relative→concrete date arithmetic (D49). An unresolvable/ambiguous/deictic value
returns `AskUser`, never a guess. Also covers the `slot_token_names` /
`expand_binding` anti-drift helpers (a `period_range` occupies TWO bind tokens).

ADD-only; does not modify the reviewer-owned `test_slots.py`.
"""

from __future__ import annotations

import pytest

from data_agent.runtime.blueprint.models import SlotSpec
from data_agent.runtime.blueprint.slots import (
    AskUser,
    PeriodRange,
    SlotBinding,
    expand_binding,
    resolve_slot,
    slot_token_names,
)


def _spec(type_: str, **kw: object) -> SlotSpec:
    return SlotSpec(name=kw.pop("name", "s"), type=type_, **kw)  # type: ignore[arg-type]


# ===========================================================================
# relative_window
# ===========================================================================


@pytest.mark.parametrize("raw", [6, "6", " 6 "])
def test_relative_window_accepts_pure_integer_forms(raw: object) -> None:
    # H2: ONLY an int or a pure-digit string (after strip). The unit lives in the
    # template (`INTERVAL {n} MONTH`), so a plain number is all the resolver accepts.
    out = resolve_slot(raw, _spec("relative_window"))
    assert isinstance(out, SlotBinding)
    assert out.value == 6
    assert isinstance(out.value, int) and not isinstance(out.value, bool)


@pytest.mark.parametrize("raw", ["6 months", "6 mo", "6 weeks", "6; DROP"])
def test_relative_window_trailing_text_asks_no_unit_mismatch(raw: object) -> None:
    # H2 (wrong-answer fix): a leading integer with trailing text must NOT silently
    # bind (e.g. "6 weeks" → 6 MONTHS is a unit mismatch). Any trailing non-digit
    # text → AskUser, never a guessed unit.
    out = resolve_slot(raw, _spec("relative_window", required=True))
    assert isinstance(out, AskUser) and out.reason == "invalid"


@pytest.mark.parametrize("raw", ["6.5", "six", "", "months", "-3", "  "])
def test_relative_window_non_integer_or_missing_asks(raw: object) -> None:
    # A fractional "6.5" is a float (never truncated to 6); a non-numeric phrase and
    # an empty/whitespace value never resolve. Required slot → AskUser.
    out = resolve_slot(raw, _spec("relative_window", required=True))
    assert isinstance(out, AskUser)


def test_relative_window_fraction_is_not_truncated() -> None:
    out = resolve_slot("6.5", _spec("relative_window"))
    assert isinstance(out, AskUser)  # NOT a silent bind to 6


def test_relative_window_bool_is_rejected() -> None:
    # A bool is an int subclass — never a window count (never binds 1/0).
    out = resolve_slot(True, _spec("relative_window"))
    assert isinstance(out, AskUser) and out.reason == "invalid"
    out2 = resolve_slot(False, _spec("relative_window"))
    assert isinstance(out2, AskUser) and out2.reason == "invalid"


def test_relative_window_float_value_is_rejected() -> None:
    out = resolve_slot(2.0, _spec("relative_window"))
    assert isinstance(out, AskUser) and out.reason == "invalid"


def test_relative_window_none_required_asks_missing() -> None:
    out = resolve_slot(None, _spec("relative_window", required=True))
    assert isinstance(out, AskUser) and out.reason == "missing"


def test_relative_window_empty_required_asks_missing() -> None:
    out = resolve_slot("", _spec("relative_window", required=True))
    assert isinstance(out, AskUser) and out.reason == "missing"


# -- bounds -----------------------------------------------------------------


def test_relative_window_below_min_asks() -> None:
    out = resolve_slot(2, _spec("relative_window", min_value=3, max_value=12))
    assert isinstance(out, AskUser) and out.reason == "invalid"


def test_relative_window_above_max_asks() -> None:
    out = resolve_slot(13, _spec("relative_window", min_value=3, max_value=12))
    assert isinstance(out, AskUser) and out.reason == "invalid"


def test_relative_window_exactly_at_both_bounds_binds() -> None:
    lo = resolve_slot(3, _spec("relative_window", min_value=3, max_value=12))
    hi = resolve_slot(12, _spec("relative_window", min_value=3, max_value=12))
    assert isinstance(lo, SlotBinding) and lo.value == 3
    assert isinstance(hi, SlotBinding) and hi.value == 12


def test_relative_window_above_default_ceiling_asks() -> None:
    # No spec bounds → the resolver's hard safety window (1..120). An absurd
    # 121-month window can never bind (no `INTERVAL 999999 MONTH`).
    out = resolve_slot(121, _spec("relative_window"))
    assert isinstance(out, AskUser) and out.reason == "invalid"


def test_relative_window_at_default_ceiling_binds() -> None:
    out = resolve_slot(120, _spec("relative_window"))
    assert isinstance(out, SlotBinding) and out.value == 120


def test_relative_window_zero_below_default_floor_asks() -> None:
    # n >= 1 always — a zero/negative window is not a filter.
    assert isinstance(resolve_slot(0, _spec("relative_window")), AskUser)
    assert isinstance(resolve_slot(-4, _spec("relative_window")), AskUser)


def test_relative_window_min_value_zero_is_clamped_to_one() -> None:
    # An authored min_value=0 is clamped to 1 (n>=1 always); 0 still asks.
    assert isinstance(resolve_slot(0, _spec("relative_window", min_value=0)), AskUser)
    assert isinstance(resolve_slot(1, _spec("relative_window", min_value=0)), SlotBinding)


# ===========================================================================
# period_range
# ===========================================================================


def test_period_range_valid_dict_binds_periodrange() -> None:
    out = resolve_slot(
        {"start": "2026-01-01", "end": "2026-03-31"}, _spec("period_range")
    )
    assert isinstance(out, SlotBinding)
    assert out.value == PeriodRange("2026-01-01", "2026-03-31")
    # The bounds are the VERBATIM input strings — no date arithmetic/normalization.
    assert out.value.start == "2026-01-01"
    assert out.value.end == "2026-03-31"


def test_period_range_valid_list_binds_periodrange() -> None:
    out = resolve_slot(["2026-01-01", "2026-03-31"], _spec("period_range"))
    assert isinstance(out, SlotBinding)
    assert out.value == PeriodRange("2026-01-01", "2026-03-31")


def test_period_range_valid_datetime_shape_binds() -> None:
    out = resolve_slot(
        {"start": "2026-01-01T00:00:00", "end": "2026-03-31T23:59:59"},
        _spec("period_range"),
    )
    assert isinstance(out, SlotBinding)
    assert out.value.start == "2026-01-01T00:00:00"


def test_period_range_reversed_asks() -> None:
    out = resolve_slot(
        {"start": "2026-03-31", "end": "2026-01-01"}, _spec("period_range")
    )
    assert isinstance(out, AskUser) and out.reason == "invalid"


def test_period_range_equal_bounds_asks() -> None:
    out = resolve_slot(
        {"start": "2026-01-01", "end": "2026-01-01"}, _spec("period_range")
    )
    assert isinstance(out, AskUser) and out.reason == "invalid"


def test_period_range_mixed_date_datetime_shapes_asks() -> None:
    # One bound carries a T-time, the other does not → an unsound string compare.
    out = resolve_slot(
        {"start": "2026-01-01", "end": "2026-03-31T00:00:00"}, _spec("period_range")
    )
    assert isinstance(out, AskUser) and out.reason == "invalid"


def test_period_range_missing_key_asks() -> None:
    out = resolve_slot({"start": "2026-01-01"}, _spec("period_range"))
    assert isinstance(out, AskUser)


@pytest.mark.parametrize("raw", ["2026-01-01", 20260101, ["2026-01-01"], ["a", "b", "c"], {}])
def test_period_range_non_dict_or_wrong_shape_asks(raw: object) -> None:
    out = resolve_slot(raw, _spec("period_range"))
    assert isinstance(out, AskUser)


@pytest.mark.parametrize(
    "deictic",
    ["last", "this month", "last month", "latest", "current quarter", "YTD"],
)
def test_period_range_deictic_bound_asks_never_resolves_to_a_date(deictic: str) -> None:
    # A deictic/relative word is NEVER resolved to a concrete date here (D49 — no
    # wall-clock or relative→concrete arithmetic). It returns AskUser, and the
    # signal carries NO PeriodRange value (no date was fabricated).
    out = resolve_slot({"start": deictic, "end": "2026-03-31"}, _spec("period_range"))
    assert isinstance(out, AskUser)
    assert out.reason == "fuzzy"
    assert not isinstance(out, SlotBinding)


def test_period_range_both_deictic_asks() -> None:
    out = resolve_slot({"start": "last month", "end": "this month"}, _spec("period_range"))
    assert isinstance(out, AskUser) and out.reason == "fuzzy"


def test_period_range_required_absent_asks_missing() -> None:
    out = resolve_slot(None, _spec("period_range", required=True))
    assert isinstance(out, AskUser) and out.reason == "missing"


def test_period_range_non_string_bound_asks() -> None:
    out = resolve_slot({"start": 20260101, "end": 20260331}, _spec("period_range"))
    assert isinstance(out, AskUser)


def test_period_range_malformed_iso_asks() -> None:
    out = resolve_slot({"start": "2026/01/01", "end": "2026/03/31"}, _spec("period_range"))
    assert isinstance(out, AskUser)


# ===========================================================================
# slot_token_names / expand_binding — the anti-drift helpers
# ===========================================================================


@pytest.mark.parametrize("type_", ["string", "entity", "enum", "period", "as_of_date",
                                    "list", "relative_window"])
def test_scalar_types_occupy_a_single_token(type_: str) -> None:
    spec = _spec(type_, name="X")
    assert slot_token_names(spec) == {"X"}


def test_period_range_occupies_two_tokens() -> None:
    spec = _spec("period_range", name="hire_window")
    assert slot_token_names(spec) == {"hire_window_start", "hire_window_end"}


def test_expand_binding_scalar_maps_name_to_value() -> None:
    spec = _spec("relative_window", name="wm")
    assert expand_binding(spec, 6) == {"wm": 6}


def test_expand_binding_string_scalar() -> None:
    spec = _spec("string", name="dept")
    assert expand_binding(spec, "Sales") == {"dept": "Sales"}


def test_expand_binding_period_range_expands_both_bounds() -> None:
    spec = _spec("period_range", name="hw")
    pr = PeriodRange("2026-01-01", "2026-03-31")
    assert expand_binding(spec, pr) == {
        "hw_start": "2026-01-01",
        "hw_end": "2026-03-31",
    }


def test_expand_binding_period_range_non_periodrange_falls_back_to_single() -> None:
    # Defensive: a period_range spec whose value is somehow NOT a PeriodRange binds
    # under the single {name} token (never silently splits a non-range value).
    spec = _spec("period_range", name="hw")
    assert expand_binding(spec, "raw") == {"hw": "raw"}
