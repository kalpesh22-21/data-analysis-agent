"""Timeout receipts and finish repairs survive the SSE/history boundary."""

import asyncio
import json

import pytest

from data_agent.runtime.app import _stream_turn
from data_agent.runtime.loop.answer_judge import JudgeVerdict
from data_agent.runtime.observability.progress import ProgressEmitter
from data_agent.runtime.session_history import project_history
from tests.runtime.final_answer import recovery_sections
from tests.runtime.loop.test_partial_answer import PART, TEXT, final, reject
from tests.runtime.test_harness_improvements import CREDS, Judge, build, discovery, query, run


async def stream_result(loop):
    stream = "".join([s async for s in _stream_turn(lambda: run(loop), ProgressEmitter())])
    frames = [
        json.loads(frame.split("data: ", 1)[1])
        for frame in stream.split("\n\n")
        if frame.startswith("event: result\n")
    ]
    assert len(frames) == 1
    return frames[0]


async def assert_history(store, result):
    doc = await store.get_or_create_session(CREDS.session_id)
    history = project_history(doc.messages, doc.tool_trail, CREDS.column_scope, None)
    assert history["turns"][0]["answer"] == result["answer_document"]["takeaway"]
    assert bool(history["turns"][0]["answer_tables"]) == bool(result["answer_document"]["sections"])
    assert doc.messages[-1].answer_document == result["answer_document"]
    return doc


async def test_timed_out_proposal_and_failed_salvage_ship_limitation_once():
    class TimeoutJudge(Judge):
        timeout_seconds = 0.02

        async def review(self, brief):
            self.briefs.append(brief)
            if len(self.briefs) > 1:
                return reject()  # the duplicate review must never happen
            await asyncio.Event().wait()

    judge = TimeoutJudge()
    loop, store, model, _, _ = build([discovery(), query(), final()], judge)
    result = await stream_result(loop)
    assert result["review"]["status"] == "exhausted"
    assert "couldn't verify" in result["answer_document"]["takeaway"]
    assert "review" not in result["answer_document"]["takeaway"].lower()
    assert not result["answer_document"]["sections"]
    assert len(judge.briefs) == 2 and model.calls_made == 3
    doc = await assert_history(store, result)
    assert doc.review_states["0"]["delivery_reason"] == "timeout"


def change_delivery_before_finish(loop):
    original = loop._finish

    async def finish(**kwargs):
        # A changed delivery must still be reviewed despite the proposal receipt.
        kwargs["assistant_text"] += " These are the available records."
        return await original(**kwargs)

    loop._finish = finish


@pytest.mark.parametrize("with_table", [False, True])
async def test_finish_rejection_ships_approved_partial_without_another_review(with_table):
    part = {**PART, 'sections': recovery_sections(['q'] if with_table else [])}
    judge = Judge([JudgeVerdict(True, reviewed=True), reject(part)])
    loop, store, model, _, events = build([discovery(), query(), final()], judge)
    change_delivery_before_finish(loop)
    result = await stream_result(loop)
    assert result["answer_document"]["takeaway"] == TEXT
    assert result["review"]["completion"] == "partial"
    assert result["review"]["status"] == "approved"
    assert bool(result["answer_document"]["sections"]) == with_table
    assert len(judge.briefs) == 2 and model.calls_made == 3
    await assert_history(store, result)
    event = next(shape for name, shape in events if name == "loop_partial_answer_recovered")
    assert event["text_length"] == len(TEXT)
    assert event["table_count"] == int(with_table)


@pytest.mark.parametrize("valid", [True, False])
async def test_changed_delivery_uses_salvage_once_without_another_prose_review(valid):
    class RecoveryJudge(Judge):
        salvage_calls = 0

        async def salvage(self, brief):
            self.salvage_calls += 1
            return {**PART, 'outcome': 'full', 'unfinished': [], 'evidence': ['q' if valid else 'missing'], 'sections': recovery_sections(['q'], titles=[], evidence=['q' if valid else 'missing'])}

    judge = RecoveryJudge([JudgeVerdict(True, reviewed=True)])
    loop, store, model, _, _ = build([discovery(), query(), final()], judge)
    change_delivery_before_finish(loop)
    result = await stream_result(loop)
    assert len(judge.briefs) == 1 and judge.salvage_calls == 1 and model.calls_made == 3
    if valid:
        assert result["answer_document"]["takeaway"] == PART["takeaway"]
        assert len(result["answer_document"]["sections"]) == 1
        assert result["review"]["status"] == "approved"
    else:
        assert result["answer_document"]["takeaway"] != PART["takeaway"]
        assert result["review"]["status"] == "exhausted"
        assert not result["answer_document"]["sections"]
    await assert_history(store, result)


async def test_timeout_does_not_override_an_outstanding_rejection():
    class TimeoutAfterRejection(Judge):
        async def review(self, brief):
            self.briefs.append(brief)
            if len(self.briefs) == 1:
                return reject()
            raise TimeoutError()

    judge = TimeoutAfterRejection()
    loop, _, _, _, _ = build([discovery(), query(), final(), final()], judge)
    result = await stream_result(loop)
    assert result["review"]["status"] == "rejected"
    assert "The leave ranking is shown" not in result["answer_document"]["takeaway"]
    assert not result["answer_document"]["sections"]
    assert len(judge.briefs) == 3


async def test_finish_rejects_invalid_partial_and_attempts_salvage():
    judge = Judge([JudgeVerdict(True, reviewed=True), reject({**PART, "evidence": ["missing"]})])
    loop, store, model, _, events = build([discovery(), query(), final()], judge)
    change_delivery_before_finish(loop)
    result = await stream_result(loop)
    assert result["review"]["status"] == "exhausted"
    assert result["answer_document"]["takeaway"] != TEXT
    assert len(judge.briefs) == 2 and model.calls_made == 3
    assert ("loop_recovery_validation_failed", {"reason": "unknown_evidence_id"}) in events
    await assert_history(store, result)


async def test_response_emission_is_recorded_after_persistence():
    judge = Judge([JudgeVerdict(True, reviewed=True)])
    loop, _, _, _, events = build([discovery(), query(), final()], judge)
    frames = [frame async for frame in _stream_turn(
        lambda: run(loop), ProgressEmitter(),
        delivery_observer=lambda name, payload: events.append((name, payload)),
    )]
    assert sum(frame.startswith("event: result\n") for frame in frames) == 1
    names = [name for name, _ in events]
    assert names.index("loop_response_persisted") < names.index("loop_response_emitted")
