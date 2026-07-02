"""QA4 Layer-1: slot-resolver type-confusion + near-miss matrix (§3.2).

Adversarial extensions of `test_slots.py`: every slot type handed wrong-type raw
values (list/dict/number/bool), enum near-misses (partial/whitespace/case) that
must ASK not silently accept, list slots with hostile/empty/heterogeneous/nested
elements, and the list-of-enum gap. Resolvers are PURE code — no LLM, no crash;
an unresolvable value must return `AskUser`, never a guess.

ADD-only; does not modify the reviewer-owned `test_slots.py`.
"""

from __future__ import annotations

import pytest

from data_agent.runtime.blueprint.models import SlotSpec
from data_agent.runtime.blueprint.slots import (
    AskUser,
    OmitSlot,
    SlotBinding,
    resolve_slot,
)


def _spec(type_: str, **kw: object) -> SlotSpec:
    return SlotSpec(name=kw.pop("name", "s"), type=type_, **kw)  # type: ignore[arg-type]


# -- enum wrong-type / near-miss (must ASK, never silently accept) ----------


@pytest.mark.parametrize("raw", [["A"], 5, True, {"k": "v"}, 3.14])
def test_enum_wrong_type_asks_user(raw: object) -> None:
    out = resolve_slot(raw, _spec("enum", enum_values=("A", "D")))
    assert isinstance(out, AskUser) and out.reason == "no_match"


def test_enum_partial_near_miss_asks_user_not_accept() -> None:
    # "activ" is a PREFIX of "Active" — must NOT be accepted; ask.
    out = resolve_slot("activ", _spec("enum", enum_values=("Active", "Inactive")))
    assert isinstance(out, AskUser)


def test_enum_whitespace_and_case_recovers_canonical() -> None:
    out = resolve_slot("  active  ", _spec("enum", enum_values=("Active",)))
    assert isinstance(out, SlotBinding) and out.value == "Active"


def test_enum_internal_whitespace_is_not_a_match() -> None:
    out = resolve_slot("Ac tive", _spec("enum", enum_values=("Active",)))
    assert isinstance(out, AskUser)


# -- string / entity wrong-type ---------------------------------------------


def test_string_slot_list_value_asks_user_not_repr() -> None:
    # UPDATED (review FIX 4a): a list handed to a scalar `string` slot is now
    # REJECTED to an AskUser(reason="invalid"), never stringified to a Python repr
    # like "[1, 2]". (List ELEMENTS resolved via a `list` slot keep their coercion.)
    out = resolve_slot([1, 2], _spec("string"))
    assert isinstance(out, AskUser) and out.reason == "invalid"


def test_string_slot_dict_value_asks_user_not_repr() -> None:
    out = resolve_slot({"k": 1}, _spec("string"))
    assert isinstance(out, AskUser) and out.reason == "invalid"


def test_entity_number_binds_as_stringified_direct() -> None:
    out = resolve_slot(42, _spec("entity"), domain=None)
    assert isinstance(out, SlotBinding) and out.value == "42"


# -- period wrong-type ------------------------------------------------------


def test_period_number_no_domain_asks_user() -> None:
    out = resolve_slot(202605, _spec("period"))
    assert isinstance(out, AskUser) and out.reason == "fuzzy"


def test_period_list_no_domain_asks_user() -> None:
    out = resolve_slot(["2026-05-31"], _spec("period"))
    assert isinstance(out, AskUser)


def test_period_number_with_domain_stringifies_and_matches() -> None:
    out = resolve_slot(20260531, _spec("period"), domain=["20260531", "20260630"])
    assert isinstance(out, SlotBinding) and out.value == "20260531"


# -- list / IN edge cases ---------------------------------------------------


def test_list_empty_asks_user() -> None:
    out = resolve_slot([], _spec("list"))
    assert isinstance(out, AskUser) and out.reason == "no_match"


def test_list_heterogeneous_elements_all_stringify_no_domain() -> None:
    out = resolve_slot([1, "a", True], _spec("list"), domain=None)
    assert isinstance(out, SlotBinding)
    assert out.value == ["1", "a", "True"]


def test_list_one_hostile_element_no_match_asks_user() -> None:
    out = resolve_slot(["OT", "ZZZ"], _spec("list"), domain=["OT", "PT"])
    assert isinstance(out, AskUser) and out.reason == "no_match"


def test_list_nested_list_element_stringifies_no_domain() -> None:
    out = resolve_slot([[1, 2]], _spec("list"), domain=None)
    assert isinstance(out, SlotBinding) and out.value == ["[1, 2]"]


def test_list_of_enum_does_not_enforce_enum_membership() -> None:
    # PIN (Slice B): SlotSpec has a single `type`, so a "list of enum" is not
    # expressible — a `list` slot resolves each element as a SCALAR and does NOT
    # check `enum_values`, even when the spec carries them. An out-of-enum element
    # binds instead of asking. Flagged as a Slice-B gap for list-of-enum slots.
    out = resolve_slot(
        ["A", "NOT_IN_ENUM"],
        _spec("list", enum_values=("A", "D")),
        domain=None,
    )
    assert isinstance(out, SlotBinding)
    assert out.value == ["A", "NOT_IN_ENUM"]  # enum membership NOT enforced


# -- optional presence ------------------------------------------------------


def test_optional_absent_number_zero_is_present_not_omitted() -> None:
    # 0 is a real value, not "absent" — must bind, not omit.
    out = resolve_slot(0, _spec("string", required=False))
    assert isinstance(out, SlotBinding) and out.value == "0"


def test_optional_blank_string_is_omitted_but_empty_list_asks() -> None:
    # An empty string is "absent" → OmitSlot; an empty list is NOT absent (it is a
    # present-but-invalid IN set) → AskUser. Pins the absence boundary.
    assert isinstance(resolve_slot("", _spec("string", required=False)), OmitSlot)
    assert isinstance(resolve_slot([], _spec("list", required=False)), AskUser)
