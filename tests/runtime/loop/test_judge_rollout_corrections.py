"""Structural reproductions of the November 5 rollout; no production rows."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from data_agent.runtime.loop.answer_judge import AnswerJudge, JudgeBrief, _read_verdict
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.observability.tracing import _GUARDRAIL_OBSERVER_ATTR_ALLOWLIST
from tests.runtime.loop.test_judge_malformed_reasons import APPROVE_DOCUMENT, DOCUMENT, _turn

BASE = {"approved": True, "violation": "", "feedback": "", "repair_type": "prose"}
BRIEF = JudgeBrief("exit_prose", "Summarize the result", allow_prose_correction=True)


async def review(args, *, brief=BRIEF, second=None):
    client = ScriptedModelClient([_turn(args)] + ([_turn(second)] if second is not None else []))
    events = []
    judge = AnswerJudge(client, 20000, observer=lambda e, p: events.append((e, p)))
    verdict = await judge.review(brief)
    return verdict, client, events


async def test_payroll_clarification_explanation_does_not_discard_corrected_options():
    question = {"question": "Which cost basis?", "options": ["Gross earnings", "Gross plus employer taxes"]}
    verdict, client, _ = await review(
        {**BASE, "feedback": "Removed employee deductions from employer cost.", "corrected_question": question},
        brief=replace(BRIEF, site="ask_user", allow_prose_correction=False),
    )
    assert verdict.reviewed and verdict.corrected_question == question
    assert verdict.feedback == ""  # Explanation does not become a repair instruction.
    assert client.calls_made == 1


async def test_expense_clarification_conflict_is_rejudged_not_silently_cleared():
    args = {**BASE, "violation": "unsupported_environment_claim", "feedback": "Remove the premise.",
            "corrected_question": {"question": "Which expense?", "options": ["Expenses", "Payroll"]},
            "evidence": [], "unfinished": []}
    refusal = {"approved": False, "violation": "unsupported_environment_claim", "feedback": "The premise still needs fixing."}
    verdict, client, events = await review(args, brief=replace(BRIEF, site="ask_user"), second=refusal)
    assert verdict.reviewed and not verdict.approved
    assert client.calls_made == 2
    assert any(e.endswith("correction_repair_requested") for e, _ in events)
    assert "Do not clear a substantive violation" in client.calls[1].messages[-1]["content"]


async def test_empty_compatibility_lists_do_not_discard_employment_distribution_prose():
    verdict, client, _ = await review({**BASE, "corrected_answer": "Supported summary.", "evidence": [], "unfinished": []})
    assert verdict.reviewed and verdict.corrected_answer == "Supported summary."
    assert client.calls_made == 1


@pytest.mark.parametrize("extras", [
    {"evidence": [], "unfinished": []},
    {"extra": None, "debug": {"token": "SECRET"}},
])
async def test_unused_top_level_fields_do_not_spend_a_repair(extras):
    args = {**BASE, "corrected_answer": "Supported summary.", **extras}
    original = deepcopy(args)
    verdict, client, events = await review(args)
    assert verdict.reviewed and verdict.corrected_answer == "Supported summary."
    assert client.calls_made == 1 and args == original
    dropped = next(p for e, p in events if e == "loop_answer_judge_fields_dropped")
    assert set(dropped["dropped_fields"].split(",")) == set(extras)
    assert "SECRET" not in str(events) and "unselected-result" not in str(events)


@pytest.mark.parametrize("field", ["feedback", "violation"])
async def test_null_optional_review_metadata_is_empty(field):
    verdict, client, _ = await review({**BASE, "corrected_answer": "Supported.", field: None})
    assert verdict.reviewed and client.calls_made == 1


@pytest.mark.parametrize("deliverables", [None, [], {}, "unused", [{"intent_id": ""}], [{"intent_id": "invented"}]])
@pytest.mark.parametrize("intents", [(), (("real", "Summary", "pending", None),)])
async def test_ignored_document_deliverables_never_trigger_validation_or_intent_repair(deliverables, intents):
    args = {**APPROVE_DOCUMENT, "corrected_document": {**DOCUMENT, "deliverables": deliverables}}
    original = deepcopy(args)
    verdict, client, events = await review(args, brief=replace(BRIEF, intents=intents))
    assert verdict.reviewed and client.calls_made == 1
    assert "deliverables" not in verdict.corrected_document
    assert args == original
    assert next(p for e, p in events if e == "loop_answer_judge_fields_dropped")["dropped_fields"] == "deliverables"


def invalid_document():
    return {**APPROVE_DOCUMENT, "corrected_document": {**DOCUMENT, "sections": None}}


async def test_invalid_applied_document_field_still_gets_one_repair():
    verdict, client, events = await review(invalid_document(), second=APPROVE_DOCUMENT)
    assert verdict.reviewed and client.calls_made == 2
    diagnostic = next(p for e, p in events if e.endswith("correction_repair_requested"))
    assert diagnostic["schema_keyword"] == "type"
    assert diagnostic["instance_path"] == "/corrected_document/sections"
    assert diagnostic["expected"] == "type=array"


async def test_persistent_invalid_correction_is_bounded_and_diagnostics_exclude_values():
    args = {**APPROVE_DOCUMENT, "corrected_document": {**DOCUMENT, "sql": "SECRET-SQL-AND-TOKEN"}}
    verdict, client, events = await review(args, second=args)
    assert not verdict.reviewed and client.calls_made == 2
    assert "SECRET-SQL-AND-TOKEN" not in str(events)
    failed = next(p for e, p in events if e == "loop_answer_judge_failed")
    assert failed["unknown_fields"] == "sql"
    assert failed["schema_keyword"] == "additionalProperties"
    assert all(k in _GUARDRAIL_OBSERVER_ATTR_ALLOWLIST for k in (
        "schema_path", "schema_keyword", "instance_path", "unknown_fields", "expected"))


async def test_disallowed_correction_does_not_spend_a_repair_call():
    verdict, client, _ = await review(invalid_document(), brief=replace(BRIEF, allow_prose_correction=False))
    assert not verdict.reviewed and client.calls_made == 1


async def test_both_calls_share_one_deadline_and_cancel_the_repair():
    class SlowClient:
        calls = 0
        cancelled = False

        async def send_turn(self, messages, tools):
            self.calls += 1
            try:
                await asyncio.sleep(.06)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return _turn(invalid_document())

    client = SlowClient()
    events = []
    verdict = await AnswerJudge(client, 20000, timeout_seconds=.1, observer=lambda e,p: events.append((e,p))).review(BRIEF)
    assert [p["outcome"] for e,p in events if e.endswith("correction_repair_finished")] == ["timeout"]
    assert verdict.failure_reason == "timeout" and not verdict.reviewed
    assert client.calls == 2 and client.cancelled


async def test_repair_provider_failure_keeps_review_unavailable():
    client = ScriptedModelClient([_turn(invalid_document())])
    events = []
    verdict = await AnswerJudge(client, 20000, observer=lambda e,p: events.append((e,p))).review(BRIEF)
    assert [p["outcome"] for e,p in events if e.endswith("correction_repair_finished")] == ["provider_error"]
    assert not verdict.reviewed and verdict.corrected_document is None
    assert len(client.calls) == 2


async def test_token_usage_accounts_for_both_calls():
    first = replace(_turn(invalid_document()), usage={"total_tokens": 20, "prompt_tokens": 12, "completion_tokens": 8})
    second = replace(_turn(APPROVE_DOCUMENT), usage={"total_tokens": 30, "prompt_tokens": 18, "completion_tokens": 12})
    events = []
    verdict = await AnswerJudge(ScriptedModelClient([first, second]), 20000, observer=lambda e,p: events.append((e,p))).review(BRIEF)
    assert verdict.reviewed
    assert next(p for e,p in events if e == "loop_answer_judge_called")["tokens"] == 50


def test_dropped_field_names_are_bounded_identifiers_only():
    args = {**BASE, "corrected_answer": "Supported.", "bad key SECRET": "SECRET", "x" * 100: "SECRET"}
    reading = _read_verdict(_turn(args), "exit_prose")
    assert reading.malformed_reason is None and reading.dropped_fields == ""


def test_trace_allowlist_has_no_duplicate_fields():
    assert len(_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST) == len(set(_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST))


@pytest.mark.parametrize("field,value", [("evidence", ["r1"]), ("unfinished", ["Missing requested breakdown"]), ("unfinished", "Missing breakdown")])
async def test_nonempty_semantic_fields_cannot_be_silently_approved(field, value):
    args = {**BASE, "corrected_answer": "Partial result.", field: value}
    verdict, client, events = await review(args, second=args)
    assert not verdict.reviewed and verdict.corrected_answer is None
    assert client.calls_made == 2
    assert next(p for e,p in events if e.endswith("correction_repair_requested"))["malformed_reason"] == "correction_semantic_fields"
    assert not any(e == "loop_answer_judge_fields_dropped" for e,p in events)
    failed_call = client.calls[1].messages[-2]
    assert failed_call["role"] == "assistant"
    assert json.loads(failed_call["tool_calls"][0]["function"]["arguments"]) == args
    feedback = client.calls[1].messages[-1]
    assert feedback["role"] == "tool" and feedback["tool_call_id"] == failed_call["tool_calls"][0]["id"]
    assert str(value) not in str(events)


async def test_evidence_survives_repair_as_structured_document():
    args = {**BASE, "corrected_answer": "Supported summary.", "evidence": ["r1"]}
    replacement = {**APPROVE_DOCUMENT, "corrected_document": {**DOCUMENT, "evidence": ["r1"]}}
    verdict, client, events = await review(args, second=replacement)
    assert verdict.reviewed and verdict.corrected_document["evidence"] == ["r1"]
    assert client.calls_made == 2
    assert [p["outcome"] for e,p in events if e.endswith("correction_repair_finished")] == ["repaired"]


async def test_unfinished_work_survives_repair_as_partial_answer():
    partial = {**DOCUMENT, "evidence": ["r1"], "unfinished": ["Missing requested breakdown"]}
    args = {**BASE, "corrected_answer": "Partial.", "unfinished": partial["unfinished"]}
    rejection = {"approved": False, "violation": "unsupported_by_evidence", "feedback": "Breakdown is missing.",
                 "repair_type": "presentation", "partial_answer": partial}
    verdict, _, _ = await review(args, second=rejection, brief=replace(BRIEF, allow_partial_answer=True))
    assert verdict.reviewed and not verdict.approved
    assert verdict.partial_answer["unfinished"] == partial["unfinished"]


def test_correction_schema_does_not_advertise_deliverables_or_mutate_proposal_schema():
    from data_agent.runtime.composer import PROPOSAL_SCHEMA
    from data_agent.runtime.loop.answer_judge import build_judge_tool

    before = deepcopy(PROPOSAL_SCHEMA)
    schema = build_judge_tool("exit_table")["parameters"]["properties"]["corrected_document"]
    assert "deliverables" not in schema["properties"]
    assert PROPOSAL_SCHEMA == before and "deliverables" in PROPOSAL_SCHEMA["properties"]


async def test_cancelled_repair_finishes_telemetry_and_propagates():
    started = asyncio.Event()

    class Client:
        calls = 0

        async def send_turn(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return _turn(invalid_document())
            started.set()
            await asyncio.Event().wait()

    events = []
    task = asyncio.create_task(AnswerJudge(Client(), 20000, observer=lambda e,p: events.append((e,p))).review(BRIEF))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [p["outcome"] for e,p in events if e.endswith("correction_repair_finished")] == ["cancelled"]


@pytest.mark.parametrize("field", ["evidence", "unfinished"])
async def test_plain_approval_with_semantic_metadata_does_not_trigger_correction_repair(field):
    verdict, client, events = await review({"approved": True, "violation": "", "feedback": "", field: ["existing metadata"]})
    assert verdict.approved and verdict.reviewed and client.calls_made == 1
    assert not any("correction_repair" in event for event, _ in events)


@pytest.mark.parametrize("empty", [None, [], {}, "", " \n\t"])
@pytest.mark.parametrize("field", ["evidence", "unfinished"])
async def test_harmless_empty_semantic_metadata_needs_no_repair(field, empty):
    verdict, client, _ = await review({**BASE, "corrected_answer": "Supported.", field: empty})
    assert verdict.reviewed and client.calls_made == 1


async def test_approval_correction_and_partial_answer_reaches_correction_count_repair():
    args = {**APPROVE_DOCUMENT, "partial_answer": {**DOCUMENT, "unfinished": ["Missing breakdown"]}}
    verdict, client, events = await review(args, second=APPROVE_DOCUMENT)
    assert verdict.reviewed and client.calls_made == 2
    assert next(p for e,p in events if e.endswith("correction_repair_requested"))["malformed_reason"] == "correction_count"
    plain = _read_verdict(_turn({"approved": True, "partial_answer": DOCUMENT}), "exit_prose")
    assert plain.malformed_reason == "approval_with_partial_answer"


@pytest.mark.parametrize("metadata", [{}, {"reasoning_content": "Preserve this reasoning", "reasoning_details": [{"type": "reasoning.text", "text": "detail"}]}])
async def test_repair_replays_response_reasoning_metadata(metadata):
    first = replace(_turn(invalid_document()), reasoning_metadata=metadata)
    client = ScriptedModelClient([first, _turn(APPROVE_DOCUMENT)])
    verdict = await AnswerJudge(client, 20000).review(BRIEF)
    assert verdict.reviewed
    replay = client.calls[1].messages[-2]
    for key, value in metadata.items():
        assert replay[key] == value
    if not metadata:
        assert "reasoning_content" not in replay


async def test_oversized_repair_is_not_sent_or_truncated():
    args = {**APPROVE_DOCUMENT, "corrected_document": {**DOCUMENT, "sections": None, "takeaway": "x" * 16000}}
    first = replace(_turn(args), reasoning_metadata={"reasoning_content": "reason" * 500})
    client = ScriptedModelClient([first])
    events = []
    judge = AnswerJudge(client, 3000, observer=lambda e,p: events.append((e,p)))
    judge.messages_for = lambda brief: [{"role": "system", "content": "instructions"}, {"role": "user", "content": "Original brief"}]
    verdict = await judge.review(BRIEF)
    assert not verdict.reviewed and client.calls_made == 1
    assert [p["outcome"] for e,p in events if e.endswith("correction_repair_finished")] == ["budget_exhausted"]
    assert next(p for e,p in events if e == "loop_answer_judge_failed")["malformed_reason"] == "correction_schema"


def test_invalid_correction_validates_and_builds_schema_only_once(monkeypatch):
    from jsonschema import Draft202012Validator

    from data_agent.runtime.loop import answer_judge

    validations, schemas = [], []
    original_validate = Draft202012Validator.iter_errors
    original_build = answer_judge.build_judge_tool

    def validate(self, *args, **kwargs):
        # Nested conditional rules use iter_errors internally; count only the
        # correction document's root validation, not jsonschema's recursion.
        if "takeaway" in self.schema.get("properties", {}):
            validations.append(True)
        return original_validate(self, *args, **kwargs)

    def build(*args, **kwargs):
        schemas.append(True)
        return original_build(*args, **kwargs)

    monkeypatch.setattr(Draft202012Validator, "iter_errors", validate)
    monkeypatch.setattr(answer_judge, "build_judge_tool", build)
    reading = _read_verdict(_turn(invalid_document()), "exit_prose")
    assert reading.malformed_reason == "correction_schema"
    assert len(validations) == len(schemas) == 1
