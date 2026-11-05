"""A successful replacement execution must not launder an old selected measurement."""
from dataclasses import replace

import pytest

from data_agent.runtime.composer import PresentationError
from data_agent.runtime.loop.answer_judge import AnswerJudge, JudgeBrief, JudgeVerdict
from data_agent.runtime.loop.measurement_repair import (
    guard_measurement_approval,
    pending_measurement_rejections,
)
from data_agent.runtime.loop.proposal import ReviewState
from tests.runtime.loop.test_salvage_contract import RecoveryClient
from tests.runtime.test_harness_improvements import batch, call

REJECTION = {
    "violation": "measurement_mismatch", "repair_type": "analysis", "result_ids": ["old"],
    "feedback": "The selected query includes payouts; restrict it to approved time-off requests.",
}


def document(ref):
    return {"takeaway": "Approved hours.", "evidence": [ref], "sections": [{
        "id": "hours", "evidence_refs": [ref], "caveats": [], "actions": [],
        "presentation": {"kind": "table", "result_id": ref},
    }]}


def brief():
    return JudgeBrief("exit_table", "Approved time-off hours excluding payouts?",
                      answer_document=document("old"), referenced_result_ids=("old",),
                      measurement_rejections=(REJECTION,), previous_rejection=REJECTION["feedback"],
                      results=({"tool_call_id": "old", "execution": {"sql": "SELECT sum(hours) FROM events"}},
                               {"tool_call_id": "new", "execution": {"sql": "SELECT sum(hours) FROM events WHERE event_type='Time Off Request'"}}))


@pytest.mark.parametrize("correction", [None, "prose", "same_document"])
def test_new_execution_does_not_clear_rejected_selected_measurement(correction):
    verdict = JudgeVerdict(True, reviewed=True,
                           corrected_answer="Payouts excluded." if correction == "prose" else None,
                           corrected_document=document("old") if correction == "same_document" else None)
    checked = guard_measurement_approval(verdict, brief())
    assert not checked.approved
    assert checked.result_ids == ("old",)
    assert checked.violation == "measurement_mismatch"
    assert checked.corrected_document is checked.corrected_answer is None


def test_corrected_document_selecting_replacement_is_approvable():
    verdict = JudgeVerdict(True, reviewed=True, corrected_document=document("new"))
    assert guard_measurement_approval(verdict, brief()) == verdict


def test_false_positive_can_be_explicitly_reassessed_without_new_query():
    verdict = JudgeVerdict(True, reviewed=True, measurement_reassessments=({
        "result_id": "old", "reason": "The selected receipt's bound event predicate already excludes payouts."},))
    assert guard_measurement_approval(verdict, brief()) == verdict
    assert not guard_measurement_approval(replace(verdict, measurement_reassessments=({"result_id": "new", "reason": "Fixed"},)), brief()).approved


def test_rejection_does_not_blacklist_later_turn_or_prose_repairs():
    state = ReviewState()
    state.reject(JudgeVerdict(False, "measurement_mismatch", "Wrong filter", True,
                             result_ids=("old",), repair_type="analysis"), "v")
    assert pending_measurement_rejections(state)
    assert not pending_measurement_rejections(ReviewState())
    state.violation = ""
    assert not pending_measurement_rejections(state)
    state.reject(JudgeVerdict(False, "contradicts_result", "Say 120 not 125", True,
                             result_ids=("old",), repair_type="analysis"), "v")
    # An earlier unresolved measurement remains relevant until approval; a fresh
    # prose-only rejection never creates a measurement constraint.
    fresh = ReviewState()
    fresh.reject(JudgeVerdict(False, "contradicts_result", "Say 120", True,
                             result_ids=("old",), repair_type="analysis"), "v")
    assert not pending_measurement_rejections(fresh)


@pytest.mark.parametrize("recheck, approved, status", [
    ({"approved": True, "violation": "", "feedback": "", "repair_type": ""}, False, "overridden"),
    ({"approved": True, "measurement_reassessments": [
        {"result_id": "old", "reason": "Its bound predicate already excludes payouts."}]}, True, "cleared"),
    ({"approved": False, "violation": "measurement_mismatch", "feedback": "Select the corrected result.",
      "repair_type": "analysis", "result_ids": ["old"]}, False, "rejected"),
    (None, False, "unavailable"),
])
async def test_approval_of_rejected_selection_is_rechecked_once(recheck, approved, status):
    from types import SimpleNamespace

    from data_agent.runtime.loop.delivery import review_once

    turns = [batch(call("record_judgement", "j1", approved=True, violation="", feedback="", repair_type=""))]
    if recheck is not None:
        turns.append(batch(call("record_judgement", "j2", **recheck)))
    client = RecoveryClient(turns)
    events = []
    loop = SimpleNamespace(_answer_judge=AnswerJudge(client, token_budget=10000),
                           _observer=lambda name, payload: events.append((name, payload)))
    verdict = await review_once(loop, brief())
    assert verdict.reviewed and verdict.approved is approved
    assert len(client.calls) == 2
    assert "measurement_recheck" not in client.calls[0].messages[1]["content"]
    assert "measurement_recheck" in client.calls[1].messages[1]["content"]
    assert ("loop_answer_judge_measurement_override",
            {"site": "exit_table", "status": status, "result_count": 1}) in events
    if status == "rejected":
        assert verdict.feedback == "Select the corrected result."


