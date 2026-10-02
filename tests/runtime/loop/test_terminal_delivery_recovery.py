"""Timeout receipts and finish repairs survive the SSE/history boundary."""

import asyncio
import json

import pytest

from data_agent.runtime.app import _stream_turn
from data_agent.runtime.loop.answer_judge import JudgeVerdict
from data_agent.runtime.observability.progress import ProgressEmitter
from data_agent.runtime.session_history import project_history
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
    assert history["turns"][0]["answer"] == result["assistant_text"]
    assert history["turns"][0]["answer_tables"] == result["answer_tables"]
    return doc


async def test_timed_out_proposal_ships_once_as_exhausted_with_table_and_history():
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
    assert result["assistant_text"] == "There are 120 employees. The leave ranking is shown."
    assert len(result["answer_tables"]) == 1
    assert len(judge.briefs) == 1 and model.calls_made == 3
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
    part = {**PART, "table_result_ids": ["q"] if with_table else []}
    judge = Judge([JudgeVerdict(True, reviewed=True), reject(part)])
    loop, store, model, _, events = build([discovery(), query(), final()], judge)
    change_delivery_before_finish(loop)
    result = await stream_result(loop)
    assert result["assistant_text"] == TEXT
    assert result["review"]["completion"] == "partial"
    assert result["review"]["status"] == "approved"
    assert bool(result["answer_tables"]) == with_table
    assert len(judge.briefs) == 2 and model.calls_made == 3
    await assert_history(store, result)
    event = next(shape for name, shape in events if name == "loop_partial_answer_recovered")
    assert event["text_length"] == len(TEXT)
    assert event["table_count"] == int(with_table)


@pytest.mark.parametrize("valid", [True, False])
async def test_finish_applies_only_validated_exact_prose_correction(valid):
    corrected = (
        "There are 120 employees in the available records." if valid else "There are 999 employees."
    )
    judge = Judge(
        [
            JudgeVerdict(True, reviewed=True),
            JudgeVerdict(True, reviewed=True, repair_type="prose", corrected_answer=corrected),
        ]
    )
    loop, store, model, _, _ = build([discovery(), query(), final()], judge)
    change_delivery_before_finish(loop)
    result = await stream_result(loop)
    assert len(judge.briefs) == 2 and model.calls_made == 3
    if valid:
        assert result["assistant_text"] == corrected
        assert len(result["answer_tables"]) == 1
        assert result["review"]["status"] == "approved"
    else:
        assert result["assistant_text"] != corrected
        assert result["review"]["status"] == "rejected"
        assert not result["answer_tables"]
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
    assert "The leave ranking is shown" not in result["assistant_text"]
    assert not result["answer_tables"]
    assert len(judge.briefs) == 2


async def test_finish_rejects_partial_with_missing_evidence_without_more_review():
    judge = Judge([JudgeVerdict(True, reviewed=True), reject({**PART, "evidence": ["missing"]})])
    loop, store, model, _, events = build([discovery(), query(), final()], judge)
    change_delivery_before_finish(loop)
    result = await stream_result(loop)
    assert result["review"]["status"] == "rejected"
    assert result["assistant_text"] != TEXT
    assert len(judge.briefs) == 2 and model.calls_made == 3
    assert ("loop_partial_answer_failed", {"reason": "no_approved_partial"}) in events
    await assert_history(store, result)
