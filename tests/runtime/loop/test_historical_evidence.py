"""Finalizer reuse requires a delivered earlier answer and scope-matching, successful
warehouse evidence. The result need not have been shown in that answer (#44 cited a
result its earlier answer used only as evidence, and lost a turn being sent to rerun it)."""

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
        "evidence_only",
        "rejected",
        "no_answer",
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
    if fault != "no_answer":
        shown = fault != "evidence_only"
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
                    "sections": [{"evidence_refs": ["prior"] if shown else []}],
                    "sources": {"prior": {"sql": entry.args["sql"]}} if shown else {},
                },
            ),
        )
    args = {"evidence": ["prior"], "sections": [], "answer": "prior"}
    result, sql = await bind_historical_evidence(store, "s", 1, scope, args)
    trail = await store.load_trail("s")
    assert args["evidence"] == ["prior"]
    assert result["answer"] == "prior"  # Never substitute prose.
    assert trail[0] == entry
    if fault in {"none", "evidence_only"}:
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


async def test_an_evidence_only_blueprint_result_is_bound_with_its_terminal_sql():
    store = InMemorySessionStore()
    ref = await store.write_full_result("s", "bp", {"blueprint_id": "bp-headcount", "terminal_sql": "SELECT 1 AS n"})
    entry = original(tool_call_id="bp", tool_name="runBlueprint", args={"blueprint_id": "bp-headcount"},
                     authoritative=True, result_full_ref=ref)
    await store.append_trail_entry("s", entry)
    await store.append_message("s", TurnMessage(
        turn_index=0, role="assistant", content="Headcount 1", ts=entry.ts, provenance=entry.provenance,
        review={"status": "approved"}, answer_document={"sections": [], "sources": {}}))
    result, sql = await bind_historical_evidence(store, "s", 1, SCOPE, {"evidence": ["bp"], "sections": []})
    reference = (await store.load_trail("s"))[-1]
    assert result["evidence"] == [reference.tool_call_id]
    assert sql == {reference.tool_call_id: "SELECT 1 AS n"}
    assert reference.model_response["reused_from_turn"] == 0


async def test_a_blueprint_result_without_recoverable_sql_is_left_for_the_ordinary_rejection():
    store = InMemorySessionStore()
    entry = original(tool_call_id="bp", tool_name="runBlueprint", args={}, authoritative=True)
    await store.append_trail_entry("s", entry)
    await store.append_message("s", TurnMessage(
        turn_index=0, role="assistant", content="Headcount 1", ts=entry.ts, provenance=entry.provenance,
        review={"status": "approved"}, answer_document={"sections": [], "sources": {}}))
    result, sql = await bind_historical_evidence(store, "s", 1, SCOPE, {"evidence": ["bp"]})
    assert result["evidence"] == ["bp"] and sql == {}
    assert len(await store.load_trail("s")) == 1


async def test_a_followup_showing_an_earlier_evidence_only_result_needs_no_extra_turn():
    """#44: turn 0 delivered an answer that used `q2` only as evidence; turn 1 showed `q2`.
    The finalizer is accepted first time, and the judge reviews it as a reused snapshot."""
    from tests.runtime.test_answer_document import section
    from tests.runtime.test_harness_improvements import (
        SQL,
        Judge,
        batch,
        build,
        call,
        discovery,
        query,
        run,
    )

    rows = {"columns": ["Department", "n"], "rows": [["Sales", 120]], "row_count": 1, "truncated": False}
    shown_later = {**section(), "evidence_refs": ["q2"], "presentation": {"kind": "table", "result_id": "q2"}}
    judge = Judge()
    loop, store, model, mcp, _ = build(
        [discovery(), query(), batch(call("runQuery", "q2", sql=SQL + " LIMIT 1")),
         batch(call("finalizeAnswer", "answer", takeaway="Counts.", sections=[section()], evidence=["q", "q2"])),
         batch(call("finalizeAnswer", "followup", takeaway="Counts again.", sections=[shown_later], evidence=["q2"]))],
        judge, rows=[rows, rows],
    )
    await run(loop)
    second = await run(loop, "Show the second result too.")
    assert second.review["status"] == "approved" and second.review["attempts"] == 1
    assert len(mcp.calls) == 2  # nothing ran again
    rid = second.answer_document["sections"][0]["presentation"]["result_id"]
    assert rid.startswith("reused_")
    reviewed = next(r for r in judge.briefs[-1].results if r["tool_call_id"] == rid)
    assert reviewed["window_note"].startswith("Reused earlier result snapshot")
    doc = await store.get_or_create_session("harness-test")
    assert not any(e.error_code == "ANSWER_TABLE_RESULT_INVALID" for e in doc.tool_trail)


