"""S2 loader — normalization + failed→fixed + askUser + blueprint usage + D46
full-result hydration + D72 read-only + determinism (matrix rows L1, L9–L14).
"""

from __future__ import annotations

import copy

import pytest

from data_agent.learning.summary import load_session_summary
from data_agent.learning.triage import triage
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import ResultPreview

from .helpers import make_doc, make_job, make_message, make_trail_entry


@pytest.fixture
def store() -> InMemorySessionStore:
    return InMemorySessionStore()


async def _load(store, doc, **job_kw):
    return await load_session_summary(doc, store, job=make_job(doc.session_id, **job_kw))


# --- L1: normalization + evidence-ref preservation --------------------------


async def test_normalization_preserves_refs_args_status_provenance(store):
    prov = frozenset({("hr.pay", "overtime"), ("hr.pay", "dept")})
    doc = make_doc(
        "sess-norm",
        messages=[make_message(0, "user", "overtime by dept?"),
                  make_message(0, "assistant", "Here it is.")],
        tool_trail=[
            make_trail_entry(turn_index=0, tool_call_id="call_A", tool_name="runQuery",
                             args={"sql": "SELECT dept, sum(ot) FROM hr.pay GROUP BY dept",
                                   "limit": 50},
                             status="ok", provenance=prov),
        ],
    )
    summary = await _load(store, doc, user_id="u9", scope_ref="scope-z", trace_id="t-7",
                          content_hash="h-42")

    # Reference envelope carried from the LearningJob.
    assert summary.session_id == "sess-norm"
    assert summary.user_id == "u9"
    assert summary.scope_ref == "scope-z"
    assert summary.trace_id == "t-7"
    assert summary.content_hash == "h-42"

    assert len(summary.tool_calls) == 1
    tc = summary.tool_calls[0]
    # tool_call_id → tool_call_ref and turn_index preserved (S3 evidence refs).
    assert tc.tool_call_ref == "call_A"
    assert tc.turn_index == 0
    assert tc.tool_name == "runQuery"
    assert tc.status == "ok"
    # args carried verbatim; sql convenience-extracted.
    assert tc.args == {"sql": "SELECT dept, sum(ot) FROM hr.pay GROUP BY dept", "limit": 50}
    assert tc.sql == "SELECT dept, sum(ot) FROM hr.pay GROUP BY dept"
    # provenance carried VERBATIM (not re-derived) — same value, including identity.
    assert tc.provenance == prov
    assert tc.provenance is doc.tool_trail[0].provenance

    # Turn grouping: user + assistant + the tool_call_ref in trail order.
    assert len(summary.turns) == 1
    turn = summary.turns[0]
    assert turn.turn_index == 0
    assert turn.user_nl == "overtime by dept?"
    assert turn.assistant_text == "Here it is."
    assert turn.tool_call_refs == ("call_A",)


async def test_provenance_none_carried_as_undetermined(store):
    doc = make_doc(
        tool_trail=[make_trail_entry(turn_index=0, tool_name="runQuery",
                                     args={"sql": "SELECT 1"}, status="ok", provenance=None)],
    )
    summary = await _load(store, doc)
    assert summary.tool_calls[0].provenance is None  # undetermined stays None


async def test_sql_extracted_only_for_sql_tools(store):
    doc = make_doc(
        tool_trail=[
            make_trail_entry(turn_index=0, tool_call_id="c1", tool_name="runQuery",
                             args={"sql": "SELECT 1"}, status="ok"),
            make_trail_entry(turn_index=0, tool_call_id="c2", tool_name="askUser",
                             args={"question": "which dept?"}, status="ok"),
            make_trail_entry(turn_index=0, tool_call_id="c3", tool_name="runBlueprint",
                             args={"blueprint_id": "bp-1", "sql": "SELECT 2"}, status="ok"),
        ],
    )
    summary = await _load(store, doc)
    by_ref = {tc.tool_call_ref: tc for tc in summary.tool_calls}
    assert by_ref["c1"].sql == "SELECT 1"       # runQuery → extracted
    assert by_ref["c2"].sql is None             # askUser → None
    assert by_ref["c3"].sql is None             # runBlueprint → SQL NOT lifted (S3/S4)
    # ...but runBlueprint's args are still carried verbatim.
    assert by_ref["c3"].args["sql"] == "SELECT 2"


