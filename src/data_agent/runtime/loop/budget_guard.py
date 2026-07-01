"""BudgetGuard — per-window iteration/token/wall-clock ceilings (D47/D55, design §4.1 step 3d).

One `BudgetGuard` instance represents exactly one "budget window" (design §4.1
step 3d / D55: "continue" past a cap grants a *fresh* window, not more budget
on the same one). `loop/agent_loop.py` constructs a new `BudgetGuard` at the
start of a turn and again for every granted "continue"; the **hard outer
ceiling** on the *number* of windows a single turn may grant itself
(`RuntimeSettings.max_budget_windows`) is tracked by the loop itself via
`PauseCheckpoint.budget_window_count`, not by this class — `BudgetGuard` only
ever knows about its own single window.

Deviation note (flagged for review): `RuntimeSettings` has no explicit
token-budget field for the loop (only `model_context_window`, which feeds the
*context-assembly* history budget, and `max_loop_iterations`/
`max_wall_clock_seconds` for the loop). This module treats
`model_context_window` as the loop's per-window cumulative-token ceiling too
(exceeding the model's own context window within one window is already a hard
failure mode, so reusing the same number is a defensible default) —
`token_budget` is accepted as an explicit, independently-overridable
constructor argument so a future dedicated `RuntimeSettings` field can be
wired in without changing this class's shape.

Wall-clock is measured via an injectable monotonic `clock` callable
(`time.monotonic` by default) so tests can simulate elapsed time
deterministically without real sleeping.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class BudgetUsage:
    """A point-in-time snapshot of one window's consumption (for telemetry)."""

    iterations: int
    tokens: int
    elapsed_seconds: float
    max_iterations: int
    max_tokens: int | None
    max_wall_clock_seconds: float


class BudgetGuard:
    """Tracks one budget window's iteration/token/wall-clock consumption."""

    def __init__(
        self,
        *,
        max_iterations: int,
        max_wall_clock_seconds: float,
        max_tokens: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_iterations = max_iterations
        self._max_wall_clock_seconds = max_wall_clock_seconds
        self._max_tokens = max_tokens
        self._clock = clock
        self._start = clock()
        self._iterations = 0
        self._tokens = 0

    def record_iteration(self, *, tokens_used: int = 0) -> None:
        """Record one model<->tool round-trip (design §4.1 step 3a-3c) against this window."""
        self._iterations += 1
        self._tokens += max(0, tokens_used)

    @property
    def exceeded(self) -> bool:
        """True once any of iterations/tokens/wall-clock has hit its cap."""
        if self._iterations >= self._max_iterations:
            return True
        if self._max_tokens is not None and self._tokens >= self._max_tokens:
            return True
        return (self._clock() - self._start) >= self._max_wall_clock_seconds

    def usage(self) -> BudgetUsage:
        return BudgetUsage(
            iterations=self._iterations,
            tokens=self._tokens,
            elapsed_seconds=self._clock() - self._start,
            max_iterations=self._max_iterations,
            max_tokens=self._max_tokens,
            max_wall_clock_seconds=self._max_wall_clock_seconds,
        )


def new_budget_window(
    *,
    max_iterations: int,
    max_wall_clock_seconds: float,
    max_tokens: int | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> BudgetGuard:
    """Construct a fresh `BudgetGuard` — the D55 "fresh window on continue" seam."""
    return BudgetGuard(
        max_iterations=max_iterations,
        max_wall_clock_seconds=max_wall_clock_seconds,
        max_tokens=max_tokens,
        clock=clock,
    )


__all__ = ["BudgetGuard", "BudgetUsage", "new_budget_window"]
