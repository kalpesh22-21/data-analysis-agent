"""QA: the Release-1 seam END TO END — a real-shaped `SessionDoc` through
`load_session_summary` and into the payload the extractor hands the model.

Everything else in this suite tests one side of the seam with a hand-built
`SessionSummary`. That is exactly where a projection bug hides: a hand-built summary
encodes what the test author BELIEVED the loader emits. These cases build the trail
the Release-1 runtime actually writes — intent-ledger churn, a
`BLUEPRINT_DEFINITION_NOT_READ` refusal before `getBlueprint`, a finalization block,
then a multi-table answer — and assert on the JSON the model receives.

Pure: an `InMemorySessionStore` and a `ScriptedModelClient`, no live infra.
"""

from __future__ import annotations

import json

from data_agent.learning.candidate.signals import SessionSignals
from data_agent.learning.extractor.prior_art import prior_art_query_text
from data_agent.learning.summary import load_session_summary
from data_agent.learning.triage import triage

from ..summary.helpers import make_doc, make_job, make_message, make_trail_entry
from .helpers import (
    KEEP_VERDICT,
    blueprint_raw,
    emit_extractor,
    evidence_item,
    make_answer_sql,
    make_summary,
    make_tool_call,
    make_turn,
)

HEADCOUNT_SQL = "SELECT count(*) AS headcount FROM hr.employee WHERE department = 'Analytics'"
PAYROLL_SQL = (
    "SELECT sum(gross_pay) AS total FROM payroll.payroll_fact "
    "WHERE department = 'Analytics' AND toYear(pay_period) = 2025"
)


def _release1_doc():
    """One turn, as Release 1 writes it: declare intents (one attempt rejected), trip
    the blueprint-definition gate, read it, run it, run a second query, record an
    assumption, get the finalization block, resolve the ledger, answer with two
    tables."""
    return make_doc(
        "sess-r1",
        messages=[
            make_message(0, "user", "headcount and payroll for Analytics in 2025?"),
            make_message(0, "assistant", "1,204 people, $92.4M."),
        ],
        tool_trail=[
            make_trail_entry(turn_index=0, tool_call_id="st1", tool_name="updateAnalysisState",
                             args={"intents": [{"id": "i1"}]}, status="denied",
                             error_code="ANALYSIS_STATE_INVALID"),
            make_trail_entry(turn_index=0, tool_call_id="st2", tool_name="updateAnalysisState",
                             args={"intents": [{"id": "i1", "status": "pending"},
                                               {"id": "i2", "status": "pending"}]},
                             status="ok"),
            make_trail_entry(turn_index=0, tool_call_id="bp_gated", tool_name="runBlueprint",
                             args={"blueprint_id": "bp-headcount"}, status="denied",
                             error_code="BLUEPRINT_DEFINITION_NOT_READ"),
            make_trail_entry(turn_index=0, tool_call_id="get", tool_name="getBlueprint",
                             args={"blueprint_id": "bp-headcount"}, status="ok"),
            make_trail_entry(turn_index=0, tool_call_id="bp_run", tool_name="runBlueprint",
                             args={"blueprint_id": "bp-headcount"}, status="ok"),
            make_trail_entry(turn_index=0, tool_call_id="q1", tool_name="runQuery",
                             args={"sql": PAYROLL_SQL}, status="ok"),
            make_trail_entry(turn_index=0, tool_call_id="rec", tool_name="recordAssumptions",
                             args={"assumptions": ["'Analytics' is a department name"]},
                             status="ok"),
            make_trail_entry(turn_index=0, tool_call_id="ans_blocked",
                             tool_name="answerWithTable",
                             args={"answer": "1,204 people.", "sql": HEADCOUNT_SQL},
                             status="denied",
                             error_code="FINALIZATION_BLOCKED_PENDING_INTENTS"),
            make_trail_entry(turn_index=0, tool_call_id="st3", tool_name="updateAnalysisState",
                             args={"intents": [{"id": "i1", "status": "completed"},
                                               {"id": "i2", "status": "completed"}]},
                             status="ok"),
            make_trail_entry(
                turn_index=0, tool_call_id="ans", tool_name="answerWithTable",
                args={"answer": "1,204 people, $92.4M.", "sql": "", "blueprint_id": "",
                      "tables": [
                          {"sql": HEADCOUNT_SQL, "caption": "headcount",
                           "blueprint_id": "bp-headcount"},
                          {"sql": PAYROLL_SQL, "caption": "payroll", "blueprint_id": ""},
                      ]},
                status="ok",
            ),
        ],
    )


def _payload(extractor) -> dict:
    """The session JSON handed to the model on the first turn (last message)."""
    return json.loads(extractor._model_client.calls[0].messages[-1]["content"])


