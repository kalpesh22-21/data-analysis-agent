"""Layer-1: the additive full-DAG read path (runblueprint-design §1.3).

`getBlueprint` expands the stored DAG additively; `map_blueprint_detail_record`
JSON-decodes the new properties fail-soft; a DAG-less blueprint renders the
byte-identical FOUND shape (extension is strictly additive); the non-oracle
{found:false} posture is unchanged.
"""

from __future__ import annotations

import json
from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.tools import GetBlueprintTool
from data_agent.runtime.retrieval.vector_index import (
    FakeVectorIndex,
    Neo4jVectorIndex,
    map_blueprint_detail_record,
)

_A = "dbpcm_warehouse.employee.Department"
_B = "dbpcm_warehouse.employee.EmployeeCode"


def _creds(scope: frozenset[str]) -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s", jwt="j", column_scope=scope)


def _dag_detail() -> BlueprintDetail:
    return BlueprintDetail(
        id="bp-x",
        intent="Average annual salary by department",
        slots_summary="department",
        uses=frozenset({_A, _B}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="sha",
        resolves={"salary": "AnnualSalary"},
        slots=[{"name": "department", "type": "string", "required": True}],
        uses_rules=["active_employee"],
        sql_template="SELECT Department FROM t WHERE Department = {department}",
        composes=None,
        result_grain=["Department"],
    )


def _bare_detail() -> BlueprintDetail:
    return BlueprintDetail(
        id="bp-legacy",
        intent="x",
        slots_summary="",
        uses=frozenset({_A, _B}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
    )


# -- record mapping ---------------------------------------------------------


def test_map_decodes_additive_dag_json_properties() -> None:
    record = {
        "id": "bp-x",
        "uses": [_A, _B],
        "resolves_json": json.dumps({"salary": "AnnualSalary"}),
        "slots_json": json.dumps([{"name": "department", "type": "string"}]),
        "uses_rules_json": json.dumps(["active_employee"]),
        "sql_template": "SELECT 1",
        "composes_json": None,
        "result_grain_json": json.dumps(["Department"]),
    }
    detail = map_blueprint_detail_record(record)
    assert detail.resolves == {"salary": "AnnualSalary"}
    assert detail.slots == [{"name": "department", "type": "string"}]
    assert detail.uses_rules == ["active_employee"]
    assert detail.sql_template == "SELECT 1"
    assert detail.composes is None
    assert detail.result_grain == ["Department"]


def test_map_malformed_dag_json_degrades_to_none() -> None:
    detail = map_blueprint_detail_record(
        {"id": "b", "uses": [_A], "slots_json": "{not json", "result_grain_json": "[1,2"}
    )
    assert detail.slots is None
    assert detail.result_grain is None


def test_map_absent_dag_properties_are_none() -> None:
    detail = map_blueprint_detail_record({"id": "b", "uses": [_A]})
    assert detail.sql_template is None and detail.slots is None and detail.resolves is None


# -- getBlueprint expansion -------------------------------------------------


async def test_get_blueprint_renders_dag_fields_when_present() -> None:
    index = FakeVectorIndex(details={"bp-x": _dag_detail()})
    tool = GetBlueprintTool(vector_index=index)
    result = await tool.run({"id": "bp-x"}, _creds(frozenset({_A, _B})))
    rf = result.result_full
    assert rf["found"] is True
    assert rf["resolves"] == {"salary": "AnnualSalary"}
    assert rf["slots"] == [{"name": "department", "type": "string", "required": True}]
    assert rf["uses_rules"] == ["active_employee"]
    assert rf["sql_template"].startswith("SELECT Department")
    assert rf["result_grain"] == ["Department"]
    # `composes` was None → NOT rendered (strictly additive).
    assert "composes" not in rf


async def test_get_blueprint_dag_less_is_byte_identical_found_shape() -> None:
    index = FakeVectorIndex(details={"bp-legacy": _bare_detail()})
    tool = GetBlueprintTool(vector_index=index)
    result = await tool.run({"id": "bp-legacy"}, _creds(frozenset({_A, _B})))
    assert set(result.result_full) == {
        "found", "id", "intent", "slots_summary", "uses",
        "status", "drift_status", "hit_count", "catalog_sha",
    }


async def test_get_blueprint_out_of_scope_dag_blueprint_is_non_oracle() -> None:
    # Even with a full DAG, an out-of-scope blueprint returns the bare {found:false}
    # (the §3 non-oracle) — no DAG detail leaks.
    index = FakeVectorIndex(details={"bp-x": _dag_detail()})
    tool = GetBlueprintTool(vector_index=index)
    narrow = frozenset({_A})  # missing _B → uses ⊄ scope
    result = await tool.run({"id": "bp-x"}, _creds(narrow))
    assert result.result_full == {"found": False}


async def test_neo4j_get_blueprint_round_trips_dag_via_run_seam() -> None:
    async def _run(query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        assert "resolves_json" in query  # the extended projection is selected
        return [
            {
                "id": "bp-x", "intent": "x", "slots_summary": "department",
                "uses": [_A, _B], "status": "validated", "drift_status": "clean",
                "hit_count": 0, "catalog_sha": "",
                "resolves_json": json.dumps({"salary": "AnnualSalary"}),
                "slots_json": json.dumps([{"name": "department", "type": "string"}]),
                "uses_rules_json": None,
                "sql_template": "SELECT Department FROM t WHERE Department = {department}",
                "composes_json": None,
                "result_grain_json": json.dumps(["Department"]),
            }
        ]

    index = Neo4jVectorIndex(
        url="bolt://unused:7687", auth=("u", "p"), expected_model="m", driver=object()
    )
    index._run = _run  # type: ignore[method-assign, assignment]
    detail = await index.get_blueprint("bp-x")
    assert detail is not None
    assert detail.resolves == {"salary": "AnnualSalary"}
    assert detail.result_grain == ["Department"]
