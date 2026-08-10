"""Learning-loop tracing (D23/D24/D25, design §10).

Both processes trace to the Phoenix `learning-loop` project via their own
`TracerProvider` (`service.name = "learning-loop"`). Reuses
`runtime/observability/tracing.py`'s `configure_tracing` (same no-op-provider
behavior when no OTLP endpoint is set — zero infra required to run) and its
`span` primitive, adding the learning-specific span helpers.

TWO cross-cutting concerns live in this module:

1. **Span chaining (ONE trace per session).** A session's learning journey spans
   three decoupled hops (sweeper → Redis → consumer → candidate store → cron
   scheduler). The sweeper's `learning.enqueue` span is the per-session trace
   ROOT; it injects its W3C `traceparent` onto the `LearningJob`, the consumer
   extracts it to nest `learning.consume`/`triage`/`extract` under it, and the
   extractor stamps the same `traceparent` onto each `CandidateEnvelope` so the
   scheduler's `promote`/`land` spans continue the SAME trace. Every helper that
   can be a cross-process child accepts a `context=` parent (rehydrated via
   `context_from_traceparent`); a missing/malformed value ⇒ a normal root span
   (fail-open).

2. **The D25 verbose GATE (`verbose=`) — amended 2026-07-15.** By deliberate
   operator choice the SETTING now defaults VERBOSE (`LEARNING_TRACE_VERBOSE=true`):
   the triage/consume/extract/promote/land helpers set human-readable attributes
   (the user question, a transcript preview, the accepted SQL, the learned
   intent/slots/rationale, the blueprint id/intent/canonical_key) BY DEFAULT, so the
   `learning-loop` (and `learning-sessions`) Phoenix project is ENTITY-BEARING BY
   DEFAULT and therefore subject to the SAME in-boundary PII posture + access control
   as the `learning_audit` and session stores (D51). Set `LEARNING_TRACE_VERBOSE=false`
   to restore the D25 shape-only telemetry posture, where the ONLY attributes set are
   non-PII counters/labels + `session.id` (the trace-grouping key) + `content_hash`/
   `message_id` (non-PII audit keys) and no transcript/SQL/question/intent content is
   emitted. The gate MECHANISM (the `verbose` param + `_verbose_attrs`) is unchanged;
   only the default posture flipped. NOTE: the per-helper docstrings below still say
   "SHAPE-only by default" — that describes the `verbose=False` PARAM default (still
   accurate); the composition root now passes `verbose=True` by default via the
   flipped setting.
"""

from __future__ import annotations

from typing import Any

from openinference.semconv.trace import OpenInferenceSpanKindValues
from opentelemetry.context import Context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Tracer

from data_agent.runtime.observability.tracing import (
    configure_tracing,
    context_from_traceparent,
    get_tracer,
    inject_current_traceparent,
    span,
)

_TRACER_NAME = "learning-loop"


def _verbose_attrs(verbose: bool, mapping: dict[str, Any]) -> dict[str, Any]:
    """The D25 verbose gate: return the human-readable attrs ONLY when *verbose* is
    True (dropping any `None` values), else an EMPTY dict — so with verbose OFF the
    key is never even present on the span (not merely None-valued)."""
    if not verbose:
        return {}
    return {key: value for key, value in mapping.items() if value is not None}


def _learning_span(
    tracer: Tracer,
    name: str,
    kind: OpenInferenceSpanKindValues,
    attributes: dict[str, Any] | None = None,
    *,
    context: Context | None = None,
) -> Any:
    """The learning-loop's `span()` wrapper — ALWAYS `record_exception=False` (D25).
    Unlike the online-runtime spans (which are `with span(...): pass` — no body, so
    nothing can raise inside), several learning spans now WRAP real work
    (`learning.consume` → the summary loader + LLM extractor; `learning.land` → the
    landing writer). `start_as_current_span`'s default `record_exception=True` would
    attach `exception.message`/`exception.stacktrace` to the exported span on ANY
    raise — and an OpenAI SDK error embeds response bodies (prompt/session content)
    while a landing error references the entity-bearing `forbidden_spans` — leaking
    content EVEN WITH VERBOSE OFF. Recording exception detail is therefore refused for
    EVERY learning span; the span STATUS is still set on error, so failures stay
    visible in traces, only the entity-bearing detail is withheld."""
    return span(
        tracer, name, kind, attributes, context=context, record_exception=False
    )