async def test_multi_turn_grouping_and_ref_order(store):
    doc = make_doc(
        messages=[make_message(0, "user", "q0"), make_message(0, "assistant", "a0"),
                  make_message(1, "user", "q1"), make_message(1, "assistant", "a1")],
        tool_trail=[
            make_trail_entry(turn_index=0, tool_call_id="c0a", tool_name="runQuery",
                             args={"sql": "S0a"}, status="ok"),
            make_trail_entry(turn_index=1, tool_call_id="c1a", tool_name="runQuery",
                             args={"sql": "S1a"}, status="ok"),
            make_trail_entry(turn_index=1, tool_call_id="c1b", tool_name="runQuery",
                             args={"sql": "S1b"}, status="ok"),
        ],
    )
    summary = await _load(store, doc)
    turns = {t.turn_index: t for t in summary.turns}
    assert turns[0].tool_call_refs == ("c0a",)
    assert turns[1].tool_call_refs == ("c1a", "c1b")  # trail order preserved
    assert turns[1].user_nl == "q1"
    assert turns[1].assistant_text == "a1"


# --- L9: failed→fixed pairing -----------------------------------------------


async def test_failed_then_fixed_pair_detected(store):
    doc = make_doc(
        tool_trail=[
            make_trail_entry(turn_index=0, tool_call_id="bad", tool_name="runQuery",
                             args={"sql": "SELCT 1"}, status="error", error_code="SYNTAX"),
            make_trail_entry(turn_index=1, tool_call_id="good", tool_name="runQuery",
                             args={"sql": "SELECT 1"}, status="ok"),
        ],
    )
    summary = await _load(store, doc)
    assert len(summary.failed_fixed_sql) == 1
    pair = summary.failed_fixed_sql[0]
    assert pair.failed_tool_call_ref == "bad"
    assert pair.failed_sql == "SELCT 1"
    assert pair.fixed_tool_call_ref == "good"
    assert pair.fixed_sql == "SELECT 1"


async def test_failed_with_no_later_success_is_not_a_fix(store):
    doc = make_doc(
        tool_trail=[
            make_trail_entry(turn_index=0, tool_call_id="bad", tool_name="runQuery",
                             args={"sql": "SELCT 1"}, status="error", error_code="SYNTAX"),
        ],
    )
    summary = await _load(store, doc)
    assert summary.failed_fixed_sql == ()


async def test_denied_query_pairs_with_later_ok(store):
    doc = make_doc(
        tool_trail=[
            make_trail_entry(turn_index=0, tool_call_id="denied", tool_name="runQuery",
                             args={"sql": "SELECT salary"}, status="denied", error_code="SCOPE"),
            make_trail_entry(turn_index=1, tool_call_id="ok", tool_name="runQuery",
                             args={"sql": "SELECT count(*)"}, status="ok"),
        ],
    )
    summary = await _load(store, doc)
    assert len(summary.failed_fixed_sql) == 1
    assert summary.failed_fixed_sql[0].failed_tool_call_ref == "denied"


# --- L10: askUser pairing ---------------------------------------------------


async def test_askuser_paired_with_same_turn_answer(store):
    # REAL runtime shape (agent_loop.py:451-452 / couchbase_store.py:235): the
    # resume appends the user's answer at the SAME turn_index as the askUser call,
    # AFTER the originating question in message-list order — NOT turn+1.
    doc = make_doc(
        messages=[make_message(0, "user", "show pay"),          # originating question (turn 0)
                  make_message(0, "user", "the sales team")],   # askUser answer, SAME turn 0
        tool_trail=[make_trail_entry(turn_index=0, tool_call_id="ask1", tool_name="askUser",
                                     args={"question": "which department?"}, status="ok")],
    )
    summary = await _load(store, doc)
    assert len(summary.askuser_exchanges) == 1
    ex = summary.askuser_exchanges[0]
    assert ex.question_tool_call_ref == "ask1"
    assert ex.question == "which department?"
    # The originating question (list index 0) is NOT paired; the same-turn answer is.
    assert ex.answer == "the sales team"
    assert ex.answer_turn_index == 0


