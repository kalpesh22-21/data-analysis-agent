"""Layer-1: the D49 deterministic slot resolvers (runblueprint-design §3.2).

Resolvers are PURE code — NO LLM. The resolver type matrix; multi-match / no-match
/ fuzzy → an `AskUser` signal (never a guess); presence rules.
"""

from __future__ import annotations

from data_agent.runtime.blueprint.models import SlotSpec
from data_agent.runtime.blueprint.slots import AskUser, OmitSlot, SlotBinding, resolve_slot


def _spec(type_: str, **kw: object) -> SlotSpec:
    return SlotSpec(name=kw.pop("name", "s"), type=type_, **kw)  # type: ignore[arg-type]


# -- presence ---------------------------------------------------------------


def test_required_absent_asks_user() -> None:
    out = resolve_slot(None, _spec("string", required=True))
    assert isinstance(out, AskUser) and out.reason == "missing"


def test_required_blank_string_asks_user() -> None:
    out = resolve_slot("   ", _spec("string", required=True))
    assert isinstance(out, AskUser) and out.reason == "missing"


def test_optional_absent_is_omitted() -> None:
    out = resolve_slot(None, _spec("string", required=False))
    assert isinstance(out, OmitSlot)


# -- string / entity --------------------------------------------------------


def test_string_no_domain_binds_normalized() -> None:
    out = resolve_slot("  Warehouse  ", _spec("string"))
    assert isinstance(out, SlotBinding) and out.value == "Warehouse"


def test_entity_exact_domain_match_binds() -> None:
    out = resolve_slot("Warehouse", _spec("entity"), domain=["Warehouse", "Sales"])
    assert isinstance(out, SlotBinding) and out.value == "Warehouse"


def test_entity_case_insensitive_single_match_binds_canonical() -> None:
    out = resolve_slot("warehouse", _spec("entity"), domain=["Warehouse", "Sales"])
    assert isinstance(out, SlotBinding) and out.value == "Warehouse"


def test_entity_no_match_asks_user() -> None:
    out = resolve_slot("Nope", _spec("entity"), domain=["Warehouse", "Sales"])
    assert isinstance(out, AskUser) and out.reason == "no_match"
    assert out.options == ["Sales", "Warehouse"]


def test_entity_multi_match_asks_user() -> None:
    # No EXACT match for "ware", but two case-insensitive matches → ambiguous.
    out = resolve_slot("ware", _spec("entity"), domain=["Ware", "WARE", "Sales"])
    assert isinstance(out, AskUser) and out.reason == "multi_match"


# -- enum -------------------------------------------------------------------


def test_enum_valid_binds() -> None:
    out = resolve_slot("A", _spec("enum", enum_values=("A", "D")))
    assert isinstance(out, SlotBinding) and out.value == "A"


def test_enum_case_insensitive_recovers() -> None:
    out = resolve_slot("a", _spec("enum", enum_values=("A", "D")))
    assert isinstance(out, SlotBinding) and out.value == "A"


def test_enum_invalid_asks_user_with_options() -> None:
    out = resolve_slot("Z", _spec("enum", enum_values=("A", "D")))
    assert isinstance(out, AskUser) and out.reason == "no_match"
    assert out.options == ["A", "D"]


# -- period / as_of_date ----------------------------------------------------


def test_period_deictic_asks_user_never_guesses() -> None:
    for token in ("latest", "current", "last", "next"):
        out = resolve_slot(token, _spec("period"), domain=["2026-05-31", "2026-06-30"])
        assert isinstance(out, AskUser) and out.reason == "fuzzy"


def test_period_no_domain_asks_user() -> None:
    out = resolve_slot("May 2026", _spec("period"))
    assert isinstance(out, AskUser) and out.reason == "fuzzy"


def test_period_explicit_single_match_binds() -> None:
    out = resolve_slot("2026-05-31", _spec("period"), domain=["2026-05-31", "2026-06-30"])
    assert isinstance(out, SlotBinding) and out.value == "2026-05-31"


def test_period_no_match_asks_user() -> None:
    out = resolve_slot("2099-01-01", _spec("period"), domain=["2026-05-31"])
    assert isinstance(out, AskUser) and out.reason == "no_match"


# -- list / IN --------------------------------------------------------------


def test_list_all_resolve_binds_list() -> None:
    out = resolve_slot(["OT", "PT"], _spec("list"), domain=["OT", "PT", "REG"])
    assert isinstance(out, SlotBinding) and out.value == ["OT", "PT"]


def test_list_one_bad_element_asks_user() -> None:
    out = resolve_slot(["OT", "ZZ"], _spec("list"), domain=["OT", "PT"])
    assert isinstance(out, AskUser) and out.reason == "no_match"


def test_list_scalar_wrapped_binds() -> None:
    out = resolve_slot("OT", _spec("list"), domain=["OT", "PT"])
    assert isinstance(out, SlotBinding) and out.value == ["OT"]
