"""finalizeAnswer may carry the answer's assumptions itself.

Nine of the failed answers in tool_model_object_issues.md put assumptions on the
finalizer (as `assumptions`, `recordAssumptions` or `record_assumptions`) and were
rejected. `assumptions` is now a declared field that writes the turn's list exactly as a
recordAssumptions call does; live, resume, history and the judge all see the same list.
"""

import pytest

from data_agent.runtime.composer import PROPOSAL_SCHEMA, PresentationError, proposal_to_internal
from data_agent.runtime.composer.components import restrict_schema
from data_agent.runtime.loop.answer_judge import build_judge_tool
from data_agent.runtime.mcp.tool_schema import FINALIZE_ANSWER_SCHEMA
from data_agent.runtime.session_history import project_history
from tests.runtime.test_answer_document import section
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

POPULATION = "Employees means Active and On Leave."
RECORDED = "Department is the employee's home department."


def finalize(ident="answer", **extra):
    return call("finalizeAnswer", ident, takeaway="Employee counts by department.",
                sections=[section()], evidence=["q"], **extra)


async def _turn(*responses):
    judge = Judge()
    loop, store, _, _, _ = build([discovery(), query(), *responses], judge)
    out = await run(loop)
    doc = await store.get_or_create_session(CREDS.session_id)
    history = project_history(doc.messages, doc.tool_trail, CREDS.column_scope, None)["turns"][0]
    resumed = await loop._compute_turn_assumptions(CREDS.session_id, 0)
    return out, judge, history, resumed


def test_the_finalizer_declares_assumptions_and_so_does_the_judge_correction():
    tool = restrict_schema(FINALIZE_ANSWER_SCHEMA)
    declared = tool["parameters"]["properties"]["assumptions"]
    assert declared["type"] == "array" and declared["items"] == {"type": "string"}
    assert "replaces" in declared["description"] and "recordAssumptions" in declared["description"]
    correction = build_judge_tool("exit_table")["parameters"]["properties"]["corrected_document"]
    assert correction["properties"]["assumptions"] == PROPOSAL_SCHEMA["properties"]["assumptions"]


async def test_finalizer_assumptions_reach_the_answer_the_judge_resume_and_history():
    out, judge, history, resumed = await _turn(batch(finalize(assumptions=[POPULATION])))
    assert out.review["status"] == "approved"
    assert out.assumptions == [POPULATION]
    assert POPULATION in out.answer_document["sections"][0]["caveats"]
    assert judge.briefs[0].assumptions == (POPULATION,)
    assert resumed == [POPULATION]
    assert history["assumptions"] == [POPULATION]


async def test_without_the_field_recorded_assumptions_stay_and_an_empty_list_clears_them():
    recorded = batch(call("recordAssumptions", "a", assumptions=[RECORDED]))
    out, _, history, resumed = await _turn(recorded, batch(finalize()))
    assert out.assumptions == resumed == history["assumptions"] == [RECORDED]

    out, _, history, resumed = await _turn(recorded, batch(finalize(assumptions=[])))
    assert out.assumptions is None and resumed == [] and history["assumptions"] is None


async def test_in_one_response_the_finalizer_list_wins_whichever_call_comes_first():
    for calls in ([finalize(assumptions=[POPULATION]), call("recordAssumptions", "a", assumptions=[RECORDED])],
                  [call("recordAssumptions", "a", assumptions=[RECORDED]), finalize(assumptions=[POPULATION])]):
        out, _, history, resumed = await _turn(batch(*calls))
        assert out.assumptions == resumed == history["assumptions"] == [POPULATION]


@pytest.mark.parametrize(
    ("assumptions", "message"),
    [([{"assumption": "Counted per department code."}], "assumptions.0: expected type: string, got: object"),
     ("Counted per department code.", "assumptions: expected type: array, got: string")],
)
def test_a_malformed_list_is_named(assumptions, message):
    """#43 sent a list of objects."""
    with pytest.raises(PresentationError, match=message):
        proposal_to_internal({"takeaway": "Counts.", "sections": [section()], "assumptions": assumptions})


