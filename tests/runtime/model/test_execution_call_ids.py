"""Provider-local call IDs must not alias execution receipts across responses."""

import json

import pytest

from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.conversation import assign_unique_call_ids
from tests.runtime.test_harness_improvements import (
    CREDS,
    SQL,
    batch,
    build,
    call,
    discovery,
    finish,
    run,
)


def test_reused_and_duplicate_ids_are_unique_without_rewriting_evidence():
    used = {"functions.runQuery:0"}
    result = ModelTurnResult(
        tool_calls=[
            ToolCallRequest("functions.runQuery:0", "runQuery", {"sql": "SELECT 2"}),
            ToolCallRequest("fresh", "finalizeAnswer", {"evidence": ["functions.runQuery:0"]}),
            ToolCallRequest("fresh", "runQuery", {"sql": "SELECT 3"}),
        ],
        reasoning_metadata={"reasoning_content": "opaque provider state"},
    )
    normalized = assign_unique_call_ids(result, used)
    ids = [c.id for c in normalized.tool_calls]
    assert len(set(ids)) == 3
    assert "functions.runQuery:0" not in ids
    assert ids[1] == "fresh"
    assert normalized.tool_calls[1].arguments == {"evidence": ["functions.runQuery:0"]}
    assert normalized.reasoning_metadata == result.reasoning_metadata
    assert result.tool_calls[0].id == "functions.runQuery:0"


def test_unique_response_preserves_provider_metadata():
    result = ModelTurnResult(
        tool_calls=[ToolCallRequest("unique", "read", {})],
        reasoning_metadata={"reasoning_content": "original"},
    )
    assert assign_unique_call_ids(result, set()) is result


@pytest.mark.parametrize("pause", [False, True])
async def test_reused_provider_id_keeps_both_query_results_in_model_history(pause):
    pause_steps = (
        [
            batch(
                call(
                    "askUser", "clarify", question="Which period?", options=["Current", "Previous"]
                )
            )
        ]
        if pause
        else []
    )
    loop, store, _, _, _ = build(
        [
            discovery(),
            batch(call("runQuery", "q", sql=SQL)),
            *pause_steps,
            batch(call("runQuery", "q", sql="SELECT count() AS n FROM hr.employee")),
            batch(finish()),
        ],
        rows=[
            {"columns": ["n"], "rows": [[120]], "row_count": 1},
            {"columns": ["n"], "rows": [[125]], "row_count": 1},
        ],
    )
    out = await run(loop)
    if pause:
        assert out.status == "paused_ask_user"
        out = await loop.resume(session_id=CREDS.session_id, credentials=CREDS, answer="Current")
    assert out.status == "done"
    trail = await store.load_trail(CREDS.session_id)
    queries = [e for e in trail if e.tool_name == "runQuery"]
    assert len(queries) == 2
    assert queries[0].tool_call_id == "q"
    assert queries[1].tool_call_id != "q"
    # Rebuild using the production assembler/batch restoration path.
    request = await loop._build_canonical_messages(
        CREDS.session_id,
        CREDS.column_scope,
        0,
        question="Headcount?",
        user_id=None,
        retrieval_memo={},
        capability_memo={},
        withheld_call_ids=set(),
    )
    results = [
        json.loads(m["content"])
        for m in request.messages
        if m.get("role") == "tool" and m.get("tool_call_id") in {e.tool_call_id for e in queries}
    ]
    assert len(results) == 2
    assert [r["result_preview"]["preview_rows"] for r in results] == [[[120]], [[125]]]


def test_lineage_observer_prefetch_seed_and_reasoning_survive_collision():
    from data_agent.runtime.model.conversation import conversation_call_ids

    messages = [
        {"role": "assistant", "tool_calls": [{"id": "syn_prefetch"}]},
        {"role": "tool", "tool_call_id": "functions.read:0"},
    ]
    used = conversation_call_ids(messages)
    events = []
    metadata = {
        "reasoning_content": "opaque",
        "reasoning_details": [{"id": "functions.read:0", "data": "unchanged"}],
    }
    result = ModelTurnResult(
        tool_calls=[
            ToolCallRequest("syn_prefetch", "listTables", {}),
            ToolCallRequest("functions.read:0", "getTableSchema", {}),
            ToolCallRequest("functions.read:0#1", "getBlueprint", {}),
            ToolCallRequest("", "listDatabases", {}),
        ],
        reasoning_metadata=metadata,
    )
    normalized = assign_unique_call_ids(result, used, lambda e, p: events.append((e, p)))
    assert [c.id for c in normalized.tool_calls] == [
        "syn_prefetch#1",
        "functions.read:0#2",
        "functions.read:0#1",
        "call#1",
    ]
    assert len(events) == 3
    assert all(e == "loop_tool_call_id_reminted" for e, _ in events)
    assert events[1][1]["old_id"] == "functions.read:0"
    assert events[1][1]["new_id"] == "functions.read:0#2"
    assert normalized.reasoning_metadata is metadata
