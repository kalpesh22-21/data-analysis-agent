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

The ONE exception is `RuntimeSettings.otlp_disable_redaction` (see `config.py`),
and since 2026-08-10 it is the DEFAULT rather than an exception an operator opts
into: the composition root passes the caller the REAL (un-redacted) tool args +
the tool RESULT preview into `tool_span`, so Phoenix shows the real tool call —
real SQL literals, real resolved values. That flip is TELEMETRY-ONLY (it never
weakens actual scope/PII enforcement) but it DOES make the Phoenix project
entity-bearing, so the collector must be access-controlled like the session
store. "IN THE DEFAULT POSTURE" above therefore now describes the OPT-OUT
(`OTLP_DISABLE_REDACTION=false`), not the shipped default.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Iterator, Sequence
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
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.trace import Span, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_TRACER_NAME = "data-agent-runtime"

# The DEFAULT set of span NAMES dropped before export so a reviewer sees only the
# meaningful spans of a runtime turn (design §7 noise-reduction). These are the
# pure-plumbing spans emitted on every normal turn — NOT their meaningful
# neighbours: `agent.turn`, the auto-instrumented OpenAI `Response`/`LLM` span,
# every `tool.<name>` dispatch span, `embedding`, `rerank`, `retrieval.recall`,
# and `loop_repeated_idempotent_read_guarded` all stay.
#
#   - `context.assembly`      — the D50 context-assembly CHAIN wrapper (plumbing).
#   - `loop_model_call_start` — the AgentLoop "about to call the model" boundary.
#   - `loop_turn_done`        — the AgentLoop "turn finished" boundary.
#
# This is only a DEFAULT: `RuntimeSettings.otlp_drop_span_names` overrides it
# wholesale (add the low-frequency status spans `loop_paused_ask_user` /
# `loop_paused_budget_cap` / `loop_hard_ceiling_stop` /
# `loop_result_withheld_provenance` if an operator also wants those gone, or set
# it EMPTY to disable filtering and export every span as before).
#
# RE-PARENTING NOTE (see `_NameFilteringSpanExporter`): `context.assembly` is a
# MID-TREE span — the `embedding` / `rerank` / `retrieval.recall` KEEP spans run
# INSIDE it (context/assembly.py runs retrieval within the `context.assembly`
# span). Dropping it name-wise at the exporter does NOT drop those children; they
# keep their `parent_span_id` and Phoenix re-roots such orphans directly under the
# trace (`agent.turn`), so they render one level flatter but stay fully visible —
# the intended, cleaner tree. The `loop_*` spans are leaves (no children), so
# dropping them is inert for parenting.
DEFAULT_DROP_SPAN_NAMES: frozenset[str] = frozenset(
    {"context.assembly", "loop_model_call_start", "loop_turn_done"}
)