def configure_learning_tracing(
    *, otlp_endpoint: str, service_name: str = "learning-loop"
) -> TracerProvider:
    """Build the learning processes' `TracerProvider` (Phoenix `learning-loop`
    project). Delegates to the runtime `configure_tracing` so the exporter /
    no-op-provider behavior is identical; does NOT install the provider globally
    (the entrypoint does that once).

    Passes `project_name="learning-loop"` so the spans land in the named
    `learning-loop` Phoenix project directly from CODE — Phoenix groups by the
    `openinference.project.name` resource attribute, so this replaces the old
    `OTEL_RESOURCE_ATTRIBUTES=openinference.project.name=learning-loop` env hack
    the demo launcher used to rely on."""
    return configure_tracing(
        otlp_endpoint=otlp_endpoint,
        service_name=service_name,
        project_name="learning-loop",
    )


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
    return _learning_span(
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
    tracer: Tracer, *, session_id: str, content_hash: str, message_id: str | None = None
) -> Any:
    """One enqueue (design §10 `learning.enqueue`, CHAIN) — the per-session trace
    ROOT (its `traceparent` is injected onto the job so the consumer/scheduler spans
    chain under it). `session.id` is the D25 trace-grouping key; `content_hash`/
    `message_id` are non-PII (`message_id` may be set on the yielded span AFTER the
    XADD returns it). SHAPE-only: enqueue never carries transcript content."""
    return _learning_span(
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
    tracer: Tracer,
    *,
    session_id: str,
    outcome: str,
    delivery_count: int,
    context: Context | None = None,
    verbose: bool = False,
    question: str | None = None,
    transcript_preview: str | None = None,
) -> Any:
    """One consume (design §10 `learning.consume`, CHAIN) — the parent of the
    triage/extract spans, started under the enqueue-propagated *context* so it joins
    the session's trace. *outcome* ∈ {`done`, `dedup_skip`, `dead_letter`}.

    SHAPE-only by default. With *verbose*, ALSO carries the user `question` + a short
    `transcript_preview` of what the chat was about (D25 entity-bearing — see module
    docstring)."""
    attrs: dict[str, Any] = {
        "session.id": session_id,
        "learning.outcome": outcome,
        "learning.delivery_count": delivery_count,
    }
    attrs.update(
        _verbose_attrs(
            verbose,
            {
                "learning.question": question,
                "learning.transcript_preview": transcript_preview,
            },
        )
    )
    return _learning_span(
        tracer, "learning.consume", OpenInferenceSpanKindValues.CHAIN, attrs, context=context
    )


def disabled_span(tracer: Tracer, *, process: str) -> Any:
    """Kill-switch trip (design §10 `learning.disabled`, GUARDRAIL). *process* ∈
    {`sweeper`, `consumer`}."""
    return _learning_span(
        tracer,
        "learning.disabled",
        OpenInferenceSpanKindValues.GUARDRAIL,
        {"learning.process": process},
    )


def triage_span(
    tracer: Tracer,
    *,
    session_id: str,
    decision: str,
    reason: str,
    target_hints: tuple[str, ...] = (),
    verbose: bool = False,
    question: str | None = None,
    transcript_preview: str | None = None,
) -> Any:
    """Triage verdict (Slice-2 §3.4 `learning.triage`, CHAIN). SHAPE-only by default
    (D25): the decision label, the K#/skip_* reason code, and the target-hint labels.
    With *verbose*, ALSO carries the user `question` + a short `transcript_preview`
    (D25 entity-bearing — see module docstring)."""
    attrs: dict[str, Any] = {
        "session.id": session_id,
        "learning.triage.decision": decision,
        "learning.triage.reason": reason,
        "learning.triage.target_hints": ",".join(target_hints),
    }
    attrs.update(
        _verbose_attrs(
            verbose,
            {
                "learning.question": question,
                "learning.transcript_preview": transcript_preview,
            },
        )
    )
    return _learning_span(tracer, "learning.triage", OpenInferenceSpanKindValues.CHAIN, attrs)


