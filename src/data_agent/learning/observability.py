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

2. **The D25 verbose GATE (`verbose=`) — amended 2026-07-15, extended 2026-08-10.** By
   deliberate operator choice the SETTING defaults VERBOSE (`LEARNING_TRACE_VERBOSE=true`):
   the triage/consume/extract/judge/promote/land helpers set human-readable attributes
   (the user question, a transcript preview, the accepted SQL, the learned
   intent/slots/rationale, the extractor's decline detail, the judge's reason and the
   prior-art block it was shown, the blueprint id/intent/canonical_key) BY DEFAULT, so the
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


def log_tracing_status(
    logger: logging.Logger, *, otlp_endpoint: str, service_name: str, process: str
) -> None:
    """Say AT STARTUP whether spans will actually leave this process.

    `LearningSettings.otlp_endpoint` defaults to `""` and `configure_tracing` answers an
    empty endpoint with a NO-OP provider — deliberately (zero infra required to run the
    loop), but SILENTLY. The observed cost of the silence: a full day of live runs that
    produced ZERO Phoenix spans, with the cause only discoverable by reading source for
    the name of the environment variable. Every learning entrypoint calls this
    immediately after `configure_learning_tracing` so the answer is the first thing in
    the log, mirroring how `scripts/run_ui_runtime_real.py` prints its OTLP posture.

    OFF is a WARNING, not an INFO: it is a supported configuration, but it is also the
    configuration in which every diagnostic this package emits to a span is discarded,
    and that must not be something an operator infers from an absence."""
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
    session_status: str | None = None,
    skip_reason: str | None = None,
    reclaimed: bool | None = None,
    verbose: bool = False,
    question: str | None = None,
    transcript_preview: str | None = None,
) -> Any:
    """One consume (design §10 `learning.consume`, CHAIN) — the parent of the
    triage/extract spans, started under the enqueue-propagated *context* so it joins
    the session's trace. *outcome* ∈ {`done`, `dedup_skip`, `dead_letter`, `skip`,
    `ack_terminal`}.

    SHAPE-only by default. With *verbose*, ALSO carries the user `question` + a short
    `transcript_preview` of what the chat was about (D25 entity-bearing — see module
    docstring).

    `session_status` / `skip_reason` / `reclaimed` are the SKIP-PATH attributes and they
    are the reason a skip is now visible at all. A delivery whose session is not
    claimable used to return silently: no span, no log, no counter, while the message
    stayed in the PEL and was reclaimed until it dead-lettered — every step invisible.
    The ONE fact a debugger needs is the state the claim was refused FROM
    (`session_status`), so it is an attribute and not a message; `skip_reason` is the
    closed-vocabulary label that makes the classes countable
    (`consumer.py::_claim_decision` owns both vocabularies), and `reclaimed` says which
    delivery path produced the outcome. All three are SHAPE-only: a lifecycle label, a
    reason code, a bool — no transcript, no content, nothing to gate."""
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