async def test_two_askusers_in_one_turn_pair_successively(store):
    # Two askUser round-trips in ONE turn: within the turn the user messages are,
    # in append order, [originating question, answer1, answer2]. answer1 pairs to
    # askUser#1, answer2 to askUser#2 (successive pairing, index 0 skipped).
    doc = make_doc(
        messages=[make_message(0, "user", "show pay"),        # originating question
                  make_message(0, "user", "the sales team"),  # answer to askUser#1
                  make_message(0, "user", "last month")],     # answer to askUser#2
        tool_trail=[
            make_trail_entry(turn_index=0, tool_call_id="ask1", tool_name="askUser",
                             args={"question": "which department?"}, status="ok"),
            make_trail_entry(turn_index=0, tool_call_id="ask2", tool_name="askUser",
                             args={"question": "which period?"}, status="ok"),
        ],
    )
    summary = await _load(store, doc)
    assert len(summary.askuser_exchanges) == 2
    ex1, ex2 = summary.askuser_exchanges
    assert ex1.question_tool_call_ref == "ask1"
    assert ex1.question == "which department?"
    assert ex1.answer == "the sales team"        # answer1 → askUser#1
    assert ex1.answer_turn_index == 0
    assert ex2.question_tool_call_ref == "ask2"
    assert ex2.question == "which period?"
    assert ex2.answer == "last month"            # answer2 → askUser#2
    assert ex2.answer_turn_index == 0


async def test_unanswered_askuser_has_none_answer(store):
    # Session ended right after the askUser: the turn holds ONLY the originating
    # question (index 0), no appended answer → answer=None (not the question).
    doc = make_doc(
        messages=[make_message(0, "user", "show pay")],  # only the originating question
        tool_trail=[make_trail_entry(turn_index=0, tool_call_id="ask1", tool_name="askUser",
                                     args={"question": "which department?"}, status="ok")],
    )
    summary = await _load(store, doc)
    assert summary.askuser_exchanges[0].answer is None
    assert summary.askuser_exchanges[0].answer_turn_index is None


async def test_answered_askuser_no_data_query_triages_keep_k3(store):
    """HIGH-1 regression: an answered-askUser session in the REAL same-turn shape,
    with NO ok data query, must now triage KEEP via K3 (it was falsely SKIP while
    the loader never paired the same-turn answer, so K3 never fired)."""
    doc = make_doc(
        messages=[make_message(0, "user", "how many people work here?"),  # originating question
                  make_message(0, "user", "just the sales team")],        # same-turn answer
        tool_trail=[make_trail_entry(turn_index=0, tool_call_id="ask1", tool_name="askUser",
                                     args={"question": "which department?"}, status="ok")],
    )
    summary = await _load(store, doc)

    # The clarification answer is present in the summary (precondition for K3).
    assert len(summary.askuser_exchanges) == 1
    assert summary.askuser_exchanges[0].answer == "just the sales team"

    verdict = triage(summary)
    assert verdict.decision == "keep"
    assert verdict.reason == "K3"
    assert "user_knowledge" in verdict.target_hints


# --- L11: blueprint usage outcome -------------------------------------------


async def test_blueprint_usage_accepted(store):
    doc = make_doc(
        messages=[make_message(0, "user", "run headcount"),
                  make_message(0, "assistant", "Done.")],
        tool_trail=[make_trail_entry(turn_index=0, tool_call_id="bp", tool_name="runBlueprint",
                                     args={"blueprint_id": "bp-headcount"}, status="ok")],
    )
    summary = await _load(store, doc)
    assert len(summary.blueprint_usages) == 1
    bp = summary.blueprint_usages[0]
    assert bp.blueprint_id == "bp-headcount"
    assert bp.status == "ok"
    assert bp.outcome == "accepted"


async def test_blueprint_usage_corrected_on_error_status(store):
    doc = make_doc(
        tool_trail=[make_trail_entry(turn_index=0, tool_call_id="bp", tool_name="runBlueprint",
                                     args={"blueprint_id": "bp-x"}, status="error",
                                     error_code="BP_FAIL")],
    )
    summary = await _load(store, doc)
    assert summary.blueprint_usages[0].outcome == "corrected"