def test_an_earlier_result_that_cannot_be_reused_says_to_run_it_again():
    from data_agent.runtime.loop.agent_loop import _answer_table_result_invalid

    earlier = _answer_table_result_invalid("chatcmpl-tool-ac0e", [], earlier_turn=True).user_message
    assert "from an earlier turn that cannot be reused here" in earlier
    assert "otherwise run that query again in this turn" in earlier and "do not rerun" not in earlier
    current = _answer_table_result_invalid("chatcmpl-tool-ac0e", []).user_message
    assert "do not rerun completed analysis" in current


@pytest.mark.parametrize("exclude_alias", [False, True])
async def test_current_turn_exclusions_cannot_be_bypassed_by_history(exclude_alias):
    from data_agent.runtime.composite.analysis_state import previous_result_reference
    from data_agent.runtime.context.scope_filter import compute_scope_hash
    from data_agent.runtime.loop.historical_evidence import bind_recovery_history

    store = InMemorySessionStore()
    entry = original()
    await store.append_trail_entry("s", entry)
    alias = previous_result_reference(entry, 1, compute_scope_hash(SCOPE))
    await store.append_trail_entry("s", alias)
    await store.append_message("s", TurnMessage(
        turn_index=1, role="assistant", content="Total 100", ts=entry.ts,
        provenance=entry.provenance, review={"status": "approved"},
        answer_document={"sections": [{"evidence_refs": [alias.tool_call_id]}],
                         "sources": {alias.tool_call_id: {"sql": entry.args["sql"]}}},
    ))
    await store.write_review_state("s", 2, {
        "excluded_components": [{"result_id": alias.tool_call_id if exclude_alias else "prior"}]
    })
    for ref in ("prior", alias.tool_call_id):
        args, sql = await bind_historical_evidence(store, "s", 2, SCOPE, {"evidence": [ref]})
        assert args["evidence"] == [ref]
        assert sql == {}
    assert await bind_recovery_history(store, "s", 2, SCOPE) == {}
    assert len(await store.load_trail("s")) == 2


async def test_recovery_can_bind_latest_delivered_snapshot_without_agent_tool_call():
    from data_agent.runtime.loop.historical_evidence import bind_recovery_history

    store = InMemorySessionStore()
    entry = original()
    await store.append_trail_entry("s", entry)
    await store.append_message("s", TurnMessage(
        turn_index=0, role="assistant", content="Total 100", ts=entry.ts,
        provenance=entry.provenance, review={"status": "approved"},
        answer_document={"sections": [{"evidence_refs": ["prior"]}],
                         "sources": {"prior": {"sql": entry.args["sql"]}}},
    ))
    sql = await bind_recovery_history(store, "s", 1, SCOPE)
    reference = (await store.load_trail("s"))[-1]
    assert sql == {reference.tool_call_id: entry.args["sql"]}
    assert reference.turn_index == 1
    assert reference.ts == entry.ts
    assert "not freshly executed" in reference.window_note
    assert await bind_recovery_history(store, "s", 2, frozenset()) == {}


async def test_an_ask_user_pause_does_not_vouch_for_its_turn_s_results():
    """A paused clarification is persisted as approved, but it delivered no answer: a new
    question must not reuse that turn's results as if an answer had been reviewed."""
    from tests.runtime.test_harness_improvements import (
        CREDS,
        Judge,
        batch,
        build,
        call,
        discovery,
        query,
        run,
    )

    loop, store, *_ = build(
        [discovery(), query(),
         batch(call("askUser", "ask", question="Which status should I count?", options=["Active", "All"]))],
        Judge(),
    )
    assert (await run(loop)).status == "paused_ask_user"
    args, sql = await bind_historical_evidence(store, CREDS.session_id, 1, CREDS.column_scope, {"evidence": ["q"]})
    assert args["evidence"] == ["q"] and sql == {}