def _extract_outcome(candidate_count: int, review_count: int) -> str:
    """The extract span's three-way outcome.

    ORDER MATTERS: a session that both extracted a candidate and parked a declined
    sibling for review reads as `extracted`, because the question this label answers is
    "did the loop produce anything?" and the answer is yes. `review_count` is on the same
    span for the sessions where it did not."""
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
    """The S3 grounded-extractor outcome (`learning.extract`, CHAIN). SHAPE-only by
    default (D25): candidate/decline COUNTS + decline reason codes + hint labels.
    `outcome=extracted` when any candidate was produced, else `declined`.

    `correction_count` is the corrective turns the extractor spent telling the model
    that a candidate could not be READ (`extractor.py`). It sits in the SHAPE-only set
    because it is a plain integer, and it is here rather than in a log line because it
    is the loop's prompt-quality signal: 0 on nearly every session is the healthy
    reading, and a rate that climbs means the tool schema and the system prompt are
    asking for something models keep mis-packaging. Correlate it with
    `learning.extract.outcome` — corrections that end in `extracted` are the loop
    healing, corrections that end in `declined` are a prompt to go fix.

    With *verbose*, ALSO carries the accepted SQL, the learned blueprint `intent`, the
    `slots` plan (e.g. `department→dbpcm_warehouse.employee.Department`), the extractor
    `rationale`, and `decline_details` (D25 entity-bearing — see module docstring).

    `decline_details` is the SENTENCE behind each `decline_reasons` code, and it is the
    half of a decline that an operator actually needs. `bad_role` names a class of
    failure; "slot pay_period has no binds_to" names the thing to go fix. The codes stay
    SHAPE-only (they are a closed vocabulary and the rates are read off them), while the
    detail is gated because it interpolates MODEL-authored strings — a slot name, a role,
    a type the model invented — and, in the `totality_violation` case, a SQL literal out
    of the analyst's own query. It is gated with `accepted_sql`, alongside which it adds
    no new class of content; `consumer.py::_decline_details` owns that argument and the
    bounding. A session that produced nothing and says nothing about why is the exact
    hole this attribute closes.

    `review_count` is the fail-to-review candidates this extraction PERSISTED for a
    human to complete (`docs/decisions/learning-declined-candidate-review.md`), and it is
    its own number for the reason the decision doc gives: it measures how often the
    parameterization form turns out to be unfillable, which is exactly the quantity that
    disappears if it is folded into `candidate_count`. So `candidate_count` stays 0 on
    these sessions — nothing was extracted — and the third `outcome` value is what makes
    the two kinds of "declined" separable in a group-by. The reason codes are UNCHANGED
    and still appear in `decline_reasons`, so every existing Phoenix query keeps working
    and simply gains a way to split the outcome it was already counting."""
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

    SHAPE-only by default; GATED-VERBOSE since 2026-08-10, and this span used to refuse
    a `verbose` parameter on principle. The refusal is reversed by deliberate operator
    choice, and the reasoning that replaces it is worth stating because the old reasoning
    is still in the git history: a judge that CANCELS work while the only readable
    account of why lives in an audit bucket behind separate credentials is a judge nobody
    tunes. `outcome=dropped` with a tier and a float answers "how often"; it does not
    answer "was that drop right", and the drop gate is the one thing in this loop that
    destroys work irrecoverably. So under *verbose* the span carries the whole basis of
    the decision — what the model was SHOWN (`prior_art`, the rendered block verbatim),
    what it NAMED (`covered_by`), and what it SAID (`reason`).

    It is GATED and not hardcoded, deliberately: the reverse of an absolute must not be
    another absolute. `LEARNING_TRACE_VERBOSE=false` restores the previous shape-only
    behaviour of this span exactly, and that switch is what makes the operator's choice
    reversible without a code change.

    **`reason` is ENTITY-BEARING and is the reason this gate exists.** It is free model
    prose about a REAL analyst session — capped and single-lined by
    `judge/schema.py::parse_assessment`, but not scanned by any leakage gate — and it can
    name a department, a cost centre or a person. `prior_art` is corpus text
    (`extractor/prior_art.py::render_prior_art_block`, the SAME renderer the model was
    fed, so the span cannot drift from the prompt) and is bounded but likewise not a
    reviewed surface. With verbose ON — the default — the `learning-loop` Phoenix project
    therefore holds the same class of content as the `learning_audit` bucket and MUST be
    access-controlled to the same standard (D51). That is the price of the reveal, and it
    is the one thing an operator must decide BEFORE turning it on rather than after.

    `threshold` is SHAPE-only (a configured float, no content) and always present. It is
    here because `confidence` alone is unreadable: the two bars differ per stage and are
    retunable, so "0.82" means nothing without the number it was compared against, and
    joining a span against a config file is not a thing anyone does at 2am.

    The span is the DENOMINATOR; the audit record is the numerator. Deliberately: the
    store holds only verdicts a judge actually gave (fabricating one would poison the
    dataset that decides whether composable blueprints are worth building), so the
    several reasons a session was NOT judged exist only here. Rates that come out of it:

      * `outcome=dropped` — work cancelled. Cross-check against
        `SELECT count(*) FROM learning_audit WHERE record_type='judge_verdict' AND
        dropped=true`; a divergence means drops are happening without records, which is
        the one failure this design refuses.
      * `outcome=skipped_unavailable` — the graph or the embedder was down. NOT a
        corpus that holds nothing, and never a drop.
      * `outcome=skipped_below_floor` vs `skipped_above_band` — the band is mistuned in
        one direction or the other. Together with `best_similarity` this is what makes
        the band tunable from evidence rather than from taste.
      * `outcome=failed` — the model errored, timed out, or emitted something
        unparseable. Every one of them proceeded to extraction; a rising rate is a
        broken judge quietly costing what it was built to save.
      * `would_drop=true AND shadow=true` — SHADOW MODE. The judge is running for real
        and discarding nothing; this is the rollout dashboard ("what would we have
        thrown away last week?"), readable here as well as in the audit bucket. Outside
        shadow mode `would_drop` and `dropped` are always equal.
      * `reused=true` — a redelivery served from the content-keyed audit record instead
        of a second, non-idempotent model call.
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
    "log_tracing_status",
    "promote_span",
    "sweep_span",
    "triage_span",
]
