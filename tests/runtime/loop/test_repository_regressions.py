"""Runtime defects exposed while migrating the complete repository suite."""

from dataclasses import replace

import pytest

from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.context.scope_filter import compute_scope_hash
from data_agent.runtime.loop.answer_rules import first_match
from data_agent.runtime.loop.proposal import deliverable_evidence
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import AnalysisState, TrackedIntent, TrailEntry, TurnMessage


def entry(call_id, tool, *, status="ok", provenance=frozenset(), **kwargs):
    return TrailEntry(
        turn_index=0,
        tool_call_id=call_id,
        tool_name=tool,
        args={},
        status=status,
        error_code=None,
        provenance=provenance,
        result_preview=None,
        result_full_ref=None,
        ts="t",
        **kwargs,
    )


def test_access_denial_supports_a_limitation_but_not_a_completed_answer():
    salary = entry("salary", "runQuery")
    denied = replace(
        entry("ssn", "runQuery", status="denied", provenance=None),
        error_code="COLUMN_SCOPE_VIOLATION",
    )
    state = AnalysisState(
        turn_index=0,
        intents=(
            TrackedIntent(
                intent_id="i1",
                description="Salary",
                status="completed",
                evidence_tool_call_id="salary",
            ),
            TrackedIntent(
                intent_id="i2",
                description="SSN",
                status="blocked",
                evidence_tool_call_id="ssn",
                reason_code="NO_ACCESS",
            ),
        ),
    )
    rows, error = deliverable_evidence(
        state, {"answer": "Salary is available; I cannot provide SSN."}, [salary, denied]
    )
    assert error is None
    assert rows[0]["evidence"] == [{"result_id": "salary", "kind": "warehouse"}]
    assert rows[1]["evidence"] == []
    assert rows[1]["limitations"] == [{"result_id": "ssn", "reason_code": "NO_ACCESS"}]
    invalid = replace(
        state, intents=(replace(state.intents[1], status="completed", reason_code=None),)
    )
    assert deliverable_evidence(invalid, {"answer": "Here is the SSN."}, [denied])[1]
    assert deliverable_evidence(state, {"answer": "Salary only."}, [salary])[1]


def test_a_decline_does_not_bypass_numeric_grounding():
    assert first_match("I could not run a query for that.", (), "headcount") is None
    assert (
        first_match("I could not verify it, but there are 412 employees.", (), "headcount").name
        == "ungrounded_quantity"
    )


async def test_final_proposal_arguments_do_not_replay_after_a_scope_change():
    store = InMemorySessionStore()
    narrow = frozenset({"db.employee.name"})
    # A finalizer can precede the query in a batch; its own empty confirmation
    # provenance must not permit the original draft to escape the batch scope.
    proposal = replace(
        entry("final", "answerWithText"),
        args={"answer": "Secret salary 123456"},
        model_response={
            "id": "batch",
            "scope_hash": compute_scope_hash(frozenset()),
            "content": "Secret salary 123456",
            "tool_calls": [],
        },
    )
    await store.append_message(
        "scope", TurnMessage(turn_index=0, role="user", content="Salary?", ts="a")
    )
    await store.append_trail_entry("scope", proposal)
    rebuilt = await ContextAssembler(store).assemble("scope", narrow, current_turn_index=0)
    assert "123456" not in str(rebuilt.messages)
    assert "Salary?" in str(rebuilt.messages)
    # The stored proposal is retained for auditing and UI history projection.
    assert (await store.load_trail("scope"))[0].args["answer"] == "Secret salary 123456"


async def test_omitted_results_cannot_corroborate_the_repaired_answer():
    from tests.runtime.test_harness_improvements import CREDS, build

    loop, store, *_ = build([])
    ref = await store.write_full_result(CREDS.session_id, "bad", {"rows": [[9184]]})
    await store.append_trail_entry(
        CREDS.session_id, replace(entry("bad", "runQuery"), result_full_ref=ref)
    )
    rendered, _, usable = await loop._judge_results(
        CREDS.session_id, 0, frozenset(), exclude_result_ids=frozenset({"bad"})
    )
    assert rendered == ()
    assert usable == ()
    assert (
        await loop._corroborated_figures(CREDS.session_id, 0, "There are 9,184 people.", usable)
        is None
    )


@pytest.mark.parametrize("code", ["TOOL_NOT_EXECUTED", "INVALID_TOOL_ARGUMENTS"])
@pytest.mark.parametrize("current_turn", [None, 0])
async def test_failed_batch_arguments_do_not_replay_after_scope_change(code, current_turn):
    from data_agent.runtime.context.scope_filter import filter_trail

    store = InMemorySessionStore()
    receipt = replace(
        entry("skipped", "runQuery", status="error"),
        args={"sql": "SELECT 123456 AS previously_observed_salary"},
        error_code=code,
        model_response={"id": "batch", "scope_hash": compute_scope_hash(frozenset())},
    )
    await store.append_trail_entry("scope", receipt)
    narrow = frozenset({"db.employee.name"})
    # Empty result provenance does not cover values copied into proposed arguments.
    assert filter_trail([receipt], narrow, current_turn_index=current_turn) == []
    rebuilt = await ContextAssembler(store).assemble(
        "scope", narrow, current_turn_index=current_turn
    )
    assert "123456" not in str(rebuilt.messages)
    # Same-scope repair still receives the failed call and its receipt.
    assert filter_trail([receipt], frozenset(), current_turn_index=current_turn) == [receipt]
