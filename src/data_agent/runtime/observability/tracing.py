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
must never pass raw SQL/scope/JWT/result rows here IN THE DEFAULT POSTURE.

The ONE exception is the access-controlled debug switch
`RuntimeSettings.otlp_disable_redaction` (see `config.py`): when set, the
composition root deliberately passes the caller the REAL (un-redacted) tool
args + the tool RESULT preview into `tool_span`, so Phoenix shows the real
tool call for debugging. That flip is TELEMETRY-ONLY (it never weakens actual
scope/PII enforcement) but it DOES make the Phoenix project entity-bearing, so
it must be access-controlled like the audit store.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from openinference.instrumentation import TraceConfig
from openinference.instrumentation.openai import OpenAIInstrumentor
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import Span, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_TRACER_NAME = "data-agent-runtime"


class LLMExceptionEventScrubber(SpanProcessor):
    """Strip content-bearing EVENTS off the auto-instrumented OpenAI `LLM` span (D25).

    `TraceConfig`/`instrument_openai(hide_content=True)` masks span ATTRIBUTES, but
    NOT span EVENTS — and the OpenAI instrumentor calls `record_exception(exc)` on a
    failed request, so an `openai.APIStatusError` (whose message/stacktrace embeds
    the response error BODY, potentially content-bearing) lands on the online LLM
    span as `exception.message` / `exception.stacktrace`, bypassing `hide_content`.
    The learning loop closes this same class by refusing `record_exception` on its
    OWN spans; here the instrumentor (not our code) owns the span, so we scrub the
    events post-hoc.

    `on_end` receives the ONE `ReadableSpan` snapshot `Span.end()` builds and hands
    to every processor in turn; rebinding its `_events` to an empty tuple drops the
    events from what the exporter later reads WITHOUT mutating the live span or its
    shape attributes. Wired FIRST (before any `SimpleSpanProcessor`, which exports
    synchronously in `on_end`) so the scrub always precedes export; the Batch path
    reads events lazily at export time, so ordering there is immaterial.

    Only OpenInference `LLM`-kind spans are touched (they carry no shape-relevant
    events — only the redacted content channels + a possible `exception`); the
    manual AGENT/TOOL/CHAIN/GUARDRAIL spans are left byte-identical (they are already
    literal-redacted and never carry content events)."""

    def on_start(
        self, span: Span, parent_context: Context | None = None
    ) -> None:  # pragma: no cover - no-op
        return

    def on_end(self, span: ReadableSpan) -> None:
        attributes = span.attributes or {}
        kind = attributes.get(SpanAttributes.OPENINFERENCE_SPAN_KIND)
        if kind != OpenInferenceSpanKindValues.LLM.value:
            return
        if span.events:
            # Rebind the exported snapshot's events → empty (drops `exception` and
            # any other content-bearing event; LLM spans carry no shape events).
            span._events = ()

    def shutdown(self) -> None:  # pragma: no cover - no-op
        return

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # pragma: no cover - no-op
        return True