class _NameFilteringSpanExporter(SpanExporter):
    """A `SpanExporter` decorator that DROPS spans by NAME before forwarding to a
    real exporter — the one central, name-based place trace noise is reduced
    (design §7). Wraps whatever exporter `configure_tracing` would otherwise use
    (the OTLP/Phoenix exporter AND any injected in-memory test exporter), so a
    single denylist governs BOTH paths identically.

    Filtering here (at the exporter, downstream of the span processor) rather than
    at each `span()` call site keeps the instrumentation call sites untouched and
    makes the policy a single tunable set (`RuntimeSettings.otlp_drop_span_names`).
    A dropped span's CHILDREN are still exported with their original
    `parent_span_id`; a trace UI (Phoenix) renders such orphans under the trace
    root, so dropping a mid-tree plumbing span flattens — never severs — the tree
    (see `DEFAULT_DROP_SPAN_NAMES`' re-parenting note)."""

    def __init__(self, inner: SpanExporter, drop_names: frozenset[str]) -> None:
        self._inner = inner
        self._drop_names = drop_names

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        kept = [span for span in spans if span.name not in self._drop_names]
        if not kept:
            # Nothing survived the filter — report success without a downstream
            # call (an empty export is a no-op for every SDK exporter anyway).
            return SpanExportResult.SUCCESS
        return self._inner.export(kept)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._inner.force_flush(timeout_millis)


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
    drop_span_names: Collection[str] = (),
    id_generator: IdGenerator | None = None,
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

    *hide_llm_content* (this PARAM): when True, install `LLMExceptionEventScrubber`
    as the FIRST span processor so a recorded `exception` event (which the OpenAI
    instrumentor embeds the response error body into) is stripped off the auto-
    instrumented `LLM` span before export — closing the residual content channel
    `TraceConfig` (attributes-only) leaves open. Pair with
    `instrument_openai(hide_content=True)`; both are gated on the SAME
    `otlp_hide_llm_content` setting by `app.py`. NOTE (D25 amended 2026-07-15): the
    SYSTEM default is now REVEAL — `otlp_hide_llm_content` defaults False, so `app.py`
    passes `hide_llm_content=False` by default and this scrubber is NOT installed by
    default (a failed OpenAI call's error body can then land on the span — the
    accepted consequence of the reveal posture). This param default stays False.

    *span_exporter* (test-only seam, D-L3-5): when supplied, its spans are
    attached via a `SimpleSpanProcessor` (synchronous flush — a batched
    processor would leave spans un-exported when a test reads them right after
    a turn). This is the injection point the Layer-3 demo launcher uses to
    install an `InMemorySpanExporter` and assert the D25 PII invariant over the
    real emitted spans, with NO Phoenix container. Production leaves it `None`,
    so this branch is inert and the provider is byte-identical to before.

    *drop_span_names* (design §7 noise reduction): span NAMES to DROP before
    export, applied centrally by wrapping EVERY real exporter (the OTLP one AND an
    injected *span_exporter*) in `_NameFilteringSpanExporter`. Empty (the default)
    ⇒ NO wrapper is installed and the provider is byte-identical to before (so
    every existing span-assertion test and any other consumer sees all spans);
    `app.py` passes `RuntimeSettings.otlp_drop_span_names` (defaulting to
    `DEFAULT_DROP_SPAN_NAMES`) so a normal deployment drops the plumbing spans.
    Filtering is name-based and downstream of the span processor, so it never
    touches an instrumentation call site and never orphans a kept child badly
    (see `_NameFilteringSpanExporter` / `DEFAULT_DROP_SPAN_NAMES`).

    *id_generator* (optional): a custom OTel `IdGenerator` for the provider — used
    by the learning session-trace projection to mint DETERMINISTIC trace/span ids
    (seeded by session id) so a re-export upserts the same Phoenix spans instead of
    duplicating them. `None` (the default) leaves the provider byte-identical to
    before (the SDK's random id generator).
    """
    drop_names = frozenset(drop_span_names)

    def _filtered(exporter: SpanExporter) -> SpanExporter:
        # Wrap ONLY when there is something to drop, so the empty-denylist path
        # stays byte-identical (same processor count, same exporter object).
        return _NameFilteringSpanExporter(exporter, drop_names) if drop_names else exporter

    resource = Resource.create(
        {
            "service.name": service_name,
            "openinference.project.name": project_name or service_name,
        }
    )
    # Branch on *id_generator* so the default path stays byte-identical (passing
    # id_generator=None explicitly would override the SDK's own default generator).
    if id_generator is not None:
        provider = TracerProvider(resource=resource, id_generator=id_generator)
    else:
        provider = TracerProvider(resource=resource)
    # FIRST (see LLMExceptionEventScrubber docstring): must precede any synchronous
    # SimpleSpanProcessor export so the LLM `exception` event is scrubbed pre-export.
    if hide_llm_content:
        provider.add_span_processor(LLMExceptionEventScrubber())
    if otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(_filtered(exporter)))
    if span_exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(_filtered(span_exporter)))
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

    D25, amended 2026-07-15: unlike the manual AGENT/TOOL/CHAIN spans — which
    already redact SQL literals / bound-slot values before setting an attribute —
    the OpenAI auto-instrumentor (D24) captures the model's raw prompt AND
    completion by DEFAULT, and the completion embeds cell values / the query-derived
    answer. By deliberate operator choice the runtime now REVEALS LLM content BY
    DEFAULT (`RuntimeSettings.otlp_hide_llm_content=False`), so the online Phoenix
    project is ENTITY-BEARING BY DEFAULT and MUST be access-controlled like the
    audit/session store — mirroring the learning loop's verbose gate. Hiding is the
    explicit opt-OUT (`otlp_hide_llm_content=True`), which restores the D25 shape/
    count/latency-only surface. This function's redaction MECHANISM is unchanged."""
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

    *hide_content* (this PARAM defaults True): when True the emitted `LLM` span
    carries NO raw prompt/completion — only shape/timing/model-name/token-counts — so
    the online per-turn Phoenix project stays content-free. NOTE (D25 amended
    2026-07-15): the SYSTEM default is now REVEAL — `app.py` resolves
    `otlp_hide_llm_content` (now defaulting False) via `effective_llm_hide` and passes
    the result here, so by default `hide_content=False` and the runtime Phoenix
    project is ENTITY-BEARING (the raw question AND the query-derived answer land on
    the span), subject to the same in-boundary PII posture + access control as the
    session/audit stores. Hiding is now the explicit opt-OUT (`otlp_hide_llm_content=
    True`) restoring the shape-only D25 posture. Same trade-off as the learning-loop
    `LEARNING_TRACE_VERBOSE` gate. This PARAM default stays True (call sites drive it).
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
#
# EMITTING AN EVENT DOES NOT PUBLISH ITS PAYLOAD (Release 1, 06). Every key an
# emitter sends that is not listed here is silently dropped — a correctly-named
# GUARDRAIL span carrying nothing. The `analysisState` keys below are therefore
# each a DELIBERATE D25 DECLARATION that the key is shape-only:
#
#   `intent_count` / `pending_count` / `proposed_count` — counts.
#   `turn_index` / `window`                            — runtime-assigned numbers.
#   `intent_id`                                        — runtime-assigned (`i1`),
#                                                        carries no user content.
#   `from_status` / `to_status` / `reason_code` / `exit` / `reason` — closed enums.
#   `evidence_tool_name` / `blocking_tool_name`        — tool names.
#
# `description` MUST NEVER BE ADDED. It is model-authored text derived from the
# user's question — the one field on `TrackedIntent` that carries user content —
# and it is never placed on any of these payloads in the first place, so this list
# is the second of two independent guards, not the only one.
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
    # --- analysisState / finalization enforcement (Release 1, 06) ---
    "intent_count",
    "turn_index",
    "intent_id",
    "from_status",
    "to_status",
    "reason_code",
    "evidence_tool_name",
    # HOW the evidence binding was established — `tagged` (the model named the
    # intent on the call, call-time tagging) or `auto_bound` (the model named
    # nothing and the runtime bound the one call that could have served it,
    # 03 §C.3.2). A closed two-member enum, so route derivation can read its own
    # provenance, and `auto_bound` is the weaker of the two by construction.
    # `declared` — the model citing a `tool_call_id` itself — was RETIRED with the
    # field on 2026-08-12 and can no longer be produced. The TAG VALUE itself is
    # never an attribute: a valid one is a runtime-assigned `intent_id` (already
    # allowlisted above, and emitted as `intent_id`), while a dropped one is
    # arbitrary model text and is reported by RULE NAME on
    # `loop_intent_tag_dropped.reason` instead (D25).
    "evidence_binding",
    "exit",
    "pending_count",
    "proposed_count",
    "blocking_tool_name",
    "reason",
    # The blueprint the getBlueprint-before-run gate refused
    # (`loop_blueprint_definition_not_read`). D25-safe: a blueprint id is
    # CORPUS-AUTHORED, never user content — the same class as the table/database
    # identifiers already allowlisted above. Neither the blueprint's SQL nor the
    # user's question is ever placed on the span.
    "blueprint_id",
    # --- trim-aware re-fetch exemption (loop/agent_loop.py) ---
    # How many re-fetches this signature has already been granted in the window, and
    # the cap. Both are small integers about the LOOP'S OWN decisions — no read
    # arguments, no result content. `loop_trimmed_read_refetch_capped` is the event
    # worth alerting on (a turn re-reading what the budget keeps dropping), and it
    # says nothing without these two.
    "refetch_count",
    "refetch_cap",
    # --- the refused-round cap (05 §F) ---
    # `loop_intent_force_blocked.budget_cap_reached` — a bare `True`, present ONLY
    # on the cap-during-a-refused-round path and omitted everywhere else. The
    # disposition on that path is `ENFORCEMENT_EXHAUSTED` because enforcement, not
    # capacity, is what ran out; this flag keeps the capacity fact visible to an
    # operator watching budget pressure without putting it on the intent record.
    # D25-safe by shape: a boolean cannot carry user content.
    "budget_cap_reached",
    # --- multi-table answerWithTable (08 §L) ---
    # `loop_answer_tables_designated` — how many tables the model designated, how
    # many of them are a blueprint's result, and how many carry a D56 badge. Three
    # small counts about the LOOP'S OWN bookkeeping. The captions, the SQL and the
    # cell values never appear on any of these payloads in the first place, so this
    # list is the second of two independent guards. `table_count` is shared with
    # `history_answer_table_scope_dropped`.
    "table_count",
    "blueprint_table_count",
    "verified_table_count",
    # --- the answer-shape gate (05 §J) ---
    # `loop_answer_shape_refused.multi_row_calls` — how many successful multi-row
    # `runQuery`/`runBlueprint` calls the turn was holding when it tried to finish in
    # bare prose. A count of the LOOP'S OWN bookkeeping: not the row counts
    # themselves, not the SQL, not a cell. Without it the event cannot distinguish
    # "one untabled result" from "five", which is the difference between a model
    # that forgot a table and one that abandoned the format entirely.
    # (`loop_answer_shape_exhausted` carries no payload at all.)
    "multi_row_calls",
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
    "DEFAULT_DROP_SPAN_NAMES",
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
