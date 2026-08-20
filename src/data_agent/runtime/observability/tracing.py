"""OTel/Phoenix setup + span helpers (D23/D24/D61).

`configure_tracing` builds a `TracerProvider`; with no `otlp_endpoint` NO span processor
is attached, so spans are still created but never exported and nothing here needs a
reachable collector. Span kinds ride OpenInference's `openinference.span.kind`; `LLM`
spans are NOT created here — the OpenAI SDK is auto-instrumented instead.

This module never re-redacts — it forwards whatever attributes the caller sets. Under
`otlp_disable_redaction` (the shipped DEFAULT) the caller passes REAL args and result
previews, which makes the Phoenix project entity-bearing and means it must be
access-controlled like the session store.
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
    """A `SpanExporter` decorator that DROPS spans by NAME before forwarding.

        Wraps every real exporter (the OTLP one AND any injected test exporter), so a single
        denylist (`RuntimeSettings.otlp_drop_span_names`) governs both paths and no
        instrumentation call site changes. A dropped span's CHILDREN are still exported with
        their original `parent_span_id`, which a trace UI renders under the trace root —
        dropping a mid-tree span flattens the tree, never severs it.
    """

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

        `instrument_openai(hide_content=True)` masks span ATTRIBUTES but not span EVENTS,
        and the instrumentor calls `record_exception`, so a response error body can land as
        `exception.message`/`exception.stacktrace`. `on_end` rebinds the `ReadableSpan`'s
        `_events` to an empty tuple, dropping them from what the exporter later reads without
        mutating the live span. Must be wired FIRST, ahead of any `SimpleSpanProcessor`
        (which exports synchronously in `on_end`). Only OpenInference `LLM`-kind spans are
        touched; the manual AGENT/TOOL/CHAIN/GUARDRAIL spans stay byte-identical.
    """

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
    """Build a `TracerProvider` exporting to *otlp_endpoint* (Phoenix), or a no-op
        provider (no span processor) when *otlp_endpoint* is empty.

        Does NOT call `trace.set_tracer_provider(...)`: that process-global side effect
        belongs to the composition root, and keeping it out makes this function safely
        callable many times in tests.

        Args:
            project_name: the Phoenix PROJECT, set as `openinference.project.name` —
                Phoenix groups traces by that resource attribute, NOT by `service.name`.
                Defaults to *service_name*, so it is never empty.
            hide_llm_content: install `LLMExceptionEventScrubber` as the FIRST span
                processor. Pair with `instrument_openai(hide_content=True)`; both are gated
                on `otlp_hide_llm_content`, whose system default is REVEAL.
            span_exporter: test-only seam, attached via a `SimpleSpanProcessor` so spans
                flush synchronously; `None` (production) installs no processor for it.
            drop_span_names: names dropped by wrapping EVERY real exporter in
                `_NameFilteringSpanExporter`. Empty ⇒ no wrapper is installed at all.
            id_generator: a custom `IdGenerator`; the learning session-trace projection
                passes a deterministic one so a re-export upserts rather than duplicates.
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
    """The OpenInference `TraceConfig` governing the auto-instrumented `LLM` span's
        CONTENT capture (D25).

        `None` when *hide_content* is False — the library default, where the span carries
        the raw prompt and completion. Otherwise every content channel is suppressed while
        the shape/timing attributes (`llm.model_name`, `llm.token_count.*`, `llm.provider`)
        stay intact. The SYSTEM default is REVEAL (`otlp_hide_llm_content=False`), so the
        online Phoenix project is entity-bearing by default and must be access-controlled
        like the audit/session store; hiding is the explicit opt-out.
    """
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

        FIRST-CALLER-WINS: `OpenAIInstrumentor` is a process-global singleton, so the first
        call's *hide_content* config wins for the whole process and a later call with a
        different value silently no-ops. The paired `LLMExceptionEventScrubber` is not
        subject to this — each provider gets its own.

        *hide_content* True emits no raw prompt/completion, only shape/timing/model-name/
        token-counts. The SYSTEM default is REVEAL: `app.py` resolves `otlp_hide_llm_content`
        (defaulting False) and passes it here, making the runtime Phoenix project
        entity-bearing. This PARAM's default stays True; call sites drive it.
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
    """Generic span helper — sets the OpenInference span-kind attribute plus whatever
        *attributes* the caller supplies (already redacted).

        *context* is the OTel parent to start under (`None` ⇒ the ambient current context).
        Passing one rehydrated from a W3C `traceparent` makes this span a CHILD of that
        remote parent, so a session's spans join ONE trace across process boundaries.

        *record_exception* defaults True. Callers that wrap real work AND must stay
        content-free even on error MUST pass `record_exception=False`: an SDK error embeds
        response bodies and a landing error references entity-bearing spans, so recording it
        leaks content even with verbose off. The span STATUS is still set on exception, so
        the error stays visible in traces — only the detail is withheld.
    """
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
    """Serialize the CURRENT span's context to a W3C `traceparent` string, or `None` when
        no span is recording. Call it INSIDE the span that should become the cross-process
        parent; the value rides the outgoing envelope for `context_from_traceparent`.
    """
    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier.get("traceparent")


def context_from_traceparent(traceparent: str | None) -> Context | None:
    """Rehydrate a parent `Context` from a `traceparent` carried on an incoming envelope,
        for `span(..., context=...)`.

        FAIL-OPEN by two DIFFERENT mechanisms: MISSING (`None`/`""`) returns `None` and the
        span starts as a normal ROOT; MALFORMED returns a non-`None` but EMPTY `Context`
        (the W3C propagator does not raise on garbage), which is also a root. The guarantee
        callers depend on is "no VALID remote parent, and never a crash" — a bad value costs
        the cross-process chaining and nothing else.
    """
    if not traceparent:
        return None
    try:
        return _PROPAGATOR.extract({"traceparent": traceparent})
    except Exception:  # noqa: BLE001 - a malformed traceparent must never crash a consume/promote
        return None


def agent_span(
    tracer: Tracer, *, scope_hash: str, turn_index: int
) -> Any:
    """One turn (`AGENT`; `session.id` / `scope_hash` / turn index)."""
    return span(
        tracer,
        "agent.turn",
        OpenInferenceSpanKindValues.AGENT,
        {"scope_hash": scope_hash, "turn.index": turn_index},
    )


def chain_span(
    tracer: Tracer, name: str, *, attributes: dict[str, Any] | None = None
) -> Any:
    """Context assembly / budget-guard-adjacent chain stages (`CHAIN`)."""
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
    record_exception: bool = True,
) -> Any:
    """Each tool call (`TOOL`).

        DEFAULT (D25) posture: *args* are already SQL-literal-masked by the caller,
        *result_preview* is `None`, and *reveal_complex_args* is False — tool RESULTS never
        reach a span and nested dict/list args are skipped entirely.

        DEBUG posture (`RuntimeSettings.otlp_disable_redaction=True`): the caller passes the
        REAL args, a *result_preview*, and `reveal_complex_args=True`, which makes the
        project ENTITY-BEARING and requires it to be access-controlled like the audit store.
        This module does not itself redact; it forwards whatever the caller supplies.

        *result_preview* columns/shape land as scalar attributes and its rows are
        JSON-serialized onto `tool.result.preview_rows`, because a span attribute cannot
        hold a ragged list-of-lists — the same reason *reveal_complex_args* JSON-serializes
        each non-scalar arg value onto `tool.args.{key}`.

        *record_exception* is forwarded to `span` and every caller that WRAPS REAL WORK
        passes False (`dispatch/tool_envelope.py`). A tool crash-guard means an exception
        normally never reaches the span, but OTel's default would write
        `exception.message`/`exception.stacktrace` — free text derived from a query, a slot
        value or a row — onto a TOOL span if one ever did. The span status is still set on
        exception, so the error stays visible; only the content-bearing detail is withheld.
        It stays True by default for the POST-HOC callers, which open the span around
        nothing and so have no exception to record either way.
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
    return span(
        tracer,
        f"tool.{tool_name}",
        OpenInferenceSpanKindValues.TOOL,
        attributes,
        record_exception=record_exception,
    )