async def test_approval_selecting_corrected_result_needs_no_recheck():
    from types import SimpleNamespace

    from data_agent.runtime.loop.delivery import review_once

    client = RecoveryClient([batch(call("record_judgement", "j1", approved=True,
                                        violation="", feedback="", repair_type=""))])
    loop = SimpleNamespace(_answer_judge=AnswerJudge(client, token_budget=10000), _observer=lambda *_: None)
    fixed = replace(brief(), answer_document=document("new"), referenced_result_ids=("new",))
    assert (await review_once(loop, fixed)).approved
    assert len(client.calls) == 1


async def test_reassessment_survives_judge_parser():
    reassessment = {"result_id": "old", "reason": "Its bound predicate already excludes payouts."}
    client = RecoveryClient([batch(call("record_judgement", "judge", approved=True,
                                       measurement_reassessments=[reassessment]))])
    verdict = await AnswerJudge(client, token_budget=10000).review(brief())
    assert verdict.approved and verdict.reviewed
    assert verdict.measurement_reassessments == (reassessment,)


async def test_salvage_repairs_stale_selection_once():
    old = {**document("old"), "outcome": "full", "unfinished": []}
    new = {**document("new"), "outcome": "full", "unfinished": []}
    client = RecoveryClient([batch(call("salvageAnswer", "s1", **old)),
                             batch(call("salvageAnswer", "s2", **new))])
    assert await AnswerJudge(client, token_budget=10000).salvage(brief()) == new
    assert len(client.calls) == 2


async def test_salvage_cannot_repeat_stale_selection_forever():
    old = {**document("old"), "outcome": "full", "unfinished": []}
    client = RecoveryClient([batch(call("salvageAnswer", "s", **old))] * 2)
    with pytest.raises(PresentationError, match='Recovery still selects "old"'):
        await AnswerJudge(client, token_budget=10000).salvage(brief())
    assert len(client.calls) == 2


@pytest.mark.parametrize("resubmit", ["old", "new"])
async def test_runtime_binds_approved_repair_to_selected_execution(resubmit):
    from tests.runtime.test_harness_improvements import Judge, build, discovery, run

    class Recovery(Judge):
        async def salvage(self, recovery_brief):
            # Even a recovery double offering the old result must be checked.
            return {**document("old"), "outcome": "full", "unfinished": []}

    judge = Recovery([
        JudgeVerdict(False, "measurement_mismatch", "Restrict the salary population.", True,
                     result_ids=("old",), repair_type="analysis"),
        JudgeVerdict(True, reviewed=True),
    ])
    loop, store, model, mcp, events = build([
        discovery(),
        batch(call("runQuery", "old", sql="SELECT Department, count() AS n FROM hr.employee GROUP BY Department")),
        batch(call("finalizeAnswer", "a1", **document("old"))),
        batch(call("runQuery", "new", sql="SELECT Department, count() AS n FROM hr.employee WHERE Salary > 100000 GROUP BY Department")),
        batch(call("finalizeAnswer", "a2", **document(resubmit))),
    ], judge, rows=[
        {"columns": ["Department", "n"], "rows": [["Sales", 3072]], "row_count": 1, "truncated": False},
        {"columns": ["Department", "n"], "rows": [["Sales", 72]], "row_count": 1, "truncated": False},
    ])
    answer = await run(loop, "Count employees earning above 100000 by department.")
    assert any("Running a corrected query does not update an older result ID" in str(c.messages) for c in model.calls)
    assert judge.briefs[1].measurement_rejections[0]["result_ids"] == ["old"]
    if resubmit == "old":
        # One recheck of the stale approval, then the override stands.
        assert len(judge.briefs) == 3 and judge.briefs[2].measurement_recheck == ("old",)
        assert any(e == "loop_answer_judge_measurement_override" and p["status"] == "overridden" for e, p in events)
        assert answer.review["status"] != "approved"
        assert not answer.answer_document.get("sources")
        assert any(e == "loop_recovery_validation_failed" and p["reason"] == "unresolved_measurement_repair" for e, p in events)
    else:
        assert len(judge.briefs) == 2
        assert answer.review["status"] == "approved"
        assert set(answer.answer_document["sources"]) == {"new"}