def extract_stub_span(
    tracer: Tracer, *, session_id: str, target_hints: tuple[str, ...] = ()
) -> Any:
    """The S2 stub extractor seam (§5.2 `learning.extract`, CHAIN). Emits
    `outcome=would_extract` + hint labels only — writes nothing. Retained for the
    consumer's back-compat path when no extractor is injected; S3 uses
    `extract_span` below when the real extractor runs."""
    return _learning_span(
        tracer,
        "learning.extract",
        OpenInferenceSpanKindValues.CHAIN,
        {
            "session.id": session_id,
            "learning.extract.outcome": "would_extract",
            "learning.extract.target_hints": ",".join(target_hints),
        },
    )


def extract_span(
    tracer: Tracer,
    *,
    session_id: str,
    candidate_count: int,
    decline_count: int,
    decline_reasons: tuple[str, ...] = (),
    target_hints: tuple[str, ...] = (),
    verbose: bool = False,
    accepted_sql: str | None = None,
    intent: str | None = None,
    slots: str | None = None,
    rationale: str | None = None,
) -> Any:
    """The S3 grounded-extractor outcome (`learning.extract`, CHAIN). SHAPE-only by
    default (D25): candidate/decline COUNTS + decline reason codes + hint labels.
    `outcome=extracted` when any candidate was produced, else `declined`.

    With *verbose*, ALSO carries the accepted SQL, the learned blueprint `intent`, the
    `slots` plan (e.g. `department→dbpcm_warehouse.employee.Department`), and the
    extractor `rationale` (D25 entity-bearing — see module docstring)."""
    attrs: dict[str, Any] = {
        "session.id": session_id,
        "learning.extract.outcome": "extracted" if candidate_count else "declined",
        "learning.extract.candidate_count": candidate_count,
        "learning.extract.decline_count": decline_count,
        "learning.extract.decline_reasons": ",".join(decline_reasons),
        "learning.extract.target_hints": ",".join(target_hints),
    }
    attrs.update(
        _verbose_attrs(
            verbose,
            {
                "learning.accepted_sql": accepted_sql,
                "learning.extract.intent": intent,
                "learning.extract.slots": slots,
                "learning.extract.rationale": rationale,
            },
        )
    )
    return _learning_span(tracer, "learning.extract", OpenInferenceSpanKindValues.CHAIN, attrs)


def dedup_span(
    tracer: Tracer,
    *,
    session_id: str,
    candidate_id: str,
    action: str,
    layer: str,
    similarity: float,
    prior_art_tier: str | None = None,
    matched_status: str | None = None,
    matched_origin: str | None = None,
) -> Any:
    """The S6 dedup verdict (`learning.dedup`, CHAIN).

    ALWAYS SHAPE-ONLY — no `verbose` parameter, unlike its neighbours. Every attribute
    here is a machine tag, a float, a tier label or a lifecycle status; there is no
    entity-bearing content to gate, and adding a verbose branch would only create a place
    for someone to put the candidate's intent later.

    Exists so the loop's DROP decisions are COUNTS and not just log lines. Three rates
    the plan asks for come out of these attributes:

      * `action=redundant_with_canon` — a DETERMINISTIC structural-key identity with the
        MCP canon. The agent owns this blueprint and failed to recall it.
      * `action=merge AND prior_art_tier=mcp` — the same story on softer (cosine)
        evidence, routed to a human instead of dropped.
        A rising rate of either means RETRIEVAL is missing artifacts it already holds;
        the fix is in the recall path, not in the learning loop.
      * `action=increment AND matched_status IN (rejected, retired)` — a byte-identical
        re-derivation of an idea a human already DECLINED. A different question ("how
        good are our rejections / are analysts repeatedly reaching for something we said
        no to?"), and without the status tag it is indistinguishable from an ordinary
        hit-count bump against a live artifact.
      * `matched_origin=corpus` — the soft match came from the `learning_corpus` bucket,
        i.e. from an IN-FLIGHT sibling candidate no graph read can see. A rising rate is
        concurrency: analysts converging on the same question inside one landing cycle.
        `matched_origin=graph` is a match against something already landed.
    """
    return _learning_span(
        tracer,
        "learning.dedup",
        OpenInferenceSpanKindValues.CHAIN,
        {
            "session.id": session_id,
            "learning.candidate_id": candidate_id,
            "learning.dedup.action": action,
            "learning.dedup.layer": layer,
            "learning.dedup.similarity": similarity,
            # "" (not None) when the verdict matched nothing / matched something with no
            # tier or status, so both attributes are ALWAYS present and a Phoenix filter
            # never has to distinguish "zero" from "no data" via a missing key.
            "learning.dedup.prior_art_tier": prior_art_tier or "",
            "learning.dedup.matched_status": matched_status or "",
            "learning.dedup.matched_origin": matched_origin or "",
        },
    )


