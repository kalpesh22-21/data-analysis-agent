"""Learning-loop tracing (D23/D24/D25, design §10).

ONE TRACE PER SESSION: the sweeper's `learning.enqueue` span is the root and injects its
`traceparent` onto the job and thence onto each `CandidateEnvelope`; every helper takes a
`context=` parent (missing/malformed ⇒ a plain root span, fail-open). D25 VERBOSE GATE:
`LEARNING_TRACE_VERBOSE` defaults TRUE, so this Phoenix project is ENTITY-BEARING and MUST
be access-controlled like `learning_audit` and the session store (D51); `false` = shape-only.
"""

from __future__ import annotations

import logging
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
    """The D25 verbose gate: the human-readable attrs only when *verbose*, else an EMPTY dict.

    `None` values are dropped, and with verbose off the key is never present on the span at
    all — not merely None-valued.
    """
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

    Several learning spans WRAP real work, and `record_exception=True` would attach
    `exception.message`/`exception.stacktrace` on ANY raise: an OpenAI SDK error embeds
    response bodies and a landing error references the entity-bearing `forbidden_spans`,
    leaking content EVEN WITH VERBOSE OFF. The span STATUS is still set, so failures stay
    visible in traces; only the entity-bearing detail is withheld.
    """
    return span(
        tracer, name, kind, attributes, context=context, record_exception=False
    )


def configure_learning_tracing(
    *, otlp_endpoint: str, service_name: str = "learning-loop"
) -> TracerProvider:
    """Build the learning processes' `TracerProvider` (Phoenix `learning-loop` project).

    Delegates to the runtime `configure_tracing`, so exporter / no-op-provider behaviour is
    identical, and does NOT install the provider globally (the entrypoint does that once).
    `project_name` lands the spans in the named Phoenix project from CODE rather than via an
    `OTEL_RESOURCE_ATTRIBUTES` env hack.
    """
    return configure_tracing(
        otlp_endpoint=otlp_endpoint,
        service_name=service_name,
        project_name="learning-loop",
    )


def get_learning_tracer(provider: TracerProvider) -> Tracer:
    return get_tracer(provider, _TRACER_NAME)


def log_tracing_status(
    logger: logging.Logger, *, otlp_endpoint: str, service_name: str, process: str
) -> None:
    """Say AT STARTUP whether spans will actually leave this process.

    An empty `otlp_endpoint` gets a NO-OP provider — deliberate (zero infra required to run
    the loop) but SILENT. OFF is a WARNING, not an INFO: it is a supported configuration, but
    it is also the one in which every diagnostic this package emits to a span is discarded.
    """
    if otlp_endpoint:
        logger.info(
            "learning %s tracing ON -> OTLP %s (service.name=%s, Phoenix project=%s)",
            process, otlp_endpoint, service_name, _TRACER_NAME,
        )
        return
    logger.warning(
        "learning %s tracing OFF — OTLP_ENDPOINT is unset/empty, so the tracer is a "
        "NO-OP provider and this process will emit ZERO spans (no learning.sweep / "
        "enqueue / consume / triage / extract / judge in Phoenix). Set "
        "OTLP_ENDPOINT=http://localhost:6006/v1/traces (or your collector) to turn it "
        "on; LEARNING_SERVICE_NAME=%s sets service.name.",
        process, service_name,
    )


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
    """One enqueue (design §10 `learning.enqueue`, CHAIN) — the per-session trace ROOT.

    Its `traceparent` is injected onto the job so the consumer/scheduler spans chain under it.
    SHAPE-only: `session.id` (the D25 trace-grouping key) plus the non-PII
    `content_hash`/`message_id`, the latter settable on the yielded span after the XADD.
    """
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
    session_status: str | None = None,
    skip_reason: str | None = None,
    reclaimed: bool | None = None,
    verbose: bool = False,
    question: str | None = None,
    transcript_preview: str | None = None,
) -> Any:
    """One consume (design §10 `learning.consume`, CHAIN) — parent of the triage/extract spans.

    Started under the enqueue-propagated *context*. *outcome* ∈ {`done`, `dedup_skip`,
    `dead_letter`, `skip`, `ack_terminal`}. SHAPE-only by default; with *verbose*, ALSO the
    user `question` + a short `transcript_preview` (entity-bearing). `session_status` /
    `skip_reason` / `reclaimed` are the SKIP-PATH attributes — the state the claim was refused
    FROM, a closed-vocabulary reason code, and which delivery path produced the outcome
    (`consumer.py::_claim_decision` owns both vocabularies).
    """
    attrs: dict[str, Any] = {
        "session.id": session_id,
        "learning.outcome": outcome,
        "learning.delivery_count": delivery_count,
    }
    if session_status is not None:
        attrs["learning.session_status"] = session_status
    if skip_reason is not None:
        attrs["learning.skip_reason"] = skip_reason
    if reclaimed is not None:
        attrs["learning.reclaimed"] = reclaimed
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
    """Triage verdict (Slice-2 §3.4 `learning.triage`, CHAIN).

    SHAPE-only by default (D25): the decision label, the K#/skip_* reason code, the target-hint
    labels. With *verbose*, ALSO the user `question` + a short `transcript_preview`
    (entity-bearing — see the module docstring).
    """
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
    """The S2 stub extractor seam (§5.2 `learning.extract`, CHAIN): `outcome=would_extract`.

    Hint labels only — writes nothing. Retained for the consumer's back-compat path when no
    extractor is injected; S3 uses `extract_span`.
    """
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


def _extract_outcome(candidate_count: int, review_count: int) -> str:
    """The extract span's three-way outcome.

    ORDER MATTERS: a session that both extracted a candidate and parked a declined sibling for
    review reads as `extracted`, because the question this label answers is "did the loop
    produce anything?". `review_count` is on the same span for the sessions where it did not.
    """
    if candidate_count:
        return "extracted"
    if review_count:
        return "declined_to_review"
    return "declined"


def extract_span(
    tracer: Tracer,
    *,
    session_id: str,
    candidate_count: int,
    decline_count: int,
    decline_reasons: tuple[str, ...] = (),
    target_hints: tuple[str, ...] = (),
    correction_count: int = 0,
    review_count: int = 0,
    verbose: bool = False,
    accepted_sql: str | None = None,
    intent: str | None = None,
    slots: str | None = None,
    rationale: str | None = None,
    decline_details: str | None = None,
) -> Any:
    """The S3 grounded-extractor outcome (`learning.extract`, CHAIN).

    SHAPE-only by default (D25): candidate/decline COUNTS, decline reason codes, hint labels,
    and `correction_count` — the corrective turns spent telling the model a candidate could not
    be READ, which is the loop's prompt-quality signal (corrections ending in `extracted` are
    the loop healing; ending in `declined` they are a prompt to go fix). `review_count` is the
    fail-to-review candidates PERSISTED for a human, kept as its own number so `candidate_count`
    stays 0 on those sessions and the two kinds of "declined" remain separable in a group-by.

    With *verbose*, ALSO the accepted SQL, the learned blueprint `intent`, the `slots` plan, the
    `rationale` and `decline_details` — the last interpolating model-authored strings and, for
    `totality_violation`, a SQL literal (`consumer.py::_decline_details` owns the bounding).
    """
    attrs: dict[str, Any] = {
        "session.id": session_id,
        "learning.extract.outcome": _extract_outcome(candidate_count, review_count),
        "learning.extract.candidate_count": candidate_count,
        "learning.extract.review_count": review_count,
        "learning.extract.decline_count": decline_count,
        "learning.extract.decline_reasons": ",".join(decline_reasons),
        "learning.extract.target_hints": ",".join(target_hints),
        "learning.extract.correction_count": correction_count,
    }
    attrs.update(
        _verbose_attrs(
            verbose,
            {
                "learning.accepted_sql": accepted_sql,
                "learning.extract.intent": intent,
                "learning.extract.slots": slots,
                "learning.extract.rationale": rationale,
                "learning.extract.decline_details": decline_details,
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

    ALWAYS SHAPE-ONLY — no `verbose` parameter, unlike its neighbours: every attribute is a
    machine tag, a float, a tier label or a lifecycle status, and a verbose branch would only
    be somewhere to put the candidate's intent later. Exists so the loop's DROP decisions are
    COUNTS: `action=redundant_with_canon`, and `action=merge AND prior_art_tier=mcp`, mean
    RETRIEVAL is missing artifacts it already holds; `action=increment AND matched_status IN
    (rejected, retired)` is re-derivation of something a human already declined;
    `matched_origin=corpus` is an in-flight sibling no graph read can see.
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


def judge_span(
    tracer: Tracer,
    *,
    session_id: str,
    stage: str,
    outcome: str,
    candidate_id: str | None = None,
    verdict: str | None = None,
    confidence: float = 0.0,
    covered_by_tier: str | None = None,
    best_similarity: float = 0.0,
    threshold: float = 0.0,
    cards_shown: int = 0,
    dropped: bool = False,
    would_drop: bool = False,
    shadow: bool = False,
    reused: bool = False,
    verbose: bool = False,
    reason: str | None = None,
    covered_by: str | None = None,
    prior_art: str | None = None,
) -> Any:
    """The coverage judge's verdict (plan §3b, `learning.judge`, CHAIN).

    SHAPE-only by default, GATED-VERBOSE by deliberate operator choice: under *verbose* the span
    carries the whole basis of the decision — what the model was SHOWN (`prior_art`, the
    rendered block verbatim, from the SAME renderer that fed the prompt), what it NAMED
    (`covered_by`), and what it SAID (`reason`). Both are ENTITY-BEARING and neither is
    leakage-scanned, so with verbose on this Phoenix project holds the same class of content as
    `learning_audit` and MUST be access-controlled to the same standard (D51).

    `threshold` is SHAPE-only and always present, because `confidence` alone is unreadable —
    the bars differ per stage and are retunable. The span is the DENOMINATOR and the audit
    record the numerator: the store holds only verdicts a judge actually gave, so the several
    reasons a session was NOT judged (`skipped_unavailable`, `skipped_below_floor`,
    `skipped_above_band`, `failed`) exist only here.
    """
    attrs: dict[str, Any] = {
        "session.id": session_id,
        "learning.judge.stage": stage,
        "learning.judge.outcome": outcome,
        # "" rather than None throughout, so every attribute is ALWAYS present and a
        # Phoenix filter never has to distinguish "no value" from "no data" via a
        # missing key (the posture `dedup_span` settled on).
        "learning.candidate_id": candidate_id or "",
        "learning.judge.verdict": verdict or "",
        "learning.judge.confidence": confidence,
        "learning.judge.threshold": threshold,
        "learning.judge.covered_by_tier": covered_by_tier or "",
        "learning.judge.best_similarity": best_similarity,
        "learning.judge.cards_shown": cards_shown,
        "learning.judge.dropped": dropped,
        "learning.judge.would_drop": would_drop,
        "learning.judge.shadow": shadow,
        "learning.judge.reused": reused,
    }
    attrs.update(
        _verbose_attrs(
            verbose,
            {
                "learning.judge.reason": reason,
                "learning.judge.covered_by": covered_by,
                "learning.judge.prior_art": prior_art,
            },
        )
    )
    return _learning_span(tracer, "learning.judge", OpenInferenceSpanKindValues.CHAIN, attrs)


def param_judge_span(
    tracer: Tracer,
    *,
    candidate_id: str,
    session_id: str,
    outcome: str,
    verdict: str | None = None,
    confidence: float = 0.0,
    findings: int = 0,
    class_a_findings: int = 0,
    would_discard: bool = False,
    shadow: bool = True,
    recorded: bool = False,
    reused: bool = False,
    model: str = "",
    verbose: bool = False,
    feedback: str | None = None,
    template: str | None = None,
) -> Any:
    """The S4 parameterization judge's verdict (design §D, `learning.param_judge`, CHAIN).

    THE SPAN IS THE DENOMINATOR, the audit record the numerator — the same relationship
    `judge_span` documents, and it matters more here because phase D-1 IS a measurement. The
    store holds only verdicts a judge actually gave, so the reasons a candidate was NOT judged
    (`skipped_not_blueprint`, `skipped_failed_validation`, `skipped_no_entries`, `failed`)
    exist ONLY on this span. Reading the flag rate off the audit rows alone would divide by
    the wrong number.

    `class_a_findings` is separate from `findings` and always present, because it is the count
    the phase-D-2 decision turns on — Class A means the blueprint is WRONG rather than merely
    narrow, and a `revise` carrying only naming nits is a different event entirely.

    SHAPE-only by default. Under *verbose* it also carries what the model SAID (`feedback`) and
    what it judged (`template`) — both ENTITY-BEARING, the template because an inline predicate
    keeps its literal value in it, so the same D51 access-control standard applies as for
    `judge_span`.
    """
    attrs: dict[str, Any] = {
        "session.id": session_id,
        "learning.candidate_id": candidate_id,
        "learning.param_judge.outcome": outcome,
        # "" / 0 rather than None throughout, so every attribute is ALWAYS present and a
        # Phoenix filter never distinguishes "no value" from "no data" via a missing key.
        "learning.param_judge.verdict": verdict or "",
        "learning.param_judge.confidence": confidence,
        "learning.param_judge.findings": findings,
        "learning.param_judge.class_a_findings": class_a_findings,
        "learning.param_judge.would_discard": would_discard,
        "learning.param_judge.shadow": shadow,
        "learning.param_judge.recorded": recorded,
        "learning.param_judge.reused": reused,
        "learning.param_judge.model": model,
    }
    attrs.update(
        _verbose_attrs(
            verbose,
            {
                "learning.param_judge.feedback": feedback,
                "learning.param_judge.template": template,
            },
        )
    )
    return _learning_span(
        tracer, "learning.param_judge", OpenInferenceSpanKindValues.CHAIN, attrs
    )


def revise_span(
    tracer: Tracer,
    *,
    candidate_id: str,
    outcome: str,
    status: str = "",
    entries: int = 0,
    replace: bool = False,
    conflicts: int = 0,
    model: str = "",
    allow_sql: bool = False,
    verbose: bool = False,
    feedback: str | None = None,
    rationale: str | None = None,
    reason: str | None = None,
) -> Any:
    """One LLM revision proposal (design §C, `learning.revise`, CHAIN).

    The only model call on this plane a HUMAN triggers, which is what the span is for: it is
    the sole record that the assistant was asked at all. Nothing is written by this path (the
    proposal goes back for a reviewer to apply through `complete`/`apply_revision`), so without
    a span a refused or empty proposal leaves no trace anywhere — and "the assistant keeps
    suggesting nothing" is exactly the complaint an operator would need to substantiate.

    `outcome` separates the ways it can produce nothing, because they need different fixes:
    `proposed`, `no_snapshot` (the candidate predates the stamp — a MIGRATION signal, not a
    model failure), `withheld_scan` (the leakage gate refused to quote the SQL), `unusable`,
    `timeout`, `failed`, `refused_template_edit` (the model wrote a field this request had no
    contract for).

    Three more belong to the §C.5 SQL-rewrite opt-in, and they are kept apart from the four
    above because none of them is a model failure in the same sense: `proposed_sql_rewrite` is a
    SUCCESS whose blast radius is different (the candidate becomes hand-authored and can no
    longer auto-land, so a rate worth watching on its own); `sql_rewrite_unusable` is a query
    that came back and could not be parsed as a read-only SELECT — a PROMPT signal; and
    `sql_rewrite_unsupported` never reached a model at all, because the candidate was composite.

    `allow_sql` records whether the reviewer opted in. Worth an attribute rather than an
    inference from the outcome: "the assistant was offered the SQL field and chose not to use
    it" and "it was never offered one" are different facts about the same `proposed` span, and
    only one of them says the prompt's PREFER-NOT-TO is working.

    `conflicts` counts diff rows that append cannot apply — the case a reviewer must resolve by
    switching to replace, and worth watching because a high rate means the PROMPT is steering
    toward the wrong mode rather than the reviewer doing anything wrong.

    ⚠ SHAPE-only by default, and the verbose payload here is the sharpest on the plane:
    `feedback` is free text a human typed into a browser and `rationale` is model prose ABOUT
    the unredacted accepted SQL. Both are entity-bearing and neither is leakage-scanned.
    """
    attrs: dict[str, Any] = {
        "learning.candidate_id": candidate_id,
        "learning.revise.outcome": outcome,
        "learning.revise.status": status,
        "learning.revise.entries": entries,
        "learning.revise.replace": replace,
        "learning.revise.conflicts": conflicts,
        "learning.revise.model": model,
        "learning.revise.allow_sql": allow_sql,
    }
    attrs.update(
        _verbose_attrs(
            verbose,
            {
                "learning.revise.feedback": feedback,
                "learning.revise.rationale": rationale,
                "learning.revise.reason": reason,
            },
        )
    )
    return _learning_span(
        tracer, "learning.revise", OpenInferenceSpanKindValues.CHAIN, attrs
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
    """The scheduler's promote edge (`learning.promote`, CHAIN), under the candidate's *context*.

    SHAPE-only by default: `session.id`, the candidate id, and the promotion *action*. With
    *verbose*, ALSO the `blueprint_id`, the learned `blueprint_intent` and the S6
    `canonical_key` (entity-bearing — see the module docstring).
    """
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
    """The scheduler's land-into-corpus step (`learning.land`, CHAIN), nested under promote.

    Same SHAPE-only/verbose contract as `promote_span`.
    """
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
    """The demo's recall probe (`learning.recall`, RETRIEVER), under the session's *context*.

    SHAPE-only: no query text (design §3.5). Named distinctly from the runtime retrieval
    `tracing.recall_span` (different signature) to avoid shadowing.
    """
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
    "log_tracing_status",
    "param_judge_span",
    "promote_span",
    "revise_span",
    "sweep_span",
    "triage_span",
]