def guardrail_span(
    tracer: Tracer, name: str, *, attributes: dict[str, Any] | None = None
) -> Any:
    """Budget-cap checks / denial classification (`GUARDRAIL`)."""
    return span(tracer, name, OpenInferenceSpanKindValues.GUARDRAIL, attributes)


def embedding_span(tracer: Tracer, *, model: str, input_count: int) -> Any:
    """The custom embedding-API call (`resolveValues` ranking, D24/D71).

        The auto-instrumentor covers only the agent LLM, not this custom endpoint, so
        `HttpEmbeddingClient` wraps its POST here manually. Vector counts, model id and
        latency only — the embedded TEXT is never logged (D25).
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
    """The custom reranker-API call and the retrieval-pipeline rerank stage (D24/D71).

        Two call sites share it: `HttpRerankerClient` wraps its POST (no `reranked` flag —
        the transport knows nothing of pipeline-level degrade), and `retrieval/pipeline.py`
        wraps the STAGE, passing `reranked=False` on the degrade path so a skipped rerank is
        still visible in traces. Document counts, model id and the flag only — never the
        reranker QUERY or the DOCUMENT text (D25).
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
    """The retrieval-pipeline vector-recall stage, per corpus (`CHAIN`).

        Structural counters only: corpus name, the recall fan-out `k`, how many candidates
        the index returned, and how many blueprint candidates the scope pre-filter dropped.
        The QUESTION TEXT is never an attribute (D25), nor are candidate ids, intent, or
        chunk text.
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
    # --- trim-aware re-fetch exemption (loop/read_guard.py) ---
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
    # --- the empty-answer gate (05 §K) ---
    # `loop_empty_answer_refused.incomplete_reason` /
    # `loop_empty_answer_exhausted.incomplete_reason` — the PROVIDER'S OWN word for why
    # a round-trip ended early (`max_output_tokens`, `content_filter`, `incomplete`),
    # or `""` for an ordinary completion that simply carried no text. It is the whole
    # value of these two events: "the model chose to say nothing" and "the completion
    # was cut off at the token cap" look identical from the loop and need opposite
    # fixes. D25-safe — a provider-vocabulary status word, containing no prompt, no
    # answer and no identifier, and the model's own (absent) text is never placed here.
    "incomplete_reason",
    # --- the answer-prose scrub (ISSUES I1) ---
    # `loop_answer_prose_redacted.redaction_count` — HOW MANY identifier-shaped
    # tokens were replaced with a visible marker in the prose that reached the
    # user. A count of the SCRUB'S OWN work, and the only number that makes the
    # event actionable (one redaction is a slip; twelve is a model narrating the
    # schema). The MATCHED TOKENS ARE NOT AND MUST NOT BE ADDED, in either
    # direction: a match is by definition an identifier the runtime just decided
    # the user may not see, and it may be a COLUMN NAME, which is deliberately
    # absent from this list — putting it here would publish through telemetry
    # exactly what was withheld from the answer. The `exit` that produced the
    # prose is already allowlisted above.
    "redaction_count",
)


def guardrail_observer(tracer: Tracer) -> Callable[[str, dict[str, Any]], None]:
    """Build a `(event, payload) -> None` observer emitting one GUARDRAIL span per `loop_*`
        `AgentLoop` stage-boundary event.

        Factored out of `app.py`'s composition root so this exact redaction-allowlist
        behavior is unit-testable without spinning up the HTTP app.
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
