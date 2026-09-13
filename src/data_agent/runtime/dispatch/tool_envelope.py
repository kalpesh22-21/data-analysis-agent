"""dispatch/tool_envelope.py — the ONE TOOL-span envelope every runtime tool wraps itself in.

`searchBlueprints`/`getBlueprint`/`searchKnowledge`, `runBlueprint` and `updateAnalysisState`
each grew the same twenty-odd lines: emit `tool_dispatch_start`, branch on a `None` tracer,
open a `tool.<name>` span optimistically as `status="ok"`, run the work INSIDE it, overwrite
the status (and add `tool.error_code`) from the result, close, then emit
`tool_dispatch_ok`/`error`. Five copies of a sequence whose ORDER is the contract meant five
places for a copy to drift silently — and the drift is invisible, because a span with a
stale optimistic `ok` looks exactly like a span that succeeded.

WHY IT LIVES IN `dispatch/` AND NOT `observability/`. The edge `dispatch -> observability` is
already load-bearing and acyclic; the reverse is the documented lazy-import cycle. This
module is imported by tool implementations that ALREADY import `tool_dispatcher`, so it adds
no edge at all.

TWO GUARANTEES THE HAND-COPIES DID NOT ALL HAVE.

1. `record_exception=False` on every envelope span. A tool's crash guard means an exception
   normally never reaches the span, but if one ever did, OTel's default would write
   `exception.message`/`exception.stacktrace` onto a TOOL span — free-text derived from a
   query, a slot value or a row. The span STATUS is still set on exception
   (`set_status_on_exception=True` in `tracing.span`), so the error stays visible; only the
   content-bearing detail is withheld. This is the same posture the learning plane's spans
   already take, applied to the one surface that had it site-by-site.

2. The status overwrite is STRUCTURALLY unavoidable. The span opens `status="ok"` before the
   work runs, so forgetting the overwrite does not fail loudly — it reports every error as a
   success. The body of the `with` is therefore a SINGLE EXPRESSION (`_stamp(span, await
   work())`): there is nowhere to insert an early return between the work and the stamp, and
   the stamp cannot be dropped without deleting the line that produces the result.

`_span_args` IS ABSTRACT WITH NO DEFAULT, DELIBERATELY. A base that defaulted to "forward
`model_args`" would put the model's free text on a span for any subclass that forgot to
think about it — D25 fail-closed means a new tool cannot be written without an explicit
answer to "what shape of this call is safe to trace?". `updateAnalysisState` is the reason
this is not theoretical: its answer is `{"intent_count": n}` and NOTHING else, even under
`otlp_disable_redaction`, because its args are model-authored descriptions of the user's
question.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal, Protocol

from data_agent.runtime.dispatch.tool_dispatcher import (
    ToolObserver,
    ToolResult,
    _default_observer,
)
from data_agent.runtime.observability import tracing

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from opentelemetry.trace import Span, Tracer

    from data_agent.runtime.auth.credentials import RuntimeCredentials
    from data_agent.runtime.loop.agent_loop import TurnContext

_logger = logging.getLogger(__name__)


class SpanOutcome(Protocol):
    """The TWO fields the envelope reads off a tool's result to close its span.

    `ToolResult` and `resolve_values.ResolveOutcome` both satisfy it, which is the whole
    reason it exists: `resolveValues` runs a `ResolveOutcome`-returning inner inside the
    span and maps it to a `ToolResult` OUTSIDE, so the span half cannot be typed against
    `ToolResult` without either a lie or a second copy of the span code. Formalising the
    duck-typing is the smaller of the two.
    """

    status: Literal["ok", "denied", "error"]
    error_code: str | None


def _stamp[OutcomeT: SpanOutcome](span: Span, outcome: OutcomeT) -> OutcomeT:
    """Overwrite the span's optimistic `status="ok"` with what actually happened.

    Returns *outcome* so the caller's `with` body stays a single expression — see the
    module docstring's guarantee 2. `tool.error_code` is set only when there IS one, so an
    `ok` span carries no empty attribute (the shape every pinned test asserts).
    """
    span.set_attribute("tool.status", outcome.status)
    if outcome.error_code is not None:
        span.set_attribute("tool.error_code", outcome.error_code)
    return outcome


async def in_tool_span[OutcomeT: SpanOutcome](
    tracer: Tracer | None,
    *,
    tool_name: str,
    args: dict[str, Any],
    reveal_complex_args: bool = False,
    work: Callable[[], Awaitable[OutcomeT]],
) -> OutcomeT:
    """Run *work* inside one `tool.<tool_name>` TOOL span and stamp its real status.

    A `None` *tracer* runs *work* unwrapped — tracing is optional everywhere in the runtime
    and no tool may depend on it, so this branch is the contract rather than a convenience.
    """
    if tracer is None:
        return await work()
    with tracing.tool_span(
        tracer,
        tool_name=tool_name,
        args=args,
        status="ok",
        error_code=None,
        reveal_complex_args=reveal_complex_args,
        record_exception=False,
    ) as span:
        return _stamp(span, await work())


class RuntimeToolBase(ABC):
    """The full envelope: symmetric progress events around one status-stamped TOOL span.

    A subclass supplies `tool_name`, the two internal-error constants, `_span_args` and
    `_execute`. Everything else — the event order, the tracer-`None` branch, the crash
    guard, the error `ToolResult` shape — is inherited and is the same on every site.
    """

    emits_dispatch_events = True
    tool_name: str = ""

    # The last-resort code/message for a crash that slipped every inner guard. PER-TOOL,
    # not boilerplate: this text is the model's ROUTING instruction ("answer from the raw
    # tools instead" vs "please try again"), so a shared string would send the model the
    # wrong way from four different failures.
    _INTERNAL_ERROR_CODE: str = ""
    _INTERNAL_ERROR_MESSAGE: str = ""

    # The D44 provenance every error `ToolResult` from `_error` carries. `frozenset()` is
    # DETERMINED-EMPTY (the entry names no column, so it survives any later scope
    # narrowing); `None` is UNDETERMINED and is dropped from replay unconditionally. The
    # two are not interchangeable and the choice is per-tool — `runBlueprint` must stay
    # `None` because its errors can carry an inner denial whose footprint is unknown here.
    _ERROR_PROVENANCE: frozenset[tuple[str, str]] | None = frozenset()

    # Exceptions `_guarded` routes to `_on_guarded_exception` instead of the generic
    # internal-error arm. Empty by default (`except ():` never matches), so a tool with no
    # domain exception of its own inherits exactly the B4-parity crash guard.
    _GUARDED_EXCEPTIONS: tuple[type[Exception], ...] = ()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # `cls.__abstractmethods__` is NOT yet populated here (ABCMeta sets it AFTER
        # `type.__new__` runs this hook), so abstractness is probed off the methods
        # themselves — an intermediate base like `_ReadTool` must not be required to name
        # a tool it does not implement.
        if any(
            getattr(getattr(cls, name, None), "__isabstractmethod__", False)
            for name in RuntimeToolBase.__abstractmethods__
        ):
            return
        if not (cls.tool_name and cls._INTERNAL_ERROR_CODE and cls._INTERNAL_ERROR_MESSAGE):
            raise TypeError(
                f"{cls.__name__} must define tool_name, _INTERNAL_ERROR_CODE and "
                "_INTERNAL_ERROR_MESSAGE — an empty one would ship a tool whose crash "
                "returns a nameless, codeless error the loop cannot route on"
            )

    def __init__(
        self,
        *,
        observer: ToolObserver = _default_observer,
        tracer: Tracer | None = None,
        disable_redaction: bool = False,
    ) -> None:
        self._observer = observer
        self._tracer = tracer
        # Access-controlled TELEMETRY DEBUG switch (RuntimeSettings.otlp_disable_redaction).
        # It reaches the span and NOTHING else: `_execute` always receives the raw
        # `model_args`, so no enforcement path can depend on it. A tool whose `_span_args`
        # never forwards model args (see `updateAnalysisState`) is unaffected by it
        # entirely, which is the point of making `_span_args` the fail-closed seam.
        self._disable_redaction = disable_redaction

    async def run(
        self,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: TurnContext | None = None,
        tool_call_id: str | None = None,
    ) -> ToolResult:
        """The envelope. *turn* (03 §C.1) is threaded to `_execute` by every site; the
        tools that do not need it accept and ignore it, so the `RuntimeTool` protocol has
        ONE signature rather than two shapes the dispatch site has to tell apart."""
        self._observer(
            "tool_dispatch_start", {"tool_name": self.tool_name, "tool_call_id": tool_call_id}
        )
        result = await in_tool_span(
            self._tracer,
            tool_name=self.tool_name,
            args=self._span_args(model_args),
            reveal_complex_args=self._disable_redaction,
            work=lambda: self._guarded(model_args, credentials, turn),
        )
        if result.status == "ok":
            self._observer(
                "tool_dispatch_ok", {"tool_name": self.tool_name, "tool_call_id": tool_call_id}
            )
        else:
            self._observer(
                "tool_dispatch_denied" if result.status == "denied" else "tool_dispatch_error",
                {
                    "tool_name": self.tool_name,
                    "tool_call_id": tool_call_id,
                    "error_code": result.error_code,
                },
            )
        return result

    @abstractmethod
    def _span_args(self, model_args: dict[str, Any]) -> dict[str, Any]:
        """The SHAPE of this call that is safe to put on a span (D25). No default — see
        the module docstring. Most sites answer `tool_span_args(...)`; a site whose args
        are model-authored prose answers with a count."""

    async def _guarded(
        self,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: TurnContext | None,
    ) -> ToolResult:
        """B4-parity crash guard: a programming error must never abort the turn nor leak
        `str(exc)` to the model. The real exception is logged server-side only.

        A `_GUARDED_EXCEPTIONS` match goes to `_on_guarded_exception`; if THAT raises, the
        internal-error arm is re-entered explicitly, because Python never consults a
        sibling `except` for an exception raised inside another one — without the inner
        try the re-raise would escape this tool and be reported by the loop's outer guard
        under a generic code instead of this tool's own.
        """
        try:
            return await self._execute(model_args, credentials, turn)
        except self._GUARDED_EXCEPTIONS as handled:
            try:
                return self._on_guarded_exception(handled, model_args)
            except Exception:  # noqa: BLE001 - never abort the turn / leak str(exc)
                return self._internal_error(credentials)
        except Exception:  # noqa: BLE001 - never abort the turn / leak str(exc)
            return self._internal_error(credentials)

    def _internal_error(self, credentials: RuntimeCredentials) -> ToolResult:
        """The single internal-error arm. Call sites are inside an `except`, which is what
        gives `_logger.exception` its traceback."""
        _logger.exception("%s internal error (session=%s)", self.tool_name, credentials.session_id)
        return self._error(self._INTERNAL_ERROR_CODE, self._INTERNAL_ERROR_MESSAGE, retryable=False)

    def _on_guarded_exception(
        self, exc: Exception, model_args: dict[str, Any]
    ) -> ToolResult:  # pragma: no cover - unreachable while _GUARDED_EXCEPTIONS is empty
        """Handle one of `_GUARDED_EXCEPTIONS`. Only a tool that declares them needs this."""
        raise exc

    @abstractmethod
    async def _execute(
        self,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: TurnContext | None,
    ) -> ToolResult:
        """The tool's real work, run INSIDE the span."""

    def _error(self, code: str, message: str, *, retryable: bool) -> ToolResult:
        return ToolResult(
            status="error",
            tool_name=self.tool_name,
            error_code=code,
            retryable=retryable,
            user_message=message,
            provenance=self._ERROR_PROVENANCE,
            result_preview=None,
            result_full=None,
        )


__all__ = [
    "RuntimeToolBase",
    "SpanOutcome",
    "in_tool_span",
]
