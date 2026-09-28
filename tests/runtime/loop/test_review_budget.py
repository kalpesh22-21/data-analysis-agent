"""Review deadlines preserve bounded time for validation after an explicit rejection."""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.loop import delivery
from data_agent.runtime.loop.answer_judge import JudgeVerdict
from tests.runtime.test_harness_improvements import (
    CREDS,
    Judge,
    batch,
    build,
    discovery,
    finish,
    query,
    run,
)


@pytest.mark.parametrize(
    "configured,first_timeout,last_timeout", [(None, 60.0, 60.0), (90.0, 30.0, 36.91)]
)
async def test_slow_rejection_leaves_full_timeout_for_corrected_answer(
    monkeypatch, configured, first_timeout, last_timeout
):
    now = [0.0]
    deadlines = []
    real_wait_for = asyncio.wait_for

    async def record_wait_for(awaitable, timeout):
        deadlines.append(timeout)
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", record_wait_for)
    monkeypatch.setattr(delivery, "time", SimpleNamespace(monotonic=lambda: now[0]))

    class SlowJudge(Judge):
        timeout_seconds = 60.0

        async def review(self, brief):
            # Reproduce the observed first rejection and slow repair approval without sleeping.
            now[0] += 23.09 if not self.briefs else 30.88
            return await super().review(brief)

    judge = SlowJudge(
        [
            JudgeVerdict(False, "contradicts_result", "Use the executed result.", True),
            JudgeVerdict(True, reviewed=True),
        ]
    )
    loop, _, _, _, _ = build(
        [
            discovery(),
            query(),
            batch(finish("There are 125 employees.")),
            batch(finish()),
        ],
        judge,
    )
    loop._answer_judge_review_budget_seconds = configured
    out = await run(loop)
    assert out.assistant_text == "There are 120 employees."
    assert out.review["status"] == "approved"
    assert len(judge.briefs) == 2
    assert first_timeout in deadlines
    assert deadlines[-1] == pytest.approx(last_timeout)


