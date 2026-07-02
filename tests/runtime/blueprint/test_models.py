"""Layer-1: the blueprint DAG parse layer (runblueprint-design §2.1).

Structural, fail-loud parsing of the additive JSON into typed value objects.
"""

from __future__ import annotations

import pytest

from data_agent.runtime.blueprint.models import (
    Blueprint,
    BlueprintParseError,
    Node,
    ResultGrain,
    SlotSpec,
    WhenClause,
)


def test_parse_single_node_blueprint() -> None:
    bp = Blueprint.parse(
        id="bp1",
        intent="x",
        resolves={"salary": "AnnualSalary"},
        slots=[{"name": "department", "type": "string", "required": True,
                "binds_to": "db.t.Department"}],
        uses_rules=["active_employee"],
        sql_template="SELECT Department FROM db.t WHERE Department = {department}",
        result_grain=["Department"],
    )
    assert bp.is_single_node
    assert bp.slot("department").binds_to == "db.t.Department"
    assert bp.result_grain.columns == ("Department",)
    assert bp.resolves == {"salary": "AnnualSalary"}


def test_slot_unknown_type_rejected() -> None:
    with pytest.raises(BlueprintParseError):
        SlotSpec.parse({"name": "s", "type": "wat"})


def test_enum_slot_requires_values() -> None:
    with pytest.raises(BlueprintParseError):
        SlotSpec.parse({"name": "s", "type": "enum"})


def test_slot_missing_name_rejected() -> None:
    with pytest.raises(BlueprintParseError):
        SlotSpec.parse({"type": "string"})


def test_when_clause_parse_and_bad_on_violation() -> None:
    wc = WhenClause.parse({"expr": "count($1) > 0", "on_violation": "skip"})
    assert wc.on_violation == "skip"
    with pytest.raises(BlueprintParseError):
        WhenClause.parse({"expr": "count($1) > 0", "on_violation": "explode"})


def test_node_parse_and_bad_output_kind() -> None:
    node = Node.parse(
        {"order": 1, "node_kind": "query", "feeds_from": [], "output": {"x": "scalar"}}
    )
    assert node.order == 1 and node.output == {"x": "scalar"}
    with pytest.raises(BlueprintParseError):
        Node.parse({"order": 2, "output": {"x": "matrix"}})


def test_node_bad_node_kind_rejected() -> None:
    with pytest.raises(BlueprintParseError):
        Node.parse({"order": 1, "node_kind": "guard"})  # D59c cut `guard`


def test_result_grain_dict_form_with_verifiable_flag() -> None:
    rg = ResultGrain.parse({"columns": ["EmployeeCode"], "verifiable": False})
    assert rg.columns == ("EmployeeCode",) and rg.verifiable is False


def test_result_grain_bad_shape_rejected() -> None:
    with pytest.raises(BlueprintParseError):
        ResultGrain.parse({"columns": [1, 2]})
    with pytest.raises(BlueprintParseError):
        ResultGrain.parse("EmployeeCode")


def test_resolves_must_be_string_map() -> None:
    with pytest.raises(BlueprintParseError):
        Blueprint.parse(id="b", intent="x", resolves={"a": 5})