def configure_tracing(
    *,
    otlp_endpoint: str,
    service_name: str,
    project_name: str | None = None,
    hide_llm_content: bool = False,
    span_exporter: SpanExporter | None = None,
) -> TracerProvider:
    """Build a `TracerProvider` exporting to *otlp_endpoint* (Phoenix), or a
    no-op provider (no span processor) when *otlp_endpoint* is empty.

    Does NOT call `trace.set_tracer_provider(...)` — that is a one-time
    process-global side effect the composition root (`app.py`) performs
    exactly once; keeping it out of this function makes `configure_tracing`
    safely callable multiple times in tests without triggering OTel's
    "Overriding of current TracerProvider is not allowed" warning.

    *project_name* is the Phoenix PROJECT the spans land in: Phoenix groups
    traces by the `openinference.project.name` resource attribute, NOT by
    `service.name`. Setting only `service.name` (as this function used to)
    dumps everything into Phoenix's catch-all `default` project, invisible as
    a named project in the UI. We set `openinference.project.name` here, IN
    CODE, so a caller (`app.py`, `configure_learning_tracing`) picks a stable
    named project without an `OTEL_RESOURCE_ATTRIBUTES` env hack. Defaults to
    *service_name* when omitted, so the project name is never empty.

    *hide_llm_content* (D25): when True, install `LLMExceptionEventScrubber` as
    the FIRST span processor so a recorded `exception` event (which the OpenAI
    instrumentor embeds the response error body into) is stripped off the auto-
    instrumented `LLM` span before export — closing the residual content channel
    `TraceConfig` (attributes-only) leaves open. Pair with
    `instrument_openai(hide_content=True)`; both are gated on the SAME
    `otlp_hide_llm_content` setting by `app.py`.

    *span_exporter* (test-only seam, D-L3-5): when supplied, its spans are
    attached via a `SimpleSpanProcessor` (synchronous flush — a batched
    processor would leave spans un-exported when a test reads them right after
    a turn). This is the injection point the Layer-3 demo launcher uses to
    install an `InMemorySpanExporter` and assert the D25 PII invariant over the
    real emitted spans, with NO Phoenix container. Production leaves it `None`,
    so this branch is inert and the provider is byte-identical to before.
    """
    resource = Resource.create(
        {
            "service.name": service_name,
            "openinference.project.name": project_name or service_name,
        }
    )
    provider = TracerProvider(resource=resource)
    # FIRST (see LLMExceptionEventScrubber docstring): must precede any synchronous
    # SimpleSpanProcessor export so the LLM `exception` event is scrubbed pre-export.
    if hide_llm_content:
        provider.add_span_processor(LLMExceptionEventScrubber())
    if otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
    if span_exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    return provider


def llm_content_trace_config(hide_content: bool) -> TraceConfig | None:
    """The OpenInference `TraceConfig` governing the auto-instrumented OpenAI
    `LLM` span's CONTENT capture (D25).

    Returns `None` when *hide_content* is False — the library default, where the
    `LLM` span carries the raw prompt + completion (`input.value`/`output.value`/
    `llm.input_messages`/`llm.output_messages`). Otherwise returns a config that
    SUPPRESSES every content channel (inputs, outputs, per-message content, and
    prompts) while leaving the non-content shape/timing attributes intact
    (`llm.model_name`, `llm.token_count.*`, `llm.provider`, span timing).

    D25 (the ONLINE per-turn HARD invariant): unlike the manual AGENT/TOOL/CHAIN
    spans — which already redact SQL literals / bound-slot values before setting
    an attribute — the OpenAI auto-instrumentor (D24) captures the model's raw
    prompt AND completion by DEFAULT, and the completion embeds cell values /
    the query-derived answer. That is a content leak into the online Phoenix
    project, which is a shape/count/latency-only surface. So the runtime hides
    LLM content BY DEFAULT (`RuntimeSettings.otlp_hide_llm_content=True`) and the
    reveal is an explicit, controlled opt-in — mirroring the learning loop's
    verbose gate."""
    if not hide_content:
        return None
    return TraceConfig(
        hide_inputs=True,
        hide_outputs=True,
        hide_input_messages=True,
        hide_output_messages=True,
        hide_prompts=True,
    )


def instrument_openai(provider: TracerProvider, *, hide_content: bool = True) -> None:
    """Auto-instrument the OpenAI SDK (D24) — idempotent, best-effort.

    Safe to call repeatedly (e.g. across test modules importing `app.py`
    more than once): a second call is a no-op rather than raising.

    FIRST-CALLER-WINS: `OpenAIInstrumentor` is a process-global singleton behind
    the idempotency guard below, so the FIRST call's *hide_content* config wins
    for the whole process — a later call with a DIFFERENT *hide_content* value
    silently no-ops (its config is ignored). Production's single `create_app` is
    unaffected; this note guards against a future in-process surprise (e.g. two
    `create_app`s with divergent settings, or a test that instruments before the
    app). The paired `LLMExceptionEventScrubber` (an OWN span processor, not the
    singleton) is NOT subject to this — each provider gets its own.

    *hide_content* (D25, default True): when True the emitted `LLM` span carries
    NO raw prompt/completion — only shape/timing/model-name/token-counts — so the
    online per-turn Phoenix project stays content-free (structure fully visible,
    content hidden). Set False ONLY in a controlled, access-controlled diagnostic
    environment: revealing the LLM content makes the runtime Phoenix project
    ENTITY-BEARING (the raw question AND the query-derived answer land on the
    span), subject to the same in-boundary PII posture + access control as the
    session/audit stores — NOT the shape-only D25 telemetry posture. This is the
    exact same trade-off as the learning-loop `LEARNING_TRACE_VERBOSE` gate.
    """
    instrumentor = OpenAIInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        return
    instrumentor.instrument(
        tracer_provider=provider, config=llm_content_trace_config(hide_content)
    )


