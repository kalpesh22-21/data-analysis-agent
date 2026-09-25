"""Concurrent summary production, ordered SSE delivery, and terminal drain."""

import asyncio
import json

import pytest

from data_agent.runtime.app import _stream_turn
from data_agent.runtime.observability.progress import ProgressEmitter, combine_observers
from data_agent.runtime.observability.progress_summarizer import SUMMARY_DEADLINE_SECONDS
from tests.runtime.loop.test_progress_summary_loop import (
    SESSION_ID,
    _build_loop,
    _credentials,
    _query_then_done,
    _run_query_mcp,
)

pytestmark = pytest.mark.usefixtures("answer_tools", "blueprint_consulted")


async def test_summaries_run_concurrently_but_cannot_overtake_reserved_slots():
    emitter = ProgressEmitter()
    gates = [asyncio.Event(), asyncio.Event()]
    started = [asyncio.Event(), asyncio.Event()]

    async def summary(index):
        started[index].set()
        await gates[index].wait()
        return f"Business task {index}"

    tasks = []
    for index in range(2):
        task = asyncio.create_task(summary(index))
        tasks.append(task)
        payload = {"tool_name": "runQuery", "tool_call_id": str(index)}
        emitter.observe("tool_progress_summary_pending", {**payload, "summary_task": task})
        emitter.observe("tool_dispatch_start", payload)
        emitter.observe("tool_dispatch_ok", payload)
    emitter.close()
    received = []

    async def consume():
        async for event in emitter.stream():
            received.append(event)

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 1)
    gates[1].set()
    await tasks[1]
    assert not received  # A fast later summary cannot pass the earlier placeholder.
    gates[0].set()
    await asyncio.wait_for(consumer, 1)
    assert [event.shape["tool_call_id"] for event in received] == ["0"] * 3 + ["1"] * 3
    assert received[0].step == "Business task 0"
    assert received[3].step == "Business task 1"


class ControlledSummary:
    def __init__(self, outcome="summary"):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.outcome = outcome
        self.task = None
        self.was_cancelled = False

    async def summarize(self, name, args, **kwargs):
        self.task = asyncio.current_task()
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.was_cancelled = True
            raise
        if self.outcome == "error":
            raise RuntimeError("private provider failure")
        return None if self.outcome == "empty" else "Finding the requested employee details"


def stream_fixture(summary):
    emitter = ProgressEmitter()
    observed = []
    loop, _ = _build_loop(
        model_client=_query_then_done(),
        mcp_client=_run_query_mcp(),
        observer=combine_observers(emitter.observe, lambda e, p: observed.append((e, p))),
        progress_summarizer=summary,
    )
    stream = _stream_turn(
        lambda: loop.run(
            session_id=SESSION_ID, credentials=_credentials(), user_message="show codes"
        ),
        emitter,
    )
    return stream, observed


async def collect(stream, frames):
    async for frame in stream:
        frames.append(frame)


@pytest.mark.parametrize("outcome", ["summary", "error", "empty", "timeout"])
async def test_real_turn_executes_before_summary_and_result_follows_drained_progress(
    outcome, monkeypatch
):
    assert SUMMARY_DEADLINE_SECONDS == 5.0
    if outcome == "timeout":
        monkeypatch.setattr("data_agent.runtime.loop.agent_loop.SUMMARY_DEADLINE_SECONDS", 0.05)
    summary = ControlledSummary(outcome)
    stream, observed = stream_fixture(summary)
    frames = []
    consumer = asyncio.create_task(collect(stream, frames))
    await asyncio.wait_for(summary.started.wait(), 1)
    # The real tool finished while its summary was blocked, but no result may ship yet.
    assert any(e == "tool_dispatch_ok" for e, _ in observed)
    assert not any("event: result" in frame for frame in frames)
    assert not any('"tool_call_id": "call_1"' in frame for frame in frames)
    if outcome != "timeout":
        summary.release.set()
    await asyncio.wait_for(consumer, 1)
    progress = [
        json.loads(frame.split("data: ")[1]) for frame in frames if "event: progress" in frame
    ]
    paired = [event for event in progress if event["shape"].get("tool_call_id") == "call_1"]
    assert len(paired) == 3
    assert paired[0]["step"] == (
        "Finding the requested employee details"
        if outcome == "summary"
        else "finding the requested information"
    )
    assert paired[1]["step"] == "finding the requested information…"
    assert "finished" in paired[2]["step"]
    assert "event: result" in frames[-1]
    assert "summary_task" not in "".join(frames)
    assert "private provider" not in "".join(frames)
    if outcome == "timeout":
        assert summary.was_cancelled


async def test_disconnect_cancels_pending_summary_producer():
    summary = ControlledSummary()
    stream, _ = stream_fixture(summary)
    consumer = asyncio.create_task(collect(stream, []))
    await asyncio.wait_for(summary.started.wait(), 1)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert summary.task.done()
    assert summary.task.cancelled()
