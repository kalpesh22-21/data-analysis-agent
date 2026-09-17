"""Task-local cancellation diagnostics for run/resume and cooperative checkpoints."""

import asyncio
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps

from data_agent.runtime.observability.tracing import mark_current_span_error

MODEL_CALL_TIMEOUT_EVENT = "loop_model_call_timeout"
TURN_ABORTED_EVENT = "loop_turn_aborted"
MODEL_CALL_TIMEOUT_TEXT = (
    "I ran out of time while preparing the answer. Any completed results I can share are "
    "shown, but I could not finish the remaining work."
)


@dataclass
class TurnPhase:
    phase: str = "setup"
    iteration: int = 0


_phase: ContextVar[TurnPhase | None] = ContextVar("runtime_turn_phase", default=None)


def observe_turn_abort(method):
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        state = TurnPhase()
        token = _phase.set(state)
        try:
            return await method(self, *args, **kwargs)
        except asyncio.CancelledError:
            try:
                mark_current_span_error("cancelled")
                self._observer(
                    TURN_ABORTED_EVENT, {"phase": state.phase, "iteration": state.iteration}
                )
            except Exception:
                logging.getLogger(__name__).warning("Cancellation telemetry could not be emitted")
            raise
        finally:
            _phase.reset(token)

    return wrapped


async def cancellation_checkpoint(phase: str, iteration: int | None = None) -> None:
    state = _phase.get()
    if state is not None:
        state.phase = phase
        if iteration is not None:
            state.iteration = iteration
    # In-memory stores and scripted clients may never suspend. Yield before
    # another round or dispatch so an already-requested cancellation can land.
    await asyncio.sleep(0)