async def test_blueprint_usage_corrected_on_trailing_correction(store):
    doc = make_doc(
        messages=[make_message(0, "user", "run headcount"),
                  make_message(0, "assistant", "Done."),
                  make_message(1, "user", "no, that's wrong")],
        tool_trail=[make_trail_entry(turn_index=0, tool_call_id="bp", tool_name="runBlueprint",
                                     args={"blueprint_id": "bp-headcount"}, status="ok")],
    )
    summary = await _load(store, doc)
    # status ok, but a correction references a later turn ⇒ corrected.
    assert summary.blueprint_usages[0].outcome == "corrected"


# --- L12: full-result hydration (D46) + graceful degradation ----------------


async def test_full_result_loaded_gives_shape(store):
    ref = await store.write_full_result(
        "sess-full", "r1", {"columns": ["dept", "ot"], "row_count": 3,
                            "rows": [["a", 1], ["b", 2], ["c", 3]]},
    )
    doc = make_doc(
        "sess-full",
        tool_trail=[make_trail_entry(turn_index=0, tool_call_id="c1", tool_name="runQuery",
                                     args={"sql": "SELECT 1"}, status="ok",
                                     result_full_ref=ref)],
    )
    summary = await _load(store, doc)
    tc = summary.tool_calls[0]
    assert tc.full_result_loaded is True
    assert tc.result_columns == ("dept", "ot")
    assert tc.result_row_count == 3
    assert tc.result_full_ref == ref


async def test_missing_full_result_falls_back_to_preview_no_crash(store):
    preview = ResultPreview(columns=["dept"], row_count=7, truncated=True, preview_rows=[["a"]])
    doc = make_doc(
        "sess-missing",
        tool_trail=[make_trail_entry(turn_index=0, tool_call_id="c1", tool_name="runQuery",
                                     args={"sql": "SELECT 1"}, status="ok",
                                     result_full_ref="result::purged-uuid",
                                     result_preview=preview)],
    )
    # The ref points at a result that was never written (TTL-purged) → None.
    summary = await _load(store, doc)
    tc = summary.tool_calls[0]
    assert tc.full_result_loaded is False           # graceful degradation, no crash
    assert tc.result_columns == ("dept",)           # preview fallback shape
    assert tc.result_row_count == 7
    assert tc.result_full_ref == "result::purged-uuid"


async def test_no_ref_no_preview_is_empty_shape(store):
    doc = make_doc(
        tool_trail=[make_trail_entry(turn_index=0, tool_call_id="c1", tool_name="askUser",
                                     args={"question": "?"}, status="ok")],
    )
    summary = await _load(store, doc)
    tc = summary.tool_calls[0]
    assert tc.full_result_loaded is False
    assert tc.result_columns == ()
    assert tc.result_row_count is None


# --- L13: D72 read-only ------------------------------------------------------


async def test_loader_does_not_mutate_session_or_results(store):
    ref = await store.write_full_result("sess-ro", "r1", {"columns": ["a"], "row_count": 1,
                                                          "rows": [[1]]})
    doc = make_doc(
        "sess-ro",
        messages=[make_message(0, "user", "q"), make_message(0, "assistant", "a"),
                  make_message(1, "user", "perfect")],
        tool_trail=[
            make_trail_entry(turn_index=0, tool_call_id="bad", tool_name="runQuery",
                             args={"sql": "SELCT"}, status="error", error_code="E"),
            make_trail_entry(turn_index=0, tool_call_id="ok", tool_name="runQuery",
                             args={"sql": "SELECT 1"}, status="ok", result_full_ref=ref),
        ],
    )
    before_doc = copy.deepcopy(doc)
    before_results = copy.deepcopy(store._results)

    await _load(store, doc)

    assert doc == before_doc                      # SessionDoc byte-unchanged
    assert store._results == before_results       # result docs byte-unchanged
    assert doc.last_activity == before_doc.last_activity


# --- L14: determinism --------------------------------------------------------


async def test_same_doc_twice_is_identical(store):
    doc = make_doc(
        messages=[make_message(0, "user", "q"), make_message(0, "assistant", "a"),
                  make_message(1, "user", "perfect")],
        tool_trail=[make_trail_entry(turn_index=0, tool_call_id="c1", tool_name="runQuery",
                                     args={"sql": "SELECT 1"}, status="ok")],
    )
    s1 = await _load(store, doc)
    s2 = await _load(store, doc)
    assert s1 == s2