@pytest.mark.parametrize("name", ["recordAssumptions", "record_assumptions"])
def test_the_tool_name_sent_as_a_field_is_pointed_at_assumptions(name):
    with pytest.raises(PresentationError, match=f"{name} \\(is not a field: send the list as assumptions\\)"):
        proposal_to_internal({"takeaway": "Counts.", "sections": [section()], name: [POPULATION]})



async def test_a_judge_corrected_assumption_list_is_what_resume_and_history_return():
    """A correction replaced "Only Active" with "Active and On Leave" in the delivered
    answer, but resume and history rebuilt the model's original list."""
    from data_agent.runtime.loop.answer_judge import JudgeVerdict

    corrected = ["Employees means Active and On Leave."]
    replacement = {"takeaway": "Employee counts by department.", "sections": [section()], "evidence": ["q"],
                   "assumptions": corrected}
    judge = Judge([JudgeVerdict(True, reviewed=True, repair_type="presentation", corrected_document=replacement)])
    followup = batch(finalize("next", assumptions=[]))
    loop, store, model, _, _ = build(
        [discovery(), query(), batch(call("recordAssumptions", "a", assumptions=["Only Active employees are counted."])),
         batch(finalize()), followup], judge)
    out = await run(loop)
    assert out.review["status"] == "approved"
    assert out.assumptions == corrected
    assert await loop._compute_turn_assumptions(CREDS.session_id, 0) == corrected
    doc = await store.get_or_create_session(CREDS.session_id)
    assert project_history(doc.messages, doc.tool_trail, CREDS.column_scope, None)["turns"][0]["assumptions"] == corrected

    await run(loop, "And by location?")
    replayed = [c["function"]["name"] for m in model.calls[-1].messages for c in m.get("tool_calls", [])]
    assert "recordAssumptions" not in replayed  # the runtime's write never reaches the model


async def test_a_judge_correction_rejected_afterwards_writes_no_assumptions():
    """The correction's prose leaked SQL, so it was rejected after binding; its assumption
    list must not reach resume or history as if it had shipped."""
    from data_agent.runtime.loop.answer_judge import JudgeVerdict

    replacement = {"takeaway": "SELECT the departments FROM the result: Sales has 120.",
                   "sections": [section()], "evidence": ["q"], "assumptions": [POPULATION]}
    judge = Judge([JudgeVerdict(True, reviewed=True, repair_type="presentation", corrected_document=replacement)])
    loop, store, *_ = build([discovery(), query(), batch(finalize()), batch(finalize("next"))], judge)
    out = await run(loop)
    assert out.review["violation"] == "leaks_sql_or_schema"
    doc = await store.get_or_create_session(CREDS.session_id)
    assert not [e for e in doc.tool_trail if e.tool_call_id.startswith("judge_assumptions_")]
    assert await loop._compute_turn_assumptions(CREDS.session_id, 0) != [POPULATION]


def test_only_a_finalizer_proposal_writes_assumptions_through_an_answer_tool():
    """finalizeAnswer is stored under the answer tool it was rewritten to, with its
    sections; a direct legacy answer-tool call has none and does not write the list."""
    from data_agent.runtime.composite.record_assumptions import writes_assumptions

    assert writes_assumptions("answerWithTable", {"sections": [], "assumptions": [POPULATION]})
    assert not writes_assumptions("answerWithTable", {"answer": "Counts.", "assumptions": [POPULATION]})
    assert writes_assumptions("recordAssumptions", {"assumptions": []})


async def test_the_freshness_rejection_says_how_to_clear_an_assumption_on_the_finalizer():
    """The message said only recordAssumptions; a model that put the assumption on
    finalizeAnswer kept resending it and the turn exhausted."""
    stale = "The data is current as of today."
    loop, store, model, *_ = build(
        [discovery(), query(), batch(finalize("f1", assumptions=[stale])), batch(finalize("f2", assumptions=[]))],
        Judge())
    out = await run(loop)
    assert out.review["status"] == "approved" and not out.assumptions
    doc = await store.get_or_create_session(CREDS.session_id)
    detail = next(e for e in doc.tool_trail if e.tool_call_id == "f1").denial_detail or ""
    nudges = " ".join(str(m.get("content")) for m in model.calls[-1].messages)
    assert "finalizeAnswer.assumptions" in detail + nudges and "[] to clear it" in detail + nudges
