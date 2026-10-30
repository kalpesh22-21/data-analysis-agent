import pytest

from data_agent.runtime.blueprint.executor import (
    _dumps_completed,
    _node_record,
    _rehydrate_completed,
)
from data_agent.runtime.provenance.freshness import (
    capture_freshness,
    ingestion_date,
    merge_freshness,
)
from data_agent.runtime.session_history import project_history
from tests.runtime.test_answer_document import final, section
from tests.runtime.test_harness_improvements import CREDS, Judge, build, discovery, query, run

DATE = "2026-10-12T05:30:00.000Z"
NOW = "2026-10-30T12:00:00.000Z"
LIVE = {"mode": "live", "sources": [{"table": "dbpcm_warehouse.employee", "kind": "live"}]}
PAY = {
    "mode": "ingested",
    "sources": [{"table": "dbpcm_warehouse.payroll", "kind": "ingested", "data_as_of": DATE}],
}
MIXED = {"mode": "mixed", "sources": LIVE["sources"] + PAY["sources"]}


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {"mode": []},
        {"mode": "live", "sources": [{"table": "x", "kind": []}]},
        {"mode": "live", "sources": [{"table": "x", "kind": "ingested"}]},
        {"mode": "live", "sources": [{"table": "x", "kind": "live", "data_as_of": DATE}]},
    ],
)
def test_invalid_optional_receipt_cannot_break_query(bad):
    assert capture_freshness({"freshness": bad}) is None


def test_live_does_not_erase_ingestion_date_and_unknown_does():
    merged = merge_freshness([LIVE, PAY])
    assert merged == MIXED
    assert ingestion_date(merged) == DATE
    assert ingestion_date(merge_freshness([LIVE, PAY, None])) is None
    assert ingestion_date(LIVE) is None
    later = {"mode": "ingested", "sources": [{**PAY["sources"][0], "data_as_of": NOW}]}
    assert ingestion_date(merge_freshness([PAY, later])) == DATE


def test_blueprint_checkpoint_preserves_live_and_ingested_receipts():
    nodes = {
        0: _node_record({}, frozenset(), "SELECT 1", freshness=LIVE, retrieved_at=NOW),
        1: _node_record(
            {}, frozenset(), "SELECT 2", freshness=PAY, retrieved_at=NOW, data_as_of=DATE
        ),
    }
    restored = _rehydrate_completed(_dumps_completed(nodes))
    assert restored == nodes
    assert ingestion_date(merge_freshness([r["freshness"] for r in restored.values()])) == DATE


@pytest.mark.parametrize("receipt,expected", [(LIVE, None), (PAY, DATE), (MIXED, DATE)])
async def test_api_receipt_reaches_judge_document_and_scoped_history(receipt, expected):
    judge = Judge()
    loop, store, *_ = build(
        [discovery(), query(), final(section())],
        judge,
        rows=[
            {
                "columns": ["Department", "n"],
                "rows": [["Sales", 120]],
                "row_count": 1,
                "freshness": receipt,
                "retrieved_at": NOW,
                "data_as_of": NOW,
            }
        ],
    )
    outcome = await run(loop)
    source = outcome.answer_document["sources"]["q"]
    assert source["freshness"] == receipt
    assert source["retrieved_at"] == NOW
    assert source.get("data_as_of") == expected  # never trust a contradictory top-level date
    assert judge.briefs[0].answer_document["sources"]["q"] == source
    doc = await store.get_or_create_session(CREDS.session_id)
    history = project_history(doc.messages, doc.tool_trail, CREDS.column_scope, None)
    assert history["turns"][0]["answer_document"] == outcome.answer_document
    suppressed = project_history(
        doc.messages, doc.tool_trail, CREDS.column_scope, None, data_as_of_enabled=False
    )
    assert "freshness" not in suppressed["turns"][0]["answer_document"]["sources"]["q"]
    assert source["freshness"] == receipt