def get_tracer(provider: TracerProvider, name: str = _TRACER_NAME) -> Tracer:
    return provider.get_tracer(name)


@contextmanager
def span(
    tracer: Tracer,
    name: str,
    kind: OpenInferenceSpanKindValues,
    attributes: dict[str, Any] | None = None,
    *,
    context: Context | None = None,
    record_exception: bool = True,
) -> Iterator[Span]:
    """Generic span helper — sets the OpenInference span-kind attribute plus
    whatever *attributes* the caller supplies (already redacted).

    *context* is the OTel parent `Context` to start the span under (default
    `None` ⇒ the ambient current context, so spans auto-nest as before). Passing
    an EXPLICIT context — e.g. one rehydrated from a W3C `traceparent` extracted
    off a cross-process message (`context_from_traceparent`) — makes this span a
    CHILD of that remote parent, so a session's spans join ONE trace across the
    sweeper → consumer → scheduler process boundaries.

    *record_exception* forwards to `start_as_current_span`. It defaults to `True`
    (the online-runtime behavior: a raised exception attaches an `exception` event —
    `exception.message` + `exception.stacktrace` — to the span). Callers that WRAP
    real work AND must stay content-free even on error (the learning-loop spans, D25)
    MUST pass `record_exception=False`: an OpenAI SDK error embeds response bodies and
    a landing error references the entity-bearing `forbidden_spans`, so recording it
    would leak transcript/entity content onto the span EVEN WITH VERBOSE OFF. The span
    status is still set on exception (`set_status_on_exception=True`), so the ERROR is
    visible in traces — only the entity-bearing detail is withheld."""
    with tracer.start_as_current_span(
        name,
        context=context,
        record_exception=record_exception,
        set_status_on_exception=True,
    ) as current_span:
        current_span.set_attribute(SpanAttributes.OPENINFERENCE_SPAN_KIND, kind.value)
        for key, value in (attributes or {}).items():
            if value is not None:
                current_span.set_attribute(key, value)
        yield current_span


# --- W3C trace-context propagation (cross-process span chaining) --------------
# The learning loop spans across THREE decoupled hops (sweeper → Redis stream →
# consumer, and consumer → candidate store → cron scheduler). To make a session's
# whole learning journey read as ONE Phoenix trace, the producer INJECTS the
# current span's `traceparent` onto the carried message/envelope and the consumer
# EXTRACTS it back into a parent `Context`. These two helpers wrap the standard
# W3C `TraceContextTextMapPropagator` so callers never touch a carrier dict.
_PROPAGATOR = TraceContextTextMapPropagator()


def inject_current_traceparent() -> str | None:
    """Serialize the CURRENT span's context to a W3C `traceparent` string (or
    `None` when there is no recording span in context). Call it INSIDE the span
    that should become the cross-process parent; the returned value rides on the
    outgoing message/envelope and is rehydrated by `context_from_traceparent`."""
    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier.get("traceparent")


