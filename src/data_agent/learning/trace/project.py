"""Project one reconstructed session lifecycle into Phoenix as ONE synthetic trace.

The live learning loop scatters a session's spans across three decoupled processes
(sweeper → consumer → scheduler). This module instead takes the already-reconstructed
`SessionTrace` (from `reconstruct.py`, read straight out of the durable Couchbase
stores) and emits it as a SINGLE, clean per-session tree into a dedicated Phoenix
project `learning-sessions`:

    learning.session            [AGENT]      (the session)
      └─ learning.candidate     [CHAIN]      (one per extracted candidate)
           └─ learning.evidence [RETRIEVER]  (one per evidence snapshot)

Trace/span ids are DETERMINISTIC and CONTENT-KEYED (the trace id from `session_id`;
each span id from a stable key — `"root"`, `candidate:<id>`, `evidence:<ref>`), so
re-exporting the same session UPSERTS the same spans in Phoenix instead of
duplicating them — and a span's id stays invariant as the session EVOLVES (a new
evidence snapshot, a landed drift stamp, a status transition), so re-export refreshes
each span in place rather than leaving stale positional duplicates.

PII posture (D25, amended 2026-07-15 — read `learning/observability.py`'s module
docstring; this mirrors it EXACTLY). The projection honors the SAME verbose gate the
rest of the learning loop uses (`_verbose_attrs`, imported — never reinvented). The
SETTING (`LearningSettings.learning_trace_verbose`) now defaults VERBOSE by deliberate
operator choice, so this project is ENTITY-BEARING BY DEFAULT and MUST be
access-controlled like the `learning_audit`/session stores (D51):

  * VERBOSE (`verbose=True`, the amended DEFAULT): the candidate/evidence spans carry
    the entity-bearing content — the learned blueprint `intent`, the generalized
    `template`, the `resolves` map, the extractor `rationale`, and the evidence
    `quote` — in ADDITION to the shape attributes below.
  * SHAPE-ONLY (`verbose=False`, now the opt-OUT): the ONLY attributes emitted are
    non-PII shape/labels/counts/statuses — session id, learning/candidate statuses,
    content hash, candidate/evidence counts, confidence, the leakage RESULT label, the
    dedup/drift verdict labels, the evidence ref/turn/tool-call/origin-trace ids. NO
    quote, NO intent, NO template, NO resolves, NO rationale reaches a span. Set
    `learning_trace_verbose=false` to restore this D25 telemetry posture.

`export_session_trace` is PURE emission: it takes a `tracer` and emits spans; it
builds no providers and reads no settings (that is `build_session_export_provider` +
the script). This keeps it unit-testable against an `InMemorySpanExporter` injected
via `configure_tracing(span_exporter=...)`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.trace import Span, Tracer

from data_agent.learning.candidate.verdicts import LeakageVerdict
from data_agent.learning.observability import _verbose_attrs
from data_agent.runtime.observability.tracing import configure_tracing

from .reconstruct import CandidateTrace, SessionTrace

_SEED_PREFIX = b"learning-session:"


class _DeterministicIdGenerator(IdGenerator):
    """OTel `IdGenerator` that mints STABLE, CONTENT-KEYED ids from a `session_id`.

    The trace id is a pure function of the session id. Each span id is a pure
    function of the seed + a STABLE content key (`"root"`, `f"candidate:{id}"`,
    `f"evidence:{ref}"`) that `_start` sets on `next_key` immediately before
    `start_span`. Keying on stable content — NOT a positional counter — makes a
    span's id INVARIANT under tree growth: if a candidate later gains an evidence
    snapshot, or a drift stamp lands, or a status advances, every OTHER span keeps
    its id, so a re-export UPSERTS each session/candidate/evidence span IN PLACE
    (refreshing its attributes) instead of leaving the first export's spans behind
    as stale duplicates. OTel rejects all-zero ids, so a zero digest is forced to 1.

    `next_key` unset (should never happen — `_start` is the single choke point that
    sets it) falls back to a monotonic counter so id minting can never crash.
    """

    def __init__(self, session_id: str) -> None:
        self._seed = _SEED_PREFIX + session_id.encode("utf-8")
        self._counter = 0
        # Set by `_start` right before each `start_span`; consumed here once.
        self.next_key: str | None = None

    def generate_trace_id(self) -> int:
        digest = hashlib.sha256(self._seed).digest()[:16]
        return int.from_bytes(digest, "big") or 1

    def generate_span_id(self) -> int:
        key = self.next_key
        self.next_key = None
        if key is None:
            # Defensive fallback — never reached when spans go through `_start`.
            self._counter += 1
            key = f"_counter:{self._counter}"
        material = self._seed + b":span:" + key.encode("utf-8")
        digest = hashlib.sha256(material).digest()[:8]
        return int.from_bytes(digest, "big") or 1


def build_session_export_provider(*, otlp_endpoint: str, session_id: str) -> TracerProvider:
    """Build the `learning-sessions` Phoenix `TracerProvider` for *session_id*.

    Wires the deterministic id generator so re-exports are idempotent. An empty
    *otlp_endpoint* yields a no-op provider (no exporter) exactly as
    `configure_tracing` does everywhere else.
    """
    return configure_tracing(
        otlp_endpoint=otlp_endpoint,
        service_name="learning-sessions",
        project_name="learning-sessions",
        id_generator=_DeterministicIdGenerator(session_id),
    )


def _iso_to_ns(iso: str | None) -> int | None:
    """ISO-8601 → epoch nanoseconds, or `None` for a missing/malformed timestamp
    (the SDK then falls back to wall-clock — never a crash on bad data)."""
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso).timestamp() * 1e9)
    except (ValueError, TypeError):
        return None


def _start(
    tracer: Tracer,
    name: str,
    kind: OpenInferenceSpanKindValues,
    parent_ctx: Context | None,
    attrs: dict[str, Any],
    *,
    start_ns: int | None,
    id_generator: _DeterministicIdGenerator | None,
    span_key: str,
) -> Span:
    """Start (do NOT end) a span with the OpenInference kind + non-None *attrs*.

    THE single choke point that pairs a span with its content key: it sets
    *id_generator*`.next_key = span_key` immediately before `start_span`, so the
    span id the SDK mints (via `generate_span_id`) is content-keyed, not positional.
    Keeping this pairing in one place makes it impossible for key and span to drift.
    A `None` generator (a non-SDK / non-deterministic tracer) is tolerated — the
    span still emits, just with the tracer's own (random) id.

    `record_exception=False` (D25): this projection never wraps work that could
    raise, but the flag guarantees no exception detail could ever attach a span."""
    if id_generator is not None:
        id_generator.next_key = span_key
    span_obj = tracer.start_span(
        name, context=parent_ctx, start_time=start_ns, record_exception=False
    )
    span_obj.set_attribute(SpanAttributes.OPENINFERENCE_SPAN_KIND, kind.value)
    for key, value in attrs.items():
        if value is not None:
            span_obj.set_attribute(key, value)
    return span_obj


def _leakage_result(entity_scan: dict[str, Any]) -> str:
    """The leakage RESULT label: `pending` for the S3 self-check sentinel, the
    settled S5 verdict result otherwise, `malformed` if a settled-looking doc fails
    to parse (bare-string hits, etc.). Shape-only — no entity spans."""
    scan = entity_scan or {}
    if not LeakageVerdict.is_settled(scan):
        return "pending"
    try:
        return LeakageVerdict.from_doc(scan).result
    except Exception:  # noqa: BLE001 — a corrupt verdict must not crash the export.
        return "malformed"


def export_session_trace(
    trace_data: SessionTrace, tracer: Tracer, *, verbose: bool = False
) -> str | None:
    """Emit *trace_data* as one synthetic Phoenix trace; return the trace id (32-hex)
    or `None` when there is no session to project.

    PURE emission: no provider construction, no settings reads. Shape-only by default;
    entity-bearing content only under *verbose* (see the module docstring)."""
    session = trace_data.session
    if session is None:
        return None

    # The content-keyed id generator (set by `build_session_export_provider`). A
    # non-SDK tracer without one is tolerated (`_start` guards) — ids are then random.
    id_generator = getattr(tracer, "id_generator", None)

    evidence_total = sum(len(candidate.evidence) for candidate in trace_data.candidates)
    root = _start(
        tracer,
        "learning.session",
        OpenInferenceSpanKindValues.AGENT,
        None,
        {
            "session.id": session.session_id,
            "learning.status": session.learning_status,
            "learning.content_hash": session.learning_content_hash,
            "learning.candidate_count": len(trace_data.candidates),
            "learning.evidence_count": evidence_total,
        },
        start_ns=_iso_to_ns(session.created_at),
        id_generator=id_generator,
        span_key="root",
    )
    root_ctx = trace.set_span_in_context(root)
    trace_id = root.get_span_context().trace_id

    for candidate in trace_data.candidates:
        _emit_candidate(tracer, candidate, root_ctx, id_generator, verbose=verbose)

    root.end(end_time=_iso_to_ns(session.last_activity))
    return format(trace_id, "032x")


def _emit_candidate(
    tracer: Tracer,
    candidate: CandidateTrace,
    root_ctx: Context,
    id_generator: _DeterministicIdGenerator | None,
    *,
    verbose: bool,
) -> None:
    env = candidate.envelope
    payload = env.payload if isinstance(env.payload, dict) else {}

    attrs: dict[str, Any] = {
        "learning.candidate_id": env.candidate_id,
        "learning.candidate.type": env.type,
        "learning.candidate.status": env.status,
        "learning.candidate.confidence": env.confidence,
        "learning.candidate.proposed_action": env.proposed_action,
        "learning.candidate.depends_on": ",".join(env.depends_on),
        "learning.leakage.result": _leakage_result(env.entity_scan),
        "learning.evidence_count": len(candidate.evidence),
    }

    # Dedup: the four verdict labels when present, else the single "unchecked" label.
    if env.dedup is None:
        attrs["learning.dedup.action"] = "unchecked"
    else:
        attrs["learning.dedup.action"] = env.dedup.action
        attrs["learning.dedup.layer"] = env.dedup.layer
        attrs["learning.dedup.matched_id"] = env.dedup.matched_id
        attrs["learning.dedup.similarity"] = env.dedup.similarity

    attrs["learning.drift.status"] = env.drift.status
    attrs["learning.drift.last_check"] = env.drift.last_drift_check_at
    attrs["learning.drift.failed_probe"] = env.drift.failed_probe

    # VERBOSE (entity-bearing) content, gated by the reused D25 helper.
    generalization = payload.get("generalization")
    template = generalization.get("template") if isinstance(generalization, dict) else None
    # Serialize `resolves` only when verbose, with `default=str` so a
    # non-JSON-serializable value can never raise mid-emission and abandon a
    # half-built trace.
    resolves = payload.get("resolves")
    resolves_json = json.dumps(resolves, default=str) if (verbose and resolves) else None
    attrs.update(
        _verbose_attrs(
            verbose,
            {
                "learning.extract.intent": payload.get("intent"),
                "learning.generalization.template": template,
                "learning.extract.resolves": resolves_json,
                "learning.extract.rationale": env.extractor_rationale or None,
            },
        )
    )

    cand_end = env.drift.last_drift_check_at or env.created_at
    cand = _start(
        tracer,
        "learning.candidate",
        OpenInferenceSpanKindValues.CHAIN,
        root_ctx,
        attrs,
        start_ns=_iso_to_ns(env.created_at),
        id_generator=id_generator,
        span_key=f"candidate:{env.candidate_id}",
    )
    cand_ctx = trace.set_span_in_context(cand)

    for snapshot in candidate.evidence:
        start_ns = _iso_to_ns(snapshot.snapshotted_at)
        evidence_attrs: dict[str, Any] = {
            "learning.evidence_ref": snapshot.evidence_ref,
            "learning.turn_ref": snapshot.turn_ref,
            "learning.tool_call_ref": snapshot.tool_call_ref,
            # The ORIGINAL online turn's trace id — the one real cross-link back to
            # the request that produced this evidence.
            "learning.origin_trace_id": snapshot.trace_id,
        }
        evidence_attrs.update(_verbose_attrs(verbose, {"learning.quote": snapshot.quote}))
        evidence = _start(
            tracer,
            "learning.evidence",
            OpenInferenceSpanKindValues.RETRIEVER,
            cand_ctx,
            evidence_attrs,
            start_ns=start_ns,
            id_generator=id_generator,
            span_key=f"evidence:{snapshot.evidence_ref}",
        )
        evidence.end(end_time=start_ns)

    cand.end(end_time=_iso_to_ns(cand_end))


__all__ = [
    "build_session_export_provider",
    "export_session_trace",
]
