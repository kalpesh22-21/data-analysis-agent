"""Learning-loop tracing (D23/D24/D25, design §10).

Both processes trace to the Phoenix `learning-loop` project via their own
`TracerProvider` (`service.name = "learning-loop"`). Reuses
`runtime/observability/tracing.py`'s `configure_tracing` (same no-op-provider
behavior when no OTLP endpoint is set — zero infra required to run) and its
`span` primitive, adding the four learning-specific span helpers.

D25 invariants enforced by construction here: the ONLY attributes ever set are
non-PII counters/labels + `session.id` (the D25 trace-grouping key) +
`content_hash`/`message_id` (non-PII audit keys). The raw JWT, the raw
`column_scope`, and any transcript/message/tool-result content are NEVER passed
to these helpers.
"""

from __future__ import annotations

from typing import Any

from openinference.semconv.trace import OpenInferenceSpanKindValues
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Tracer

from data_agent.runtime.observability.tracing import configure_tracing, get_tracer, span

_TRACER_NAME = "learning-loop"


def configure_learning_tracing(
    *, otlp_endpoint: str, service_name: str = "learning-loop"
) -> TracerProvider:
    """Build the learning processes' `TracerProvider` (Phoenix `learning-loop`
    project). Delegates to the runtime `configure_tracing` so the exporter /
    no-op-provider behavior is identical; does NOT install the provider globally
    (the entrypoint does that once)."""
    return configure_tracing(otlp_endpoint=otlp_endpoint, service_name=service_name)


def get_learning_tracer(provider: TracerProvider) -> Tracer:
    return get_tracer(provider, _TRACER_NAME)


def sweep_span(
    tracer: Tracer,
    *,
    scanned: int,
    claimed: int,
    enqueued: int,
    disabled: bool = False,
) -> Any:
    """One sweep cycle (design §10 `learning.sweep`, CHAIN)."""
    return span(
        tracer,
        "learning.sweep",
        OpenInferenceSpanKindValues.CHAIN,
        {
            "learning.scanned": scanned,
            "learning.claimed": claimed,
            "learning.enqueued": enqueued,
            "learning.disabled": disabled,
        },
    )


def enqueue_span(
    tracer: Tracer, *, session_id: str, content_hash: str, message_id: str
) -> Any:
    """One enqueue (design §10 `learning.enqueue`, CHAIN). `session.id` is the
    D25 trace-grouping key; `content_hash`/`message_id` are non-PII."""
    return span(
        tracer,
        "learning.enqueue",
        OpenInferenceSpanKindValues.CHAIN,
        {
            "session.id": session_id,
            "learning.content_hash": content_hash,
            "learning.message_id": message_id,
        },
    )


def consume_span(
    tracer: Tracer, *, session_id: str, outcome: str, delivery_count: int
) -> Any:
    """One consume (design §10 `learning.consume`, CHAIN). *outcome* ∈
    {`done`, `dedup_skip`, `dead_letter`}."""
    return span(
        tracer,
        "learning.consume",
        OpenInferenceSpanKindValues.CHAIN,
        {
            "session.id": session_id,
            "learning.outcome": outcome,
            "learning.delivery_count": delivery_count,
        },
    )


def disabled_span(tracer: Tracer, *, process: str) -> Any:
    """Kill-switch trip (design §10 `learning.disabled`, GUARDRAIL). *process* ∈
    {`sweeper`, `consumer`}."""
    return span(
        tracer,
        "learning.disabled",
        OpenInferenceSpanKindValues.GUARDRAIL,
        {"learning.process": process},
    )


__all__ = [
    "configure_learning_tracing",
    "consume_span",
    "disabled_span",
    "enqueue_span",
    "get_learning_tracer",
    "sweep_span",
]
