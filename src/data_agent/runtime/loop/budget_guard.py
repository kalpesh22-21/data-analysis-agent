"""BudgetGuard — per-window iteration/token/wall-clock ceilings (D47/D55).

One instance is exactly ONE budget window: a "continue" past a cap grants a FRESH
window, not more budget on the same one. The hard outer ceiling on how many windows a
single turn may grant itself (`RuntimeSettings.max_budget_windows`) is tracked by the
loop via `PauseCheckpoint.budget_window_count`, not here.

THE TOKEN COUNTER MEASURES SPEND, NOT OCCUPANCY. Occupancy — "will the next request
overflow the model's context window?" — is `max(context_k)`, never a sum, and is enforced
at the send seam by `context/budget.py::fit_request_to_budget`. Spend is genuinely
Σ(prompt + completion) over the window's round-trips, and is compared against
`RuntimeSettings.max_window_token_spend`; comparing it against the model context window
instead is quadratic in round count and ends a window at a fraction of real occupancy.

CACHED PROMPT TOKENS ARE COUNTED AT FULL WEIGHT, deliberately: this ceiling is a runaway
backstop, not a cost model. The cache discount is priced into WHERE the ceiling sits (see
`config.py::max_window_token_spend`), not into the counter, so no provider-specific
`cached_tokens` field has to be plumbed through the model seam.

Wall-clock is measured via an injectable monotonic `clock` so tests can simulate elapsed
time without sleeping.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class BudgetUsage:
    """A point-in-time snapshot of one window's consumption (for telemetry)."""

    iterations: int
    token_spend: int
    elapsed_seconds: float
    max_iterations: int
    max_token_spend: int | None
    max_wall_clock_seconds: float


class BudgetGuard:
    """Tracks one budget window's iteration/token-spend/wall-clock consumption."""

    def __init__(
        self,
        *,
        max_iterations: int,
        max_wall_clock_seconds: float,
        max_token_spend: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_iterations = max_iterations
        self._max_wall_clock_seconds = max_wall_clock_seconds
        self._max_token_spend = max_token_spend
        self._clock = clock
        self._start = clock()
        self._iterations = 0
        self._token_spend = 0

    def record_iteration(self, *, tokens_used: int = 0) -> None:
        """Record one model<->tool round-trip against this window.

                *tokens_used* is that round-trip's prompt + completion total as the provider
                reported it — a SPEND increment, not an occupancy reading (see the module
                docstring).
        """
        self._iterations += 1
        self._token_spend += max(0, tokens_used)

    @property
    def exceeded(self) -> bool:
        """True once any of iterations/token-spend/wall-clock has hit its cap."""
        if self._iterations >= self._max_iterations:
            return True
        if self._max_token_spend is not None and self._token_spend >= self._max_token_spend:
            return True
        return (self._clock() - self._start) >= self._max_wall_clock_seconds

    def usage(self) -> BudgetUsage:
        return BudgetUsage(
            iterations=self._iterations,
            token_spend=self._token_spend,
            elapsed_seconds=self._clock() - self._start,
            max_iterations=self._max_iterations,
            max_token_spend=self._max_token_spend,
            max_wall_clock_seconds=self._max_wall_clock_seconds,
        )


__all__ = ["BudgetGuard", "BudgetUsage"]