def promote_span(
    tracer: Tracer,
    *,
    session_id: str,
    candidate_id: str,
    action: str,
    context: Context | None = None,
    verbose: bool = False,
    blueprint_id: str | None = None,
    blueprint_intent: str | None = None,
    canonical_key: str | None = None,
) -> Any:
    """The scheduler's promote edge (`learning.promote`, CHAIN), started under the
    candidate-propagated *context* so it continues the session's trace. SHAPE-only by
    default: `session.id`, the candidate id, and the promotion *action*. With
    *verbose*, ALSO the `blueprint_id`, the learned `blueprint_intent`, and the S6
    `canonical_key` (D25 entity-bearing — see module docstring)."""
    attrs: dict[str, Any] = {
        "session.id": session_id,
        "learning.candidate_id": candidate_id,
        "learning.promote.action": action,
    }
    attrs.update(
        _verbose_attrs(
            verbose,
            {
                "learning.blueprint_id": blueprint_id,
                "learning.blueprint.intent": blueprint_intent,
                "learning.canonical_key": canonical_key,
            },
        )
    )
    return _learning_span(
        tracer, "learning.promote", OpenInferenceSpanKindValues.CHAIN, attrs, context=context
    )


def land_span(
    tracer: Tracer,
    *,
    session_id: str,
    candidate_id: str,
    context: Context | None = None,
    verbose: bool = False,
    blueprint_id: str | None = None,
    blueprint_intent: str | None = None,
    canonical_key: str | None = None,
) -> Any:
    """The scheduler's land-into-corpus step (`learning.land`, CHAIN), nested under
    the promote span. Same SHAPE-only/verbose contract as `promote_span`."""
    attrs: dict[str, Any] = {
        "session.id": session_id,
        "learning.candidate_id": candidate_id,
    }
    attrs.update(
        _verbose_attrs(
            verbose,
            {
                "learning.blueprint_id": blueprint_id,
                "learning.blueprint.intent": blueprint_intent,
                "learning.canonical_key": canonical_key,
            },
        )
    )
    return _learning_span(
        tracer, "learning.land", OpenInferenceSpanKindValues.CHAIN, attrs, context=context
    )


def learning_recall_span(
    tracer: Tracer,
    *,
    session_id: str,
    context: Context | None = None,
) -> Any:
    """The demo's recall probe (`learning.recall`, RETRIEVER), wrapped under the
    session's *context* so the forget/recall demonstration reads in the same trace.
    SHAPE-only: no query text (design §3.5). Named distinctly from the runtime
    retrieval `tracing.recall_span` (different signature) to avoid shadowing."""
    return _learning_span(
        tracer,
        "learning.recall",
        OpenInferenceSpanKindValues.RETRIEVER,
        {"session.id": session_id},
        context=context,
    )


__all__ = [
    "configure_learning_tracing",
    "consume_span",
    "context_from_traceparent",
    "dedup_span",
    "disabled_span",
    "enqueue_span",
    "extract_span",
    "extract_stub_span",
    "get_learning_tracer",
    "inject_current_traceparent",
    "land_span",
    "learning_recall_span",
    "promote_span",
    "sweep_span",
    "triage_span",
]
