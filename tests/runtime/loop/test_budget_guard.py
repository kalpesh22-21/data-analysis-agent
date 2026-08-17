"""Unit tests for loop/budget_guard.py (Layer 1, pure — deterministic injected clock).

The token counter SUMS, and that is correct — it measures this window's SPEND
(prompt + completion per round-trip), not the occupancy of the model's context
window. The 2026-08-12 fix did not change the counting; it renamed the ceiling and
gave it its own setting so a spend sum stops being compared against an occupancy
limit. Occupancy is covered by `tests/runtime/loop/test_total_request_token_budget.py`
and `tests/runtime/context/test_budget.py`.
"""

from __future__ import annotations

from data_agent.runtime.loop.budget_guard import BudgetGuard


class _FakeClock:
    """A controllable monotonic clock for deterministic wall-clock tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def test_not_exceeded_below_all_caps() -> None:
    guard = BudgetGuard(max_iterations=5, max_wall_clock_seconds=60, max_token_spend=1000)
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


def test_exceeded_at_token_spend_cap() -> None:
    guard = BudgetGuard(max_iterations=999, max_wall_clock_seconds=999, max_token_spend=500)
    guard.record_iteration(tokens_used=300)
    assert guard.exceeded is False
    guard.record_iteration(tokens_used=200)
    assert guard.exceeded is True


def test_no_spend_cap_means_tokens_never_trigger() -> None:
    guard = BudgetGuard(max_iterations=999, max_wall_clock_seconds=999, max_token_spend=None)
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
    guard = BudgetGuard(max_iterations=15, max_wall_clock_seconds=60, max_token_spend=2000, clock=clock)
    guard.record_iteration(tokens_used=50)
    guard.record_iteration(tokens_used=75)
    clock.advance(5.0)

    usage = guard.usage()
    assert usage.iterations == 2
    assert usage.token_spend == 125
    assert usage.elapsed_seconds == 5.0
    assert usage.max_iterations == 15
    assert usage.max_token_spend == 2000
    assert usage.max_wall_clock_seconds == 60


def test_a_fresh_guard_is_independent_from_the_prior_window() -> None:
    clock = _FakeClock()
    first = BudgetGuard(max_iterations=1, max_wall_clock_seconds=60, clock=clock)
    first.record_iteration()
    assert first.exceeded is True

    second = BudgetGuard(max_iterations=1, max_wall_clock_seconds=60, clock=clock)
    assert second.exceeded is False  # fresh window (D55) — no carryover from `first`
