"""Finalizer reuse requires delivered, scope-matching, successful warehouse evidence."""

from dataclasses import replace

import pytest

from data_agent.runtime.loop.historical_evidence import bind_historical_evidence
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import TurnMessage
from tests.runtime.composite.test_previous_result_reuse import SCOPE, original


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "rejected",
        "not_delivered",
        "unknown_scope",
        "changed_scope",
        "unknown_provenance",
        "denied",
        "scratch",
    ],
)
async def test_only_authorized_delivered_receipts_can_be_bound(fault):
    store = InMemorySessionStore()
    entry = original()
    scope = SCOPE
    if fault == "unknown_scope":
        entry = replace(entry, model_response=None)
    elif fault == "changed_scope":
        scope = frozenset()
    elif fault == "unknown_provenance":
        entry = replace(entry, provenance=None)
    elif fault == "denied":
        entry = replace(entry, status="denied", error_code="SQL_EXECUTION_ERROR")
    elif fault == "scratch":
        entry = replace(entry, provenance=frozenset({("scratch.temp", "salary")}))
    await store.append_trail_entry("s", entry)
    await store.append_message(
        "s",
        TurnMessage(
            turn_index=0,
            role="assistant",
            content="Total 100",
            ts=entry.ts,
            provenance=entry.provenance,
            review={"status": "rejected" if fault == "rejected" else "approved"},
            answer_document={
                "sections": [{"evidence_refs": [] if fault == "not_delivered" else ["prior"]}],
                "sources": {"prior": {"sql": entry.args["sql"]}},
            },
        ),
    )
    args = {"evidence": ["prior"], "sections": [], "answer": "prior"}
    result, sql = await bind_historical_evidence(store, "s", 1, scope, args)
    trail = await store.load_trail("s")
    assert args["evidence"] == ["prior"]
    assert result["answer"] == "prior"  # Never substitute prose.
    assert trail[0] == entry
    if fault == "none":
        assert len(trail) == 2
        assert sql == {trail[1].tool_call_id: entry.args["sql"]}
        assert result["evidence"] == [trail[1].tool_call_id]
        assert trail[1].ts == entry.ts
        again, again_sql = await bind_historical_evidence(store, "s", 1, scope, args)
        assert again == result
        assert again_sql == sql
        assert len(await store.load_trail("s")) == 2
        bound, _ = await bind_historical_evidence(
            store,
            "s",
            1,
            scope,
            {"deliverables": [{"intent_id": "i1", "result_ids": ["prior"]}]},
        )
        assert bound["deliverables"][0]["result_ids"] == result["evidence"]
    else:
        assert result["evidence"] == ["prior"]
        assert len(trail) == 1


async def test_repeated_followups_resolve_original_receipt_without_retrieval():
    store = InMemorySessionStore()
    entry = original()
    await store.append_trail_entry("s", entry)

    async def delivered(turn, rid):
        await store.append_message(
            "s",
            TurnMessage(
                turn_index=turn,
                role="assistant",
                content="Total 100",
                ts=entry.ts,
                provenance=entry.provenance,
                review={"status": "approved"},
                answer_document={
                    "sections": [{"evidence_refs": [rid]}],
                    "sources": {rid: {"sql": entry.args["sql"]}},
                },
            ),
        )

    await delivered(0, "prior")
    first, _ = await bind_historical_evidence(store, "s", 1, SCOPE, {"evidence": ["prior"]})
    await delivered(1, first["evidence"][0])
    second, sql = await bind_historical_evidence(store, "s", 2, SCOPE, first)
    assert second["evidence"] != first["evidence"]
    receipt = (await store.load_trail("s"))[-1]
    assert receipt.model_response["reused_from_result_id"] == "prior"
    assert receipt.ts == entry.ts
    assert sql == {receipt.tool_call_id: entry.args["sql"]}