async def test_runtime_ships_wrongly_rejected_result_once_the_judge_reassesses_it():
    from tests.runtime.test_harness_improvements import Judge, build, discovery, run

    reason = "Its SQL already counts only salaries above 100000."
    judge = Judge([
        JudgeVerdict(False, "measurement_mismatch", "Restrict the salary population.", True,
                     result_ids=("old",), repair_type="analysis"),
        JudgeVerdict(True, reviewed=True),
        JudgeVerdict(True, reviewed=True, measurement_reassessments=({"result_id": "old", "reason": reason},)),
    ])
    loop, store, model, mcp, events = build([
        discovery(),
        batch(call("runQuery", "old", sql="SELECT Department, count() AS n FROM hr.employee WHERE Salary > 100000 GROUP BY Department")),
        batch(call("finalizeAnswer", "a1", **document("old"))),
        batch(call("finalizeAnswer", "a2", **document("old"))),
    ], judge, rows=[{"columns": ["Department", "n"], "rows": [["Sales", 72]], "row_count": 1, "truncated": False}])
    answer = await run(loop, "Count employees earning above 100000 by department.")
    assert judge.briefs[2].measurement_recheck == ("old",)
    assert any(e == "loop_answer_judge_measurement_override" and p["status"] == "cleared" for e, p in events)
    assert answer.review["status"] == "approved"
    assert set(answer.answer_document["sources"]) == {"old"}


def test_text_only_answer_cannot_hide_rejected_evidence_binding():
    text_brief = replace(brief(), answer_document={"takeaway": "3072 hours.", "sections": [], "sources": {}})
    assert not guard_measurement_approval(JudgeVerdict(True, reviewed=True), text_brief).approved


async def test_no_recheck_without_review_time(monkeypatch):
    from types import SimpleNamespace

    from data_agent.runtime.loop import delivery

    clock = iter([0.0] + [1e6] * 8)
    monkeypatch.setattr(delivery, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    client = RecoveryClient([batch(call("record_judgement", "j1", approved=True,
                                        violation="", feedback="", repair_type=""))])
    events = []
    loop = SimpleNamespace(_answer_judge=AnswerJudge(client, token_budget=10000),
                           _observer=lambda name, payload: events.append((name, payload)))
    verdict = await delivery.review_once(loop, brief())
    assert not verdict.approved and len(client.calls) == 1
    assert ("loop_answer_judge_measurement_override",
            {"site": "exit_table", "status": "skipped", "result_count": 1}) in events


def test_measurement_rejection_is_told_to_list_only_wrong_results():
    from data_agent.runtime.loop.answer_judge import _ANSWER_JUDGE_PROMPT, _unfiltered_judge_tool

    description = _unfiltered_judge_tool("exit_table")["parameters"]["properties"]["result_ids"]["description"]
    assert "not correct results combined with them" in description
    assert "measurement_recheck" in _ANSWER_JUDGE_PROMPT


async def test_measurement_recheck_and_override_share_the_agent_trace():
    from types import SimpleNamespace

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from data_agent.runtime.loop.delivery import review_once
    from data_agent.runtime.observability import tracing

    exporter = InMemorySpanExporter()
    provider = tracing.configure_tracing(
        otlp_endpoint="", service_name="test", span_exporter=exporter,
    )
    tracer = tracing.get_tracer(provider)
    client = RecoveryClient([
        batch(call("record_judgement", "j1", approved=True)),
        batch(call("record_judgement", "j2", approved=True,
                   measurement_reassessments=[{"result_id": "old", "reason": "Its executed predicate already excludes payouts."}])),
    ])
    loop = SimpleNamespace(
        _answer_judge=AnswerJudge(client, token_budget=10000, tracer=tracer),
        _observer=tracing.guardrail_observer(tracer),
    )
    with tracing.agent_span(tracer, scope_hash="synthetic", turn_index=0) as root:
        verdict = await review_once(loop, brief())
        root_context = root.get_span_context()
    assert verdict.approved
    spans = exporter.get_finished_spans()
    children = [s for s in spans if s.name in {"answer_judge", "loop_answer_judge_measurement_override"}]
    assert [s.name for s in children].count("answer_judge") == 2
    override = next(s for s in children if s.name == "loop_answer_judge_measurement_override")
    assert override.attributes["status"] == "cleared"
    assert override.attributes["result_count"] == 1
    assert all(s.context.trace_id == root_context.trace_id for s in children)
    assert all(s.parent.span_id == root_context.span_id for s in children)
    provider.shutdown()