async def test_ordinary_reviews_cannot_spend_repair_reserve(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(delivery, "time", SimpleNamespace(monotonic=lambda: now[0]))
    calls = []

    async def review(brief):
        calls.append(brief)
        now[0] += 30
        return JudgeVerdict(True, reviewed=True)

    loop = SimpleNamespace(_answer_judge=SimpleNamespace(review=review, timeout_seconds=30))
    context = delivery.DeliveryContext(None, review_seconds=60, repair_reserve_seconds=30)
    token = delivery.CURRENT_DELIVERY.set(context)
    try:
        assert (await delivery.review_once(loop, "initial")).reviewed
        assert not (await delivery.review_once(loop, "extra")).reviewed
        assert context.review_seconds == 30
        assert (await delivery.review_once(loop, "repair", repair=True)).reviewed
        assert context.review_seconds == 0
        assert not (await delivery.review_once(loop, "extra repair", repair=True)).reviewed
        assert calls == ["initial", "repair"]
    finally:
        delivery.CURRENT_DELIVERY.reset(token)


async def test_failed_repair_charges_budget_and_preserves_rejection(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(delivery, "time", SimpleNamespace(monotonic=lambda: now[0]))

    contexts = []

    class TimeoutJudge(Judge):
        timeout_seconds = 30.0

        async def review(self, brief):
            if self.briefs:
                contexts.append(delivery.CURRENT_DELIVERY.get())
                now[0] += 30
                raise TimeoutError
            return await super().review(brief)

    judge = TimeoutJudge([JudgeVerdict(False, "contradicts_result", "Fix the count.", True)])
    loop, store, _, _, _ = build(
        [
            discovery(),
            query(),
            batch(finish("There are 125 employees.")),
            batch(finish()),
        ],
        judge,
    )
    out = await run(loop)
    assert out.review["status"] == "rejected"
    assert not out.answer_tables
    assert contexts[0].review_seconds == 60.0
    assert len(contexts) == 1  # Do not retry an unavailable judge on the exit path.
    assert contexts[0].repair_reserve_seconds == 0.0
    assert contexts[0].terminal_reserve_seconds == 30.0
    assert (await store.get_or_create_session(CREDS.session_id)).review_states["0"]["violation"]


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_invalid_total_review_budget_is_rejected(value):
    with pytest.raises(ValidationError):
        RuntimeSettings(_env_file=None, answer_judge_review_budget_seconds=value)


def test_total_review_budget_reads_environment(monkeypatch):
    monkeypatch.setenv("ANSWER_JUDGE_REVIEW_BUDGET_SECONDS", "150")
    assert RuntimeSettings(_env_file=None).answer_judge_review_budget_seconds == 150


async def test_terminal_reserve_survives_exhausted_proposal_and_repair(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(delivery, "time", SimpleNamespace(monotonic=lambda: now[0]))
    calls = []

    async def review(brief):
        calls.append(brief)
        now[0] += 30
        return JudgeVerdict(True, reviewed=True)

    loop = SimpleNamespace(_answer_judge=SimpleNamespace(review=review, timeout_seconds=30))
    context = delivery.DeliveryContext(
        None,
        review_seconds=90,
        repair_reserve_seconds=30,
        terminal_reserve_seconds=30,
    )
    token = delivery.CURRENT_DELIVERY.set(context)
    try:
        assert (await delivery.review_once(loop, "initial")).reviewed
        assert not (await delivery.review_once(loop, "extra initial")).reviewed
        assert (await delivery.review_once(loop, "repair", repair=True)).reviewed
        assert not (await delivery.review_once(loop, "extra repair", repair=True)).reviewed
        assert context.review_seconds == 30
        assert (await delivery.review_once(loop, "partial", terminal=True)).reviewed
        assert context.review_seconds == 0
        assert calls == ["initial", "repair", "partial"]
    finally:
        delivery.CURRENT_DELIVERY.reset(token)


@pytest.mark.parametrize("total,initial_timeout", [(180.0, 60.0), (90.0, 30.0)])
@pytest.mark.parametrize("final_result", ["approved", "rejected", "timeout"])
async def test_final_delivery_uses_remaining_reserves_after_initial_timeout(
    monkeypatch, total, initial_timeout, final_result
):
    """Reproduce the live timeout without waiting: delivery must make a real call."""
    now = [0.0]
    deadlines = []
    contexts = []
    real_wait_for = asyncio.wait_for

    async def record_wait_for(awaitable, timeout):
        deadlines.append(timeout)
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", record_wait_for)
    monkeypatch.setattr(delivery, "time", SimpleNamespace(monotonic=lambda: now[0]))

    class TimedJudge(Judge):
        timeout_seconds = 60.0

        async def review(self, brief):
            self.briefs.append(brief)
            contexts.append(delivery.CURRENT_DELIVERY.get())
            if len(self.briefs) == 1:
                now[0] += initial_timeout
                raise TimeoutError
            if final_result == "timeout":
                now[0] += 60
                raise TimeoutError
            now[0] += 10
            return (
                JudgeVerdict(True, reviewed=True)
                if final_result == "approved"
                else JudgeVerdict(
                    False, "contradicts_result", "The count is not supported.", reviewed=True
                )
            )

    judge = TimedJudge()
    loop, _, _, _, _ = build([discovery(), query(), batch(finish())], judge)
    loop._answer_judge_review_budget_seconds = total
    out = await run(loop)
    assert deadlines[:2] == [initial_timeout, 60.0]
    assert (
        out.review["status"]
        == {"approved": "approved", "rejected": "rejected", "timeout": "exhausted"}[final_result]
    )
    assert len(judge.briefs) == (3 if final_result == "timeout" and total == 180 else 2)
    assert contexts[-1].review_seconds >= 0
    assert now[0] <= total
    if final_result == "timeout":
        assert contexts[-1].review_seconds == 0
    if final_result == "rejected":
        assert "120" not in out.assistant_text
        assert not out.answer_tables
