"""BudgetGuard — per-window iteration/token/wall-clock ceilings (D47/D55, design §4.1 step 3d).

One `BudgetGuard` instance represents exactly one "budget window" (design §4.1
step 3d / D55: "continue" past a cap grants a *fresh* window, not more budget
on the same one). `loop/agent_loop.py` constructs a new `BudgetGuard` at the
start of a turn and again for every granted "continue"; the **hard outer
ceiling** on the *number* of windows a single turn may grant itself
(`RuntimeSettings.max_budget_windows`) is tracked by the loop itself via
`PauseCheckpoint.budget_window_count`, not by this class — `BudgetGuard` only
ever knows about its own single window.

THE TOKEN COUNTER MEASURES *SPEND*, NOT *OCCUPANCY* (fixed 2026-08-12). These
are two different questions and this class only answers the second one:

  - OCCUPANCY — "will the next request overflow the model's context window?" —
    is `max(context_k)`, never a sum, and is NOT this class's job. It is
    enforced at the send seam by `context/budget.py::fit_request_to_budget`,
    which trims the FULL canonical list to `RuntimeSettings.request_token_budget()`
    before every `send_turn` (`agent_loop.py::_build_canonical_messages`).
  - SPEND — "how many tokens has this window cost?" — genuinely IS
    Σ(prompt + completion) over the window's round-trips, which is what
    `record_iteration(tokens_used=...)` accumulates.

The counting was always right; the COMPARISON was wrong. `max_token_spend` used
to be fed `settings.model_context_window` (128k), so a sum of whole replayed
requests was measured against an occupancy limit — quadratic in round count, and
a window ended after 6-14 rounds with the real context at 12-31% of the window.
It now takes its own ceiling, `RuntimeSettings.max_window_token_spend`, derived
in that field's comment from measured full-window spend.

CACHED PROMPT TOKENS ARE COUNTED AT FULL WEIGHT, deliberately. Live traces show
most of each request is a cache read (`cache_read: 17920` of `prompt: 19134`),
which bills at a fraction of fresh input — but this ceiling is a runaway
backstop, not a cost model: a cache read still occupies the window and still
bills, and treating it as free would let a runaway run several times longer than
the counter claims. The discount is priced into WHERE the ceiling sits (see
`config.py::max_window_token_spend`), not into the counter, so no provider-
specific `cached_tokens` field has to be plumbed through the model seam.

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
        """Record one model<->tool round-trip (design §4.1 step 3a-3c) against this window.

        *tokens_used* is that round-trip's prompt + completion total as the
        provider reported it — a SPEND increment, not an occupancy reading (see
        the module docstring).
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