async def test_a_release1_session_reaches_the_model_clean(store):
    """The whole slice in one assertion set, on a doc the runtime could have written."""
    summary = await load_session_summary(_release1_doc(), store, job=make_job("sess-r1"))

    # --- S2: the projection is faithful, the INFERENCES are clean -------------
    assert [tc.tool_call_ref for tc in summary.tool_calls] == [
        "st1", "st2", "bp_gated", "get", "bp_run", "q1", "rec", "ans_blocked", "st3", "ans",
    ]
    assert summary.failed_fixed_sql == ()  # fix 1: no gate is analyst friction
    assert [(u.tool_call_ref, u.outcome) for u in summary.blueprint_usages] == [
        ("bp_run", "accepted")  # fix 1: the refused run is not a usage of any kind
    ]
    assert [(a.tool_call_ref, a.sql, a.blueprint_id) for a in summary.answer_sqls] == [
        ("ans", HEADCOUNT_SQL, "bp-headcount"),  # fix 2: never dispatched as a runQuery
        ("ans", PAYROLL_SQL, None),
    ]

    # The durable stamp a human ranks on months later carries no invented friction.
    assert SessionSignals.from_summary(summary) == SessionSignals(
        accepted_signal="no_correction", turn_count=1, failed_fixed_count=0,
        askuser_count=0, corrected_blueprint=False,
    )
    assert triage(summary).decision == "keep"

    # --- S3: the payload seam ------------------------------------------------
    extractor = emit_extractor([blueprint_raw(source_refs=("q1",),
                                              evidence=[evidence_item(tool_call_ref="q1")])])
    await extractor.extract(summary, triage(summary))
    payload = _payload(extractor)

    names = [tc["tool_name"] for tc in payload["tool_calls"]]
    # fix 3: the ledger/assumption churn is gone from the PROMPT...
    assert "updateAnalysisState" not in names
    assert "recordAssumptions" not in names
    # ...and nothing else is: the refused calls stay, because "the model tried this
    # and the runtime said no" is narrative the extractor can legitimately use.
    assert [tc["tool_call_ref"] for tc in payload["tool_calls"]] == [
        "bp_gated", "get", "bp_run", "q1", "ans_blocked", "ans",
    ]
    # ...while the SUMMARY other stages read still has all ten.
    assert len(summary.tool_calls) == 10

    # fix 2: the answer's SQL arrives as its own section. `HEADCOUNT_SQL` appears
    # NOWHERE in `tool_calls` (the blueprint run does not carry its SQL, and the
    # answer call is not a _SQL_TOOLS member) — without this section the extractor
    # would never see half of what the user was shown.
    assert all(tc["sql"] != HEADCOUNT_SQL for tc in payload["tool_calls"])
    assert payload["answer_sql"] == [
        {"tool_call_ref": "ans", "sql": HEADCOUNT_SQL, "blueprint_id": "bp-headcount"},
        {"tool_call_ref": "ans", "sql": PAYROLL_SQL, "blueprint_id": None},
    ]
    assert payload["failed_fixed_sql"] == []


async def test_the_excluded_tools_are_dropped_only_at_the_payload_seam(store):
    """Stated as a property of the SUMMARY, which is the contract fix 3 is narrow
    about: triage, generalize, the judge and the audit snapshot all read it, and a
    filter that leaked into the loader would change what they see."""
    summary = await load_session_summary(_release1_doc(), store, job=make_job("sess-r1"))
    state_calls = [tc for tc in summary.tool_calls
                   if tc.tool_name in ("updateAnalysisState", "recordAssumptions")]
    assert len(state_calls) == 4
    # Visible to triage: they are what makes this a session with tool calls at all
    # when nothing else ran.
    bare = await load_session_summary(
        make_doc("sess-state-only",
                 tool_trail=[make_trail_entry(turn_index=0, tool_call_id="st1",
                                              tool_name="updateAnalysisState",
                                              args={"intents": []}, status="ok")]),
        store, job=make_job("sess-state-only"),
    )
    assert triage(bare).reason == "skip_other"  # NOT skip_no_tool_calls


# --- the gaps this sweep found (fixed: `summary/refs.py::sql_by_ref`) ---------


async def test_a_candidate_may_cite_the_answer_sql_ref_it_was_shown():
    """WAS a gap: every ref→SQL map downstream was built as
    `{tc.tool_call_ref: tc.sql for tc in summary.tool_calls}`, and `answerWithTable`
    is not a `_SQL_TOOLS` member, so its `.sql` is `None` — a candidate citing the
    answer's ref (the ONLY ref a session has when the answer SQL was never dispatched
    as a runQuery, which is the case the projection exists for) declined
    `unrewritable_sql`. Both maps now go through `summary/refs.py::sql_by_ref`."""
    summary = make_summary(
        tool_calls=(make_tool_call(ref="ans", sql=None, tool_name="answerWithTable"),),
        answer_sqls=(make_answer_sql(ref="ans"),),  # the helper's PAYROLL_SQL
    )
    extractor = emit_extractor([blueprint_raw(source_refs=("ans",),
                                              evidence=[evidence_item(tool_call_ref="ans")])])
    result = await extractor.extract(summary, KEEP_VERDICT)
    assert [d.reason for d in result.declines] == []
    assert len(result.candidates) == 1


def test_the_answer_sql_survives_a_busy_sessions_query_text():
    """WAS a gap: `prior_art_query_text` appended `answer_sqls` LAST, after every ok
    call's SQL, and truncates the join at `_MAX_QUERY_CHARS`. On a multi-intent
    Release-1 session the one query the answer stood on was exactly what fell off the
    end. The answer SQL now follows the turns directly."""
    noisy = tuple(
        make_tool_call(ref=f"q{i}",
                       sql=f"SELECT /*{i}*/ " + "a_column_name, " * 25 + "x FROM hr.employee")
        for i in range(6)
    )
    summary = make_summary(
        turns=(make_turn(user_nl="headcount, payroll, attrition and tenure for Analytics?"),),
        tool_calls=noisy,
        answer_sqls=(make_answer_sql("SELECT what_the_user_actually_saw FROM hr.final",
                                     ref="ans"),),
    )
    assert "what_the_user_actually_saw" in prior_art_query_text(summary)
