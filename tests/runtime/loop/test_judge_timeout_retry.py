import asyncio
from types import SimpleNamespace

import pytest

from data_agent.runtime.loop.answer_judge import AnswerJudge
from data_agent.runtime.loop.delivery import CURRENT_DELIVERY, DeliveryContext, review_once
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from tests.runtime.loop.test_answer_judge_unit import _brief, _verdict_turn


class SlowClient:
    def __init__(self, retry_result=None):
        self.retry_result = retry_result
        self.retry_calls = 0
        self.cancelled = False

    async def send_turn(self, messages, tools):
        try:
            await asyncio.sleep(10)
        finally:
            self.cancelled = True

    def without_thinking(self, kwargs):
        assert kwargs == {"enable_thinking": False, "thinking": False}
        self.retry_calls += 1
        return ScriptedModelClient([self.retry_result]) if self.retry_result else self


@pytest.mark.parametrize("approved", [True, False])
async def test_timeout_retries_once_and_honors_verdict(approved):
    client = SlowClient(_verdict_turn(approved, violation="unsupported_by_evidence" if not approved else "", feedback="Unsupported claim." if not approved else ""))
    judge = AnswerJudge(client, 20000, timeout_seconds=.01, timeout_retry_seconds=.05)
    events = []
    loop = SimpleNamespace(_answer_judge=judge, _observer=lambda e,p: events.append(e))
    context = DeliveryContext(None, review_seconds=.3, repair_reserve_seconds=.1, terminal_reserve_seconds=.1)
    token = CURRENT_DELIVERY.set(context)
    try:
        verdict = await review_once(loop, _brief())
    finally:
        CURRENT_DELIVERY.reset(token)
    assert verdict.approved == approved and verdict.reviewed
    assert client.cancelled and client.retry_calls == 1
    assert context.review_seconds < .3
    assert not context.review_unavailable
    assert events == ["loop_answer_judge_timeout_retry"]


@pytest.mark.parametrize("enabled,budget", [(False,.2), (True,.01)])
async def test_disabled_or_exhausted_retry_does_not_call_provider(enabled,budget):
    client = SlowClient()
    judge = AnswerJudge(client, 20000, timeout_seconds=.01, timeout_retry_enabled=enabled)
    context = DeliveryContext(None, review_seconds=budget, repair_reserve_seconds=0, terminal_reserve_seconds=0)
    token = CURRENT_DELIVERY.set(context)
    try:
        with pytest.raises(TimeoutError):
            await review_once(SimpleNamespace(_answer_judge=judge, _observer=lambda *a:None), _brief())
    finally:
        CURRENT_DELIVERY.reset(token)
    assert context.review_failure_reason == "timeout"


async def test_second_timeout_does_not_recurse():
    client = SlowClient()
    judge = AnswerJudge(client, 20000, timeout_seconds=.01, timeout_retry_seconds=.01)
    with pytest.raises(TimeoutError):
        await review_once(SimpleNamespace(_answer_judge=judge, _observer=lambda *a:None), _brief())
    assert client.retry_calls == 1


async def test_cancellation_never_retries():
    client = SlowClient()
    judge = AnswerJudge(client, 20000, timeout_seconds=10)
    task = asyncio.create_task(review_once(SimpleNamespace(_answer_judge=judge), _brief()))
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.retry_calls == 0


@pytest.mark.parametrize("mode", ["success", "error"])
async def test_non_timeout_never_retries(mode):
    class Client(SlowClient):
        async def send_turn(self, messages, tools):
            if mode == "error":
                raise RuntimeError("provider unavailable")
            return _verdict_turn(True)

    client = Client()
    verdict = await review_once(SimpleNamespace(_answer_judge=AnswerJudge(client, 20000)), _brief())
    assert verdict.approved
    assert verdict.reviewed == (mode == "success")
    assert client.retry_calls == 0


async def test_retry_cannot_consume_terminal_reserve():
    client = SlowClient()
    context = DeliveryContext(None, review_seconds=.06, repair_reserve_seconds=.02, terminal_reserve_seconds=.02)
    token = CURRENT_DELIVERY.set(context)
    try:
        with pytest.raises(TimeoutError):
            await review_once(SimpleNamespace(
                _answer_judge=AnswerJudge(client, 20000, timeout_seconds=.02, timeout_retry_seconds=1),
                _observer=lambda *a: None,
            ), _brief())
    finally:
        CURRENT_DELIVERY.reset(token)
    assert context.review_seconds >= .01  # allowance for scheduler overhead
    assert context.repair_reserve_seconds < .01
    assert context.terminal_reserve_seconds == .02
