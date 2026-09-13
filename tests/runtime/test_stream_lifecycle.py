"""Request-owned turn workers terminate before their SSE response exits."""

import asyncio

import pytest
from starlette.requests import ClientDisconnect

from data_agent.runtime.app import _stream_turn, _TurnStreamingResponse
from data_agent.runtime.loop.agent_loop import TurnOutcome
from data_agent.runtime.observability.progress import ProgressEmitter


class BlockedTurn:
    def __init__(self):
        self.emitter = ProgressEmitter()
        self.started = asyncio.Event()
        self.cleaned = asyncio.Event()
        self.task = None

    async def __call__(self):
        self.task = asyncio.current_task()
        try:
            self.emitter.observe("loop_model_call_start", {})
            self.started.set()
            await asyncio.Event().wait()
        finally:
            # Cancellation must allow awaited provider/resource cleanup to finish.
            await asyncio.sleep(0)
            self.cleaned.set()

    def assert_stopped(self):
        assert self.task is not None and self.task.done()
        assert self.task.cancelled()
        assert self.cleaned.is_set()


async def test_closing_generator_cancels_and_awaits_turn():
    turn = BlockedTurn()
    stream = _stream_turn(turn, turn.emitter)
    assert "event: progress" in await anext(stream)
    await stream.aclose()
    turn.assert_stopped()


@pytest.mark.parametrize("phase", ["progress_wait", "send"])
async def test_asgi_disconnect_cleans_worker_inside_cancel_scope(phase):
    turn = BlockedTurn()
    response = _TurnStreamingResponse(_stream_turn(turn, turn.emitter))
    sent = asyncio.Event()

    async def send(message):
        if message["type"] == "http.response.body":
            sent.set()
            if phase == "send":
                await asyncio.Event().wait()

    async def receive():
        await sent.wait()
        return {"type": "http.disconnect"}

    # ASGI <2.4 uses an AnyIO task-group cancel scope on http.disconnect.
    await asyncio.wait_for(
        response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send), 2
    )
    turn.assert_stopped()


async def test_asgi_socket_failure_closes_suspended_generator():
    turn = BlockedTurn()
    response = _TurnStreamingResponse(_stream_turn(turn, turn.emitter))

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("synthetic disconnected socket")

    async def receive():
        raise AssertionError("ASGI 2.4 should detect this disconnect through send")

    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    turn.assert_stopped()


async def test_request_task_cancellation_propagates_after_worker_cleanup():
    turn = BlockedTurn()
    stream = _stream_turn(turn, turn.emitter)

    async def consume():
        async for _ in stream:
            pass

    consumer = asyncio.create_task(consume())
    await turn.started.wait()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    turn.assert_stopped()


async def test_completed_turn_keeps_progress_and_terminal_result():
    emitter = ProgressEmitter()

    async def work():
        emitter.observe("loop_model_call_start", {})
        return TurnOutcome(
            status="done",
            assistant_text="Synthetic answer",
            pending_question=None,
            tool_calls_made=0,
        )

    events = [event async for event in _stream_turn(work, emitter)]
    assert len(events) == 2
    assert events[0].startswith("event: progress")
    assert events[1].startswith("event: result")
    assert "Synthetic answer" in events[1]


async def test_worker_failure_keeps_sanitized_terminal_error():
    async def work():
        raise RuntimeError("synthetic provider detail")

    events = [event async for event in _stream_turn(work, ProgressEmitter())]
    assert len(events) == 1
    assert events[0].startswith("event: error")
    assert "INTERNAL_ERROR" in events[0]
    assert "synthetic provider detail" not in events[0]
