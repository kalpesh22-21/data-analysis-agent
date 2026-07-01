"""Unit tests for loop/budget_guard.py (Layer 1, pure — deterministic injected clock)."""

from __future__ import annotations

from data_agent.runtime.loop.budget_guard import BudgetGuard, new_budget_window


class _FakeClock:
    """A controllable monotonic clock for deterministic wall-clock tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def test_not_exceeded_below_all_caps() -> None:
    guard = BudgetGuard(max_iterations=5, max_wall_clock_seconds=60, max_tokens=1000)
    guard.record_iteration(tokens_used=100)
    assert guard.exceeded is False


def test_exceeded_at_iteration_cap() -> None:
    guard = BudgetGuard(max_iterations=3, max_wall_clock_seconds=999)
    for _ in range(3):
        guard.record_iteration()
    assert guard.exceeded is True


def test_not_exceeded_just_below_iteration_cap() -> None:
    guard = BudgetGuard(max_iterations=3, max_wall_clock_seconds=999)
    guard.record_iteration()
    guard.record_iteration()
    assert guard.exceeded is False


def test_exceeded_at_token_cap() -> None:
    guard = BudgetGuard(max_iterations=999, max_wall_clock_seconds=999, max_tokens=500)
    guard.record_iteration(tokens_used=300)
    assert guard.exceeded is False
    guard.record_iteration(tokens_used=200)
    assert guard.exceeded is True


def test_no_token_cap_means_tokens_never_trigger() -> None:
    guard = BudgetGuard(max_iterations=999, max_wall_clock_seconds=999, max_tokens=None)
    guard.record_iteration(tokens_used=10_000_000)
    assert guard.exceeded is False


def test_exceeded_at_wall_clock_cap_via_injected_clock() -> None:
    clock = _FakeClock()
    guard = BudgetGuard(max_iterations=999, max_wall_clock_seconds=10, clock=clock)
    clock.advance(9.9)
    assert guard.exceeded is False
    clock.advance(0.2)
    assert guard.exceeded is True


def test_usage_snapshot_reports_current_counters() -> None:
    clock = _FakeClock()
    guard = BudgetGuard(max_iterations=15, max_wall_clock_seconds=60, max_tokens=2000, clock=clock)
    guard.record_iteration(tokens_used=50)
    guard.record_iteration(tokens_used=75)
    clock.advance(5.0)

    usage = guard.usage()
    assert usage.iterations == 2
    assert usage.tokens == 125
    assert usage.elapsed_seconds == 5.0
    assert usage.max_iterations == 15
    assert usage.max_tokens == 2000
    assert usage.max_wall_clock_seconds == 60


def test_new_budget_window_is_independent_from_prior_window() -> None:
    clock = _FakeClock()
    first = new_budget_window(max_iterations=1, max_wall_clock_seconds=60, clock=clock)
    first.record_iteration()
    assert first.exceeded is True

    second = new_budget_window(max_iterations=1, max_wall_clock_seconds=60, clock=clock)
    assert second.exceeded is False  # fresh window (D55) — no carryover from `first`
