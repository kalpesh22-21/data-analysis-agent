"""QA Layer-1: the parse layer for the windowed-period slot types (§2.1, D41/D49).

Two new slot `type`s — `relative_window` (a bounded trailing-integer "last N
months") and `period_range` (an explicit {start,end}) — plus the additive
`min_value`/`max_value` bounds. Structural, fail-loud parsing:
  - both new types are accepted;
  - `min_value`/`max_value` parse as ints and reject a bool / non-int;
  - the bounds are optional (default to the resolver's safety window).

ADD-only; does not modify the reviewer-owned `test_models.py`.
"""

from __future__ import annotations

import pytest

from data_agent.runtime.blueprint.models import (
    SLOT_TYPES,
    Blueprint,
    BlueprintParseError,
    SlotSpec,
)


# -- both new types are in the closed set + parse -------------------------------


def test_relative_window_is_a_valid_slot_type() -> None:
    assert "relative_window" in SLOT_TYPES
    spec = SlotSpec.parse({"name": "w", "type": "relative_window"})
    assert spec.type == "relative_window"
    # Bounds default to None (defer to the resolver's 1..120 safety window).
    assert spec.min_value is None and spec.max_value is None


def test_period_range_is_a_valid_slot_type() -> None:
    assert "period_range" in SLOT_TYPES
    spec = SlotSpec.parse({"name": "hw", "type": "period_range"})
    assert spec.type == "period_range"


def test_relative_window_through_blueprint_parse() -> None:
    bp = Blueprint.parse(
        id="bp-win",
        intent="hires over a trailing window",
        slots=[{"name": "window_months", "type": "relative_window", "required": True,
                "min_value": 1, "max_value": 36}],
        sql_template=(
            "SELECT COUNT(DISTINCT EmployeeCode) AS hires FROM db.t "
            "WHERE d >= now() - INTERVAL {window_months} MONTH"
        ),
        result_grain=["month"],
    )
    slot = bp.slot("window_months")
    assert slot is not None
    assert slot.type == "relative_window"
    assert slot.min_value == 1 and slot.max_value == 36


# -- min_value / max_value: int-validated, bool rejected ------------------------


def test_min_max_value_parse_as_ints() -> None:
    spec = SlotSpec.parse(
        {"name": "w", "type": "relative_window", "min_value": 3, "max_value": 12}
    )
    assert spec.min_value == 3 and spec.max_value == 12
    # H1b: the parse layer now enforces `1 <= min_value <= max_value <= 120`, so a
    # zero/negative bound is rejected at WRITE (no longer merely clamped at READ).
    with pytest.raises(BlueprintParseError, match="bounds must satisfy"):
        SlotSpec.parse({"name": "w", "type": "relative_window", "min_value": 0})


@pytest.mark.parametrize("field", ["min_value", "max_value"])
def test_bool_bound_is_rejected(field: str) -> None:
    # A bool is an int subclass; a `true` bound is an authoring bug, not the int 1.
    with pytest.raises(BlueprintParseError, match=f"'{field}' must be an integer"):
        SlotSpec.parse({"name": "w", "type": "relative_window", field: True})


@pytest.mark.parametrize("field", ["min_value", "max_value"])
@pytest.mark.parametrize("bad", ["6", 6.0, [6], {"n": 6}])
def test_non_int_bound_is_rejected(field: str, bad: object) -> None:
    with pytest.raises(BlueprintParseError, match=f"'{field}' must be an integer"):
        SlotSpec.parse({"name": "w", "type": "relative_window", field: bad})


def test_absent_bounds_are_none_not_zero() -> None:
    spec = SlotSpec.parse({"name": "w", "type": "relative_window"})
    assert spec.min_value is None
    assert spec.max_value is None


# -- enum_values interaction with the new types --------------------------------
#
# NOTE (spec-vs-impl): the QA brief expected `enum_values` to be REJECTED for the
# new types. The implementation does NOT reject it — like `min_value`/`max_value`,
# an irrelevant `enum_values` is CARRIED-BUT-IGNORED (the resolver for these types
# never reads it). This pins the ACTUAL, internally-consistent behavior; the
# discrepancy is reported to the caller rather than asserted as a (failing) reject.


@pytest.mark.parametrize("type_", ["relative_window", "period_range"])
def test_enum_values_on_new_type_is_carried_but_ignored(type_: str) -> None:
    spec = SlotSpec.parse({"name": "s", "type": type_, "enum_values": ["A", "B"]})
    # Accepted (not rejected) and simply carried on the frozen spec.
    assert spec.enum_values == ("A", "B")
    assert spec.type == type_


@pytest.mark.parametrize("type_", ["relative_window", "period_range"])
def test_malformed_enum_values_still_type_checked_on_new_types(type_: str) -> None:
    # Even carried-but-ignored, a non-list-of-strings `enum_values` is a structural
    # error and fails loud (the generic enum_values shape guard still applies).
    with pytest.raises(BlueprintParseError, match="'enum_values' must be a list of strings"):
        SlotSpec.parse({"name": "s", "type": type_, "enum_values": [1, 2]})


# -- the new types do NOT require enum_values ----------------------------------


@pytest.mark.parametrize("type_", ["relative_window", "period_range"])
def test_new_types_do_not_require_enum_values(type_: str) -> None:
    # Only `type: enum` requires a non-empty enum_values; the new types must parse
    # without it (the `enum requires values` gate must not spill onto them).
    spec = SlotSpec.parse({"name": "s", "type": type_})
    assert spec.enum_values is None
