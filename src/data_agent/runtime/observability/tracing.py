"""tracing.py — OTel/Phoenix setup + span helpers (D23/D24/D61, design §7).

`configure_tracing(...)` builds an OTel `TracerProvider`. When
`otlp_endpoint` is set it wires an OTLP-over-HTTP exporter (self-hosted
Phoenix, D24); when unset (local dev / tests / no infra) **no span
processor is attached at all** — spans are still created (so all the manual
instrumentation call sites below work unconditionally) but are never
exported anywhere, so importing/using this module never requires a
reachable Phoenix collector or makes a network call (design §8: `uv run
pytest` stays green with zero infrastructure).

Span kinds mirror design §7's Phase-0 subset (`AGENT`/`LLM`/`TOOL`/`CHAIN`/
`GUARDRAIL`) via the OpenInference `openinference.span.kind` attribute
(OpenInference semantic conventions, D23) on plain OTel spans. `LLM` spans
are **not** manually created here — the OpenAI SDK is auto-instrumented via
`openinference-instrumentation-openai` (D24), which emits its own `LLM`
spans around every `responses.create`/`chat.completions.create` call.

Every attribute passed through the `*_span` helpers below must already be
redacted by the caller (`observability/redaction.py`) — this module does not
re-redact; it only forwards `{key: value}` pairs onto the OTel span. Callers
must never pass raw SQL/scope/JWT/result rows here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from openinference.instrumentation.openai import OpenAIInstrumentor
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Tracer

_TRACER_NAME = "data-agent-runtime"


def configure_tracing(*, otlp_endpoint: str, service_name: str) -> TracerProvider:
    """Build a `TracerProvider` exporting to *otlp_endpoint* (Phoenix), or a
    no-op provider (no span processor) when *otlp_endpoint* is empty.

    Does NOT call `trace.set_tracer_provider(...)` — that is a one-time
    process-global side effect the composition root (`app.py`) performs
    exactly once; keeping it out of this function makes `configure_tracing`
    safely callable multiple times in tests without triggering OTel's
    "Overriding of current TracerProvider is not allowed" warning.
    """
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    if otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def instrument_openai(provider: TracerProvider) -> None:
    """Auto-instrument the OpenAI SDK (D24) — idempotent, best-effort.

    Safe to call repeatedly (e.g. across test modules importing `app.py`
    more than once): a second call is a no-op rather than raising.
    """
    instrumentor = OpenAIInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        return
    instrumentor.instrument(tracer_provider=provider)


def get_tracer(provider: TracerProvider, name: str = _TRACER_NAME) -> Tracer:
    return provider.get_tracer(name)


@contextmanager
def span(
    tracer: Tracer,
    name: str,
    kind: OpenInferenceSpanKindValues,
    attributes: dict[str, Any] | None = None,
) -> Iterator[Span]:
    """Generic span helper — sets the OpenInference span-kind attribute plus
    whatever *attributes* the caller supplies (already redacted)."""
    with tracer.start_as_current_span(name) as current_span:
        current_span.set_attribute(SpanAttributes.OPENINFERENCE_SPAN_KIND, kind.value)
        for key, value in (attributes or {}).items():
            if value is not None:
                current_span.set_attribute(key, value)
        yield current_span


def agent_span(
    tracer: Tracer, *, scope_hash: str, turn_index: int
) -> Any:
    """One turn (design §7 span map: `AGENT`, `session.id`/`scope_hash`/turn index)."""
    return span(
        tracer,
        "agent.turn",
        OpenInferenceSpanKindValues.AGENT,
        {"scope_hash": scope_hash, "turn.index": turn_index},
    )


def chain_span(
    tracer: Tracer, name: str, *, attributes: dict[str, Any] | None = None
) -> Any:
    """Context assembly / budget-guard-adjacent chain stages (design §7 `CHAIN`)."""
    return span(tracer, name, OpenInferenceSpanKindValues.CHAIN, attributes)


def tool_span(
    tracer: Tracer, *, tool_name: str, args: dict[str, Any], status: str, error_code: str | None
) -> Any:
    """Each tool call (design §7 `TOOL`) — *args* must already be
    SQL-literal-masked (`observability/redaction.py::redact_tool_args`)."""
    attributes: dict[str, Any] = {"tool.name": tool_name, "tool.status": status}
    if error_code is not None:
        attributes["tool.error_code"] = error_code
    for key, value in args.items():
        if isinstance(value, str | int | float | bool):
            attributes[f"tool.args.{key}"] = value
    return span(tracer, f"tool.{tool_name}", OpenInferenceSpanKindValues.TOOL, attributes)


def guardrail_span(
    tracer: Tracer, name: str, *, attributes: dict[str, Any] | None = None
) -> Any:
    """Budget-cap checks / denial classification (design §7 `GUARDRAIL`)."""
    return span(tracer, name, OpenInferenceSpanKindValues.GUARDRAIL, attributes)


# B5: a strict attribute ALLOWLIST — never a bare type-filter — for the
# `AgentLoop`'s own (non-tool) `loop_*` stage-boundary observer events.
# `loop_paused_ask_user`'s payload carries a `question` key that can quote
# user-supplied/warehouse-derived PII (D25); a bare `isinstance(v, str | int |
# float | bool)` filter would let it straight through into a span attribute
# (and from there, Phoenix). Only these non-sensitive scalar counters/labels
# are ever forwarded.
_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST = ("window", "tool_calls_made")


def guardrail_observer(tracer: Tracer) -> Callable[[str, dict[str, Any]], None]:
    """Build a `(event, payload) -> None` observer emitting one GUARDRAIL span
    per `loop_*` `AgentLoop` stage-boundary event (design §7). The single
    call site `app.py`'s composition root uses — factored out here (rather
    than defined inline in `app.py`) so this exact redaction-allowlist
    behavior is directly unit-testable without spinning up the whole HTTP app
    (see `tests/runtime/observability/test_tool_span_wiring_e2e.py`).
    """

    def _observe(event: str, payload: dict[str, Any]) -> None:
        if not event.startswith("loop_"):
            return
        attributes = {
            key: payload[key]
            for key in _GUARDRAIL_OBSERVER_ATTR_ALLOWLIST
            if key in payload and isinstance(payload[key], str | int | float | bool)
        }
        with guardrail_span(tracer, event, attributes=attributes):
            pass

    return _observe


__all__ = [
    "agent_span",
    "chain_span",
    "configure_tracing",
    "get_tracer",
    "guardrail_observer",
    "guardrail_span",
    "instrument_openai",
    "span",
    "tool_span",
]

# Re-exported for callers that need to install the process-global provider
# (`trace.set_tracer_provider`) without importing `opentelemetry.trace` twice.
set_global_tracer_provider = trace.set_tracer_provider