def context_from_traceparent(traceparent: str | None) -> Context | None:
    """Rehydrate a parent `Context` from a `traceparent` carried on an incoming
    message/envelope, for `span(..., context=...)`. FAIL-OPEN: a missing or
    malformed value ⇒ `None` (the span starts as a normal root), never a crash."""
    if not traceparent:
        return None
    try:
        return _PROPAGATOR.extract({"traceparent": traceparent})
    except Exception:  # noqa: BLE001 - a malformed traceparent must never crash a consume/promote
        return None


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
    tracer: Tracer,
    *,
    tool_name: str,
    args: dict[str, Any],
    status: str,
    error_code: str | None,
    result_preview: Any = None,
    reveal_complex_args: bool = False,
) -> Any:
    """Each tool call (design §7 `TOOL`).

    DEFAULT (D25) posture: *args* must already be SQL-literal-masked by the caller
    (`observability/redaction.py::redact_tool_args`), *result_preview* is `None`,
    and *reveal_complex_args* is `False` — tool RESULTS never reach a span and only
    SCALAR args are set (nested dict/list args like `period`/`slot_bindings` are
    skipped entirely), so the online Phoenix project stays a shape/count/latency-
    only surface.

    DEBUG posture (`RuntimeSettings.otlp_disable_redaction=True`): the caller passes
    the REAL (un-redacted) *args*, a *result_preview*, AND *reveal_complex_args=True*
    so the WHOLE tool call — the real SQL/values, the nested dict/list args (real
    `period` bounds, `slot_bindings` values), AND the columns + preview rows it
    returned — is visible in Phoenix. This makes the project ENTITY-BEARING and MUST
    be access-controlled like the audit store (see
    `RuntimeSettings.otlp_disable_redaction`). This module does not itself redact; it
    forwards whatever the caller supplies.

    *result_preview* (when supplied — debug only) is a `ResultPreview`-shaped object
    (`.columns`/`.row_count`/`.truncated`/`.preview_rows`); its columns/shape land as
    scalar span attributes and the preview rows are JSON-serialized onto one
    attribute (`tool.result.preview_rows`), since a span attribute cannot hold a
    ragged list-of-lists.

    *reveal_complex_args* (debug only) JSON-serializes each non-scalar arg value
    (dict/list) onto `tool.args.{key}` — a span attribute cannot hold a nested dict,
    so a scalar-only pass would drop `period`/`slot_bindings` even when the flag is
    on. Default `False` keeps the default span byte-identical (those keys absent).
    """
    attributes: dict[str, Any] = {"tool.name": tool_name, "tool.status": status}
    if error_code is not None:
        attributes["tool.error_code"] = error_code
    for key, value in args.items():
        if isinstance(value, str | int | float | bool):
            attributes[f"tool.args.{key}"] = value
        elif reveal_complex_args and value is not None:
            # DEBUG-ONLY (otlp_disable_redaction): a non-scalar arg (dict/list —
            # e.g. resolveValues.period, runBlueprint.slot_bindings) JSON-serialized
            # so its REAL values show. NEVER reached in the default posture (the flag
            # is off), so the default span is byte-identical (these keys absent).
            attributes[f"tool.args.{key}"] = json.dumps(value, default=str)
    if result_preview is not None:
        # DEBUG-ONLY (otlp_disable_redaction): attach the tool RESULT so the full
        # call is visible. NEVER reached in the default D25 posture (the dispatcher
        # passes None), so the shape-only surface is byte-identical when off.
        attributes["tool.result.columns"] = list(result_preview.columns)
        attributes["tool.result.row_count"] = result_preview.row_count
        attributes["tool.result.truncated"] = result_preview.truncated
        attributes["tool.result.preview_rows"] = json.dumps(
            result_preview.preview_rows, default=str
        )
    return span(tracer, f"tool.{tool_name}", OpenInferenceSpanKindValues.TOOL, attributes)


def guardrail_span(
    tracer: Tracer, name: str, *, attributes: dict[str, Any] | None = None
) -> Any:
    """Budget-cap checks / denial classification (design §7 `GUARDRAIL`)."""
    return span(tracer, name, OpenInferenceSpanKindValues.GUARDRAIL, attributes)


def embedding_span(tracer: Tracer, *, model: str, input_count: int) -> Any:
    """The custom embedding-API call (`resolveValues` ranking, D24/D71).

    The auto-instrumentor covers only the agent LLM, not this custom endpoint,
    so `HttpEmbeddingClient` wraps its POST here manually. Vector counts +
    model id + latency only — the embedded TEXT is never logged (D25).
    """
    return span(
        tracer,
        "embedding",
        OpenInferenceSpanKindValues.EMBEDDING,
        {"embedding.model": model, "embedding.input_count": input_count},
    )