async def test_a_copy_omitted_in_one_turn_can_be_reconsidered_later_with_the_reason():
    """An omission protects only its own turn. A later turn may reuse the result, and its
    judge sees why the earlier judge left it out."""
    from data_agent.runtime.composite.analysis_state import previous_result_reference
    from data_agent.runtime.context.review_feedback import with_historical_feedback
    from data_agent.runtime.context.scope_filter import compute_scope_hash
    from data_agent.runtime.loop.answer_judge import JudgeVerdict
    from data_agent.runtime.loop.proposal import ReviewState

    store, entry = InMemorySessionStore(), original()
    await store.append_trail_entry("s", entry)
    await store.append_message("s", TurnMessage(turn_index=0, role="assistant", content="x", ts=entry.ts,
                               provenance=entry.provenance, review={"status": "rejected"}, answer_document={}))
    copy = previous_result_reference(entry, 1, compute_scope_hash(SCOPE))
    await store.append_trail_entry("s", copy)
    state = ReviewState(scope_hash=compute_scope_hash(SCOPE))
    state.reject(JudgeVerdict(False, "unsupported_by_evidence", "This table counts terminated staff; omit it.",
                              repair_type="omit_component", result_ids=(copy.tool_call_id,)), "v1")
    state.excluded_components = ({"kind": "table", "result_id": copy.tool_call_id},)
    await store.write_review_state("s", 1, state.to_doc())
    await store.append_message("s", TurnMessage(turn_index=1, role="assistant", content="y", ts=entry.ts,
                               provenance=entry.provenance, review={"status": "approved"},
                               answer_document={"sections": [], "sources": {}}))
    args, sql = await bind_historical_evidence(store, "s", 2, SCOPE, {"evidence": [copy.tool_call_id]})
    assert args["evidence"][0].startswith("reused_") and args["evidence"][0] in sql
    doc = await store.get_or_create_session("s")
    later = [e for e in with_historical_feedback(doc.tool_trail, doc, 2, SCOPE) if e.turn_index == 2]
    assert any("terminated staff" in (e.window_note or "") for e in later)


async def test_a_non_string_table_result_id_is_rejected_not_raised():
    from tests.runtime.test_harness_improvements import (
        CREDS,
        Judge,
        batch,
        build,
        call,
        discovery,
        query,
        run,
    )

    loop, store, *_ = build(
        [discovery(), query(),
         batch(call("answerWithTable", "t1", answer="Counts.", tables=[{"result_id": ["q"]}])),
         batch(call("finalizeAnswer", "a2", takeaway="Counts.", sections=[], evidence=["q"]))],
        Judge(),
    )
    assert (await run(loop)).review["status"] == "approved"
    doc = await store.get_or_create_session(CREDS.session_id)
    assert next(e for e in doc.tool_trail if e.tool_call_id == "t1").error_code == "ANSWER_TABLE_RESULT_INVALID"


ANSWERS = [
    ("Show the top departments by approved time-off request hours.", "Operations leads approved time-off hours.", "Approved time-off hours"),
    ("Show total paid expense amount.", "Paid expenses total $3,600.", "Paid expenses"),
    ("Show paid expense amounts by month.", "Monthly paid expenses rose.", "Paid expenses by month"),
    ("Show Active employee counts by department.", "Sales has 3 Active employees.", "Active employees"),
]


async def history(question):
    store = InMemorySessionStore()
    for turn, (request, takeaway, title) in enumerate(ANSWERS):
        entry = original(turn_index=turn, tool_call_id=f"r{turn}")
        await store.append_trail_entry("s", entry)
        await store.append_message("s", TurnMessage(turn_index=turn, role="user", content=request, ts=entry.ts))
        await store.append_message("s", TurnMessage(
            turn_index=turn, role="assistant", content=takeaway, ts=entry.ts,
            provenance=entry.provenance, review={"status": "approved"},
            answer_document={"takeaway": takeaway, "sections": [{"title": title, "evidence_refs": [f"r{turn}"]}],
                             "sources": {f"r{turn}": {"sql": entry.args["sql"]}}},
        ))
    await store.append_message("s", TurnMessage(turn_index=4, role="user", content=question, ts="2026-10-14T13:00:00Z"))
    return store


async def offered(store):
    from data_agent.runtime.loop.historical_evidence import bind_recovery_history

    await bind_recovery_history(store, "s", 4, SCOPE)
    return {e.model_response["reused_from_result_id"] for e in await store.load_trail("s") if e.turn_index == 4}


async def test_recovery_offers_the_earlier_answer_the_request_names():
    store = await history("Return to the approved time-off ranking from earlier and summarize the leading department.")
    # The latest answer and the matching older one; unrelated answers are not offered.
    assert await offered(store) == {"r3", "r0"}


async def test_recovery_offers_only_the_latest_answer_without_a_related_request():
    store = await history("Summarize that.")
    assert await offered(store) == {"r3"}
