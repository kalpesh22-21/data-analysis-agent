"""Layer-1 tests for FakeVectorIndex (design §3.1) — cosine recall, kind filter,
plus the blueprint RECALL-row mapper (`map_blueprint_record`) including the
release-1 §02 card-enrichment properties.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.retrieval.models import Candidate
from data_agent.runtime.retrieval.vector_index import (
    _BLUEPRINT_RECALL_QUERY,
    FakeVectorIndex,
    map_blueprint_record,
)


def _c(id: str, kind: str, vec: list[float]) -> tuple[Candidate, list[float]]:
    return (Candidate(id=id, kind=kind, text=id, uses=None), vec)  # type: ignore[arg-type]


async def test_recall_filters_by_kind() -> None:
    index = FakeVectorIndex(
        [_c("bp", "blueprint", [1.0, 0.0]), _c("kn", "knowledge", [1.0, 0.0])]
    )
    got = await index.recall(query_vector=[1.0, 0.0], kind="blueprint", k=10)
    assert [c.id for c in got] == ["bp"]


async def test_recall_orders_by_descending_cosine_and_sets_score() -> None:
    index = FakeVectorIndex(
        [
            _c("near", "blueprint", [1.0, 0.0]),
            _c("mid", "blueprint", [1.0, 1.0]),
            _c("far", "blueprint", [0.0, 1.0]),
        ]
    )
    got = await index.recall(query_vector=[1.0, 0.0], kind="blueprint", k=10)
    assert [c.id for c in got] == ["near", "mid", "far"]
    assert got[0].score is not None and got[0].score > (got[2].score or 0.0)


async def test_recall_respects_k() -> None:
    index = FakeVectorIndex([_c(f"c{i}", "blueprint", [1.0, i / 10]) for i in range(5)])
    got = await index.recall(query_vector=[1.0, 0.0], kind="blueprint", k=2)
    assert len(got) == 2


async def test_recall_fail_flag_returns_empty() -> None:
    index = FakeVectorIndex([_c("bp", "blueprint", [1.0, 0.0])], fail=True)
    assert await index.recall(query_vector=[1.0, 0.0], kind="blueprint", k=10) == []


# ---------------------------------------------------------------------------
# map_blueprint_record — card enrichment (release-1 §02 changes 1 + 2)
# ---------------------------------------------------------------------------


_BASE_ROW: dict[str, Any] = {
    "id": "bp-overtime",
    "text": "Earnings by department",
    "slots_summary": "department, pay_period",
    "uses": ["dbpcm_warehouse.payroll.amount"],
    "score": 0.9,
}


def _row(**props: Any) -> dict[str, Any]:
    return {**_BASE_ROW, **props}


def test_recall_query_selects_the_three_enrichment_props() -> None:
    # The enrichment data is ALREADY on the node; the whole change is selecting
    # it. Pin the RETURN clause so a future edit cannot quietly drop a prop and
    # leave every card silently un-enriched (no error, no other test failure).
    for prop in ("node.resolves_json", "node.slots_json", "node.result_grain_json"):
        assert prop in _BLUEPRINT_RECALL_QUERY
    # And the recall trust/eligibility gates are untouched by the enrichment.
    assert "node.source = 'mcp'" in _BLUEPRINT_RECALL_QUERY
    assert "coalesce(node.status, 'validated') = 'validated'" in _BLUEPRINT_RECALL_QUERY


def test_map_blueprint_record_decodes_the_three_props_into_payload() -> None:
    candidate = map_blueprint_record(
        _row(
            resolves_json='{"salary":"annual_salary"}',
            slots_json='[{"name":"department","type":"string","required":true,'
            '"binds_to":"dbpcm_warehouse.employee.department_name"}]',
            result_grain_json='["Department"]',
        )
    )
    assert candidate.payload["resolves"] == {"salary": "annual_salary"}
    # The payload carries the RAW authored slots (binds_to included) — the
    # {name,type,required} projection is enforced downstream in _to_thin_card.
    assert candidate.payload["slots"] == [
        {
            "name": "department",
            "type": "string",
            "required": True,
            "binds_to": "dbpcm_warehouse.employee.department_name",
        }
    ]
    assert candidate.payload["result_grain"] == ["Department"]
    # The pre-existing payload keys are unchanged.
    assert candidate.payload["intent"] == "Earnings by department"
    assert candidate.payload["slots_summary"] == "department, pay_period"
    assert candidate.uses == frozenset({"dbpcm_warehouse.payroll.amount"})


def test_map_blueprint_record_result_grain_dict_form_is_carried_raw() -> None:
    # A `{columns, verifiable}` grain is legal; the payload carries it as stored
    # and the coercion to the column tuple happens on the card.
    candidate = map_blueprint_record(
        _row(result_grain_json='{"columns":["Department"],"verifiable":false}')
    )
    assert candidate.payload["result_grain"] == {"columns": ["Department"], "verifiable": False}


def test_map_blueprint_record_corrupt_json_degrades_to_none_without_raising() -> None:
    candidate = map_blueprint_record(
        _row(resolves_json="{not valid", slots_json="[oops", result_grain_json="<<<")
    )
    assert candidate.payload["resolves"] is None
    assert candidate.payload["slots"] is None
    assert candidate.payload["result_grain"] is None
    # The row still maps — a corrupt DAG prop degrades the CARD, never the recall.
    assert candidate.id == "bp-overtime"
    assert candidate.score == 0.9


def test_map_blueprint_record_wrong_json_type_is_coerced_to_none() -> None:
    # `resolves` must be an object and `slots` a list; the wrong shape is None,
    # never a mis-typed surface the card projection would have to defend against.
    candidate = map_blueprint_record(
        _row(resolves_json='["a","b"]', slots_json='{"x":1}', result_grain_json='"Department"')
    )
    assert candidate.payload["resolves"] is None
    assert candidate.payload["slots"] is None
    assert candidate.payload["result_grain"] is None


def test_map_blueprint_record_absent_props_map_like_a_dag_less_blueprint() -> None:
    candidate = map_blueprint_record(dict(_BASE_ROW))
    assert candidate.payload["resolves"] is None
    assert candidate.payload["slots"] is None
    assert candidate.payload["result_grain"] is None