def rerank_span(
    tracer: Tracer,
    *,
    model: str,
    document_count: int,
    reranked: bool | None = None,
) -> Any:
    """The custom reranker-API call / the retrieval-pipeline rerank stage
    (retrieval pipeline, D24/D71, design §3.5).

    Mirrors `embedding_span`: the auto-instrumentor covers only the agent LLM,
    not this custom endpoint. Two call sites share this helper:
      - `HttpRerankerClient` wraps its POST here manually (transport level, no
        `reranked` flag — it has no knowledge of pipeline-level degrade).
      - `retrieval/pipeline.py` wraps the rerank STAGE here (design §3.5),
        passing `reranked` — `False` on the degrade path (no reranker
        configured / rerank error → recall order used), so the degrade is
        visible in traces even when the reranker client was never called.
    Document counts + model id + the `reranked` flag only — the reranker QUERY
    and the DOCUMENT text are never logged (D25).
    """
    return span(
        tracer,
        "rerank",
        OpenInferenceSpanKindValues.RERANKER,
        {
            "reranker.model": model,
            "reranker.document_count": document_count,
            "reranker.reranked": reranked,
        },
    )


def recall_span(
    tracer: Tracer,
    *,
    corpus: str,
    recall_k: int,
    candidate_count: int,
    dropped_by_scope_count: int,
) -> Any:
    """The retrieval-pipeline vector-recall stage per corpus (design §3.5).

    `CHAIN` kind. Structural counters only — corpus name (`blueprint`/
    `knowledge`, non-PII), the recall fan-out `k`, how many candidates the
    index returned, and how many blueprint candidates the scope pre-filter
    dropped (D60/D44 read-path analogue). The QUESTION TEXT is never an
    attribute (D25), nor are candidate ids/intent/chunk text (design §3.5:
    "no query text"; intent/chunk text is not worth the redaction surface).
    """
    return span(
        tracer,
        "retrieval.recall",
        OpenInferenceSpanKindValues.CHAIN,
        {
            "retrieval.corpus": corpus,
            "retrieval.recall_k": recall_k,
            "retrieval.candidate_count": candidate_count,
            "retrieval.dropped_by_scope_count": dropped_by_scope_count,
        },
    )


# B5: a strict attribute ALLOWLIST — never a bare type-filter — for the
# `AgentLoop`'s own (non-tool) `loop_*` stage-boundary observer events.
# `loop_paused_ask_user`'s payload carries a `question` key that can quote
# user-supplied/warehouse-derived PII (D25); a bare `isinstance(v, str | int |
# float | bool)` filter would let it straight through into a span attribute
# (and from there, Phoenix). Only these non-sensitive scalar counters/labels
# are ever forwarded.
#
# The `tool_name`/`tool_call_id`/`deduped`/`guard_reason`/`dedup_target`/`note`/
# `database`/`table` labels make the repeated-idempotent-read guard's
# `loop_repeated_idempotent_read_guarded` span self-describing (so a reader can
# tell a deduped SECOND read from a real first dispatch) and enrich the D94
# `loop_result_withheld_provenance` span; every one is a non-sensitive identifier
# or fixed label (`database`/`table` are catalog metadata — the same scalar
# identifiers a real `tool.<name>` span already exposes; free-form args like
# `sql` are NEVER placed on these events by the emitter, so no literal can leak).
_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST = (
    "window",
    "tool_calls_made",
    "tool_name",
    "tool_call_id",
    "deduped",
    "guard_reason",
    "dedup_target",
    "note",
    "database",
    "table",
)


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
    "LLMExceptionEventScrubber",
    "agent_span",
    "chain_span",
    "configure_tracing",
    "context_from_traceparent",
    "embedding_span",
    "get_tracer",
    "guardrail_observer",
    "guardrail_span",
    "inject_current_traceparent",
    "instrument_openai",
    "llm_content_trace_config",
    "recall_span",
    "rerank_span",
    "span",
    "tool_span",
]

# Re-exported for callers that need to install the process-global provider
# (`trace.set_tracer_provider`) without importing `opentelemetry.trace` twice.
set_global_tracer_provider = trace.set_tracer_provider
