"""Unit tests for `export_session_trace` — projecting ONE reconstructed session
lifecycle into Phoenix as a SINGLE synthetic OTel trace (learning/trace/project.py).

Captured WITHOUT a Phoenix collector: a provider is built via the real
`configure_tracing(span_exporter=..., id_generator=...)` seam with an
`InMemorySpanExporter` + the module's `_DeterministicIdGenerator`, exactly as the
Layer-3 launcher does; `export_session_trace` is pure emission, so the emitted span
tree is read straight off the exporter and asserted.

The suite pins the four load-bearing invariants of the projection:

  * the span TREE (names / OpenInference kinds / parent-child links / one trace id);
  * the D25 SHAPE-ONLY gate (verbose OFF ⇒ NO entity-bearing key ever reaches a span,
    even when the candidate/evidence are SEEDED with real intent/template/quote/
    rationale — the release-blocking invariant);
  * the VERBOSE (entity-bearing) posture (those keys appear, carrying the values);
  * DETERMINISTIC ids (re-export ⇒ identical trace+span ids ⇒ Phoenix upserts).

Plus robustness (unsettled leakage, unchecked dedup, malformed timestamps, a
non-dict generalization), the `None`-session short-circuit, and the id-generator unit.
"""

from __future__ import annotations

import json
from dataclasses import replace

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.learning.candidate.verdicts import DedupVerdict, DriftStamp, LeakageVerdict
from data_agent.learning.trace.project import (
    _DeterministicIdGenerator,
    export_session_trace,
)
from data_agent.learning.trace.reconstruct import CandidateTrace, SessionTrace
from data_agent.runtime.observability.tracing import configure_tracing

from ._trace_fixtures import make_candidate, make_evidence, make_session

HASH = "hash-1"
SID = "sess-1"

# The FIVE entity-bearing attribute keys that MUST NEVER appear on any span with
# verbose OFF (the release-blocking D25 shape-only invariant).
_ENTITY_KEYS = (
    "learning.quote",
    "learning.extract.intent",
    "learning.generalization.template",
    "learning.extract.resolves",
    "learning.extract.rationale",
)

# Rich entity-bearing content seeded so a leak of ANY of the five keys is detectable.
INTENT = "total earnings for a department in a given year"
TEMPLATE = "SELECT sum({metric}) FROM {tbl} WHERE dept = {dept}"
RESOLVES = {"earnings": "payroll.payroll_fact.gross_pay"}
RATIONALE = "reusable department-earnings report"
QUOTE = "Jane Doe earns $85,000"
EVIDENCE_REF = "evidence::sess-1::a"


# --- helpers -----------------------------------------------------------------


def _export(
    trace: SessionTrace, *, session_id: str = SID, verbose: bool = False
) -> tuple[str | None, list]:
    """Emit *trace* against a fresh in-memory-backed provider seeded by *session_id*
    and return `(returned_trace_id, finished_spans)`."""
    exporter = InMemorySpanExporter()
    provider = configure_tracing(
        otlp_endpoint="",
        service_name="learning-sessions",
        project_name="learning-sessions",
        span_exporter=exporter,
        id_generator=_DeterministicIdGenerator(session_id),
    )
    tracer = provider.get_tracer("learning-sessions")
    trace_id = export_session_trace(trace, tracer, verbose=verbose)
    return trace_id, list(exporter.get_finished_spans())


def _by_name(spans: list) -> dict[str, list]:
    out: dict[str, list] = {}
    for span in spans:
        out.setdefault(span.name, []).append(span)
    return out


def _kind(span) -> str:
    return span.attributes[SpanAttributes.OPENINFERENCE_SPAN_KIND]


def _rich_candidate() -> CandidateTrace:
    """A fully-populated candidate: settled leakage verdict, a dedup verdict, a drift
    stamp with non-None fields, an entity-bearing payload (intent/template/resolves),
    a rationale, and one entity-bearing evidence snapshot (quote)."""
    env = make_candidate(
        HASH,
        0,
        status="validated",
        entity_scan=LeakageVerdict(result="pass", scanner="regex+ner+llm").to_doc(),
    )
    env = replace(
        env,
        payload={
            "intent": INTENT,
            "kind": "single",
            "resolves": RESOLVES,
            "generalization": {"template": TEMPLATE},
        },
        extractor_rationale=RATIONALE,
        dedup=DedupVerdict(
            canonical_key="sha256:abc",
            matched_id="artifact-9",
            similarity=0.83,
            action="merge",
            layer="soft",
        ),
        drift=DriftStamp(
            status="suspect",
            last_drift_check_at="2026-07-02T00:00:00+00:00",
            probes=("grain_integrity",),
            failed_probe="grain_integrity",
        ),
    )
    return CandidateTrace(envelope=env, evidence=[make_evidence(EVIDENCE_REF, quote=QUOTE)])


# --- 1. span tree shape ------------------------------------------------------


def test_span_tree_shape_names_kinds_parents_and_single_trace_id() -> None:
    """2 candidates (one with 1 evidence snapshot, one with 0) ⇒ exactly 4 spans:
    one AGENT `learning.session` root, two CHAIN `learning.candidate`, one RETRIEVER
    `learning.evidence`; evidence→candidate→session parent links; ALL spans share ONE
    trace id equal to the returned 32-hex string; the root has no parent."""
    trace = SessionTrace(
        session_id=SID,
        session=make_session(SID, learning_content_hash=HASH),
        candidates=[
            CandidateTrace(
                envelope=make_candidate(HASH, 0), evidence=[make_evidence(EVIDENCE_REF)]
            ),
            CandidateTrace(envelope=make_candidate(HASH, 1), evidence=[]),
        ],
    )

    trace_id, spans = _export(trace)

    assert len(spans) == 4
    by_name = _by_name(spans)
    assert len(by_name["learning.session"]) == 1
    assert len(by_name["learning.candidate"]) == 2
    assert len(by_name["learning.evidence"]) == 1

    root = by_name["learning.session"][0]
    evidence = by_name["learning.evidence"][0]
    candidates = by_name["learning.candidate"]

    # OpenInference span kinds.
    assert _kind(root) == OpenInferenceSpanKindValues.AGENT.value
    assert all(_kind(c) == OpenInferenceSpanKindValues.CHAIN.value for c in candidates)
    assert _kind(evidence) == OpenInferenceSpanKindValues.RETRIEVER.value

    # Parent/child links: root has no parent; candidates parent to root; the evidence
    # parents to the candidate that OWNS it (candidate ordinal 0).
    assert root.parent is None
    assert all(c.parent.span_id == root.context.span_id for c in candidates)

    owner = next(c for c in candidates if c.context.span_id == evidence.parent.span_id)
    assert owner.attributes["learning.candidate_id"] == "candidate::hash-1::0"

    # ONE trace id across every span, and it equals the returned hex string.
    assert trace_id is not None
    assert {format(s.context.trace_id, "032x") for s in spans} == {trace_id}
    # Root-level shape attributes.
    assert root.attributes["session.id"] == SID
    assert root.attributes["learning.candidate_count"] == 2
    assert root.attributes["learning.evidence_count"] == 1


# --- 2. SHAPE-ONLY invariant (release-blocking) ------------------------------


def test_verbose_off_never_leaks_entity_bearing_attributes() -> None:
    """With verbose=False, NONE of the five entity-bearing keys appear on ANY span —
    even though the candidate/evidence are seeded with real intent/template/resolves/
    rationale/quote (so a gate leak would FAIL here). The non-PII shape attributes
    that SHOULD be present are present."""
    trace = SessionTrace(
        session_id=SID,
        session=make_session(SID, learning_content_hash=HASH),
        candidates=[_rich_candidate()],
    )

    _trace_id, spans = _export(trace, verbose=False)

    # The critical assertion: no entity-bearing key on any emitted span.
    for span in spans:
        for key in _ENTITY_KEYS:
            assert key not in span.attributes, (
                f"SHAPE-ONLY LEAK: {key!r} present on span {span.name!r} with verbose OFF"
            )

    by_name = _by_name(spans)
    cand = by_name["learning.candidate"][0]
    ev = by_name["learning.evidence"][0]

    # Shape attributes that MUST be present on the candidate span.
    assert cand.attributes["learning.candidate_id"] == "candidate::hash-1::0"
    assert cand.attributes["learning.candidate.status"] == "validated"
    assert cand.attributes["learning.leakage.result"] == "pass"
    # Dedup verdict labels.
    assert cand.attributes["learning.dedup.action"] == "merge"
    assert cand.attributes["learning.dedup.layer"] == "soft"
    assert cand.attributes["learning.dedup.matched_id"] == "artifact-9"
    assert cand.attributes["learning.dedup.similarity"] == 0.83
    # Drift stamp labels.
    assert cand.attributes["learning.drift.status"] == "suspect"
    assert cand.attributes["learning.drift.last_check"] == "2026-07-02T00:00:00+00:00"
    assert cand.attributes["learning.drift.failed_probe"] == "grain_integrity"

    # Shape ids on the evidence span (the non-PII refs + the origin trace crosslink).
    assert ev.attributes["learning.evidence_ref"] == EVIDENCE_REF
    assert ev.attributes["learning.turn_ref"] == 0
    assert ev.attributes["learning.tool_call_ref"] == "tc1"
    assert ev.attributes["learning.origin_trace_id"] == "trace-1"


# --- 3. verbose posture ------------------------------------------------------


def test_verbose_on_carries_seeded_entity_bearing_values() -> None:
    """With verbose=True, the five entity-bearing keys ARE present and carry the
    seeded values: quote on the evidence span; intent/template/resolves/rationale on
    the candidate span."""
    trace = SessionTrace(
        session_id=SID,
        session=make_session(SID, learning_content_hash=HASH),
        candidates=[_rich_candidate()],
    )

    _trace_id, spans = _export(trace, verbose=True)

    by_name = _by_name(spans)
    cand = by_name["learning.candidate"][0]
    ev = by_name["learning.evidence"][0]

    assert ev.attributes["learning.quote"] == QUOTE
    assert cand.attributes["learning.extract.intent"] == INTENT
    assert cand.attributes["learning.generalization.template"] == TEMPLATE
    assert cand.attributes["learning.extract.resolves"] == json.dumps(RESOLVES)
    assert cand.attributes["learning.extract.rationale"] == RATIONALE


# --- 4. deterministic / idempotent ids ---------------------------------------


def _id_map(spans: list) -> dict[str, int]:
    """Map each span to a STABLE key (name, or candidate_id / evidence_ref) → span id."""
    out: dict[str, int] = {}
    for span in spans:
        if span.name == "learning.candidate":
            key = f"candidate:{span.attributes['learning.candidate_id']}"
        elif span.name == "learning.evidence":
            key = f"evidence:{span.attributes['learning.evidence_ref']}"
        else:
            key = span.name
        out[key] = span.context.span_id
    return out


def test_same_session_reexport_is_idempotent_in_trace_and_span_ids() -> None:
    """Exporting the SAME SessionTrace twice (two fresh providers, each seeded with
    `_DeterministicIdGenerator(session_id)`) yields the SAME trace id AND the same
    per-span ids — so a Phoenix re-export UPSERTS rather than duplicates."""
    trace = SessionTrace(
        session_id=SID,
        session=make_session(SID, learning_content_hash=HASH),
        candidates=[
            CandidateTrace(
                envelope=make_candidate(HASH, 0), evidence=[make_evidence(EVIDENCE_REF)]
            ),
            CandidateTrace(envelope=make_candidate(HASH, 1), evidence=[]),
        ],
    )

    trace_id_a, spans_a = _export(trace, session_id=SID)
    trace_id_b, spans_b = _export(trace, session_id=SID)

    assert trace_id_a == trace_id_b
    assert _id_map(spans_a) == _id_map(spans_b)


def test_reexport_after_session_evolution_keeps_stable_span_ids() -> None:
    """Content-keyed span ids (`"root"` / `candidate:{id}` / `evidence:{ref}`) — NOT
    positional counters — keep every unchanged span's id INVARIANT when the session
    EVOLVES between exports, so Phoenix upserts each span in place and only the genuinely
    new content mints a new id.

    V1: candidate A (one evidence) + candidate B (status "candidate"). V2 is the SAME
    session evolved: candidate A gains a SECOND evidence snapshot, candidate B advances
    to "validated" and gains a drift stamp. Across the two exports (fresh providers, same
    session_id seed) the trace id, the root/A/B ids, and candidate A's ORIGINAL evidence
    id are stable; only the new evidence mints a new id; and candidate B's evolved status
    rides its SAME span id."""
    session = make_session(SID, learning_content_hash=HASH)
    ref_a1 = "evidence::sess-1::a1"
    ref_a2 = "evidence::sess-1::a2"  # the genuinely-new snapshot added in V2

    trace_v1 = SessionTrace(
        session_id=SID,
        session=session,
        candidates=[
            CandidateTrace(
                envelope=make_candidate(HASH, 0), evidence=[make_evidence(ref_a1)]
            ),
            CandidateTrace(envelope=make_candidate(HASH, 1, status="candidate"), evidence=[]),
        ],
    )
    trace_id_v1, spans_v1 = _export(trace_v1, session_id=SID)
    ids_v1 = _id_map(spans_v1)

    # V2 — the SAME session, evolved: candidate A gains a second evidence snapshot;
    # candidate B advances to "validated" and gains a drift stamp.
    cand_b_v2 = replace(
        make_candidate(HASH, 1, status="validated"),
        drift=DriftStamp(status="clean", last_drift_check_at="2026-07-03T00:00:00+00:00"),
    )
    trace_v2 = SessionTrace(
        session_id=SID,
        session=session,
        candidates=[
            CandidateTrace(
                envelope=make_candidate(HASH, 0),
                evidence=[make_evidence(ref_a1), make_evidence(ref_a2)],
            ),
            CandidateTrace(envelope=cand_b_v2, evidence=[]),
        ],
    )
    trace_id_v2, spans_v2 = _export(trace_v2, session_id=SID)
    ids_v2 = _id_map(spans_v2)

    key_a = "candidate:candidate::hash-1::0"
    key_b = "candidate:candidate::hash-1::1"
    key_a1 = f"evidence:{ref_a1}"
    key_a2 = f"evidence:{ref_a2}"

    # Trace id + every UNCHANGED span's id are stable-in-place across the evolution.
    assert trace_id_v1 == trace_id_v2
    assert ids_v2["learning.session"] == ids_v1["learning.session"]
    assert ids_v2[key_a] == ids_v1[key_a]
    assert ids_v2[key_b] == ids_v1[key_b]
    assert ids_v2[key_a1] == ids_v1[key_a1]  # candidate A's ORIGINAL evidence span

    # The genuinely-new evidence snapshot mints a NEW id absent from V1.
    assert key_a2 not in ids_v1
    assert ids_v2[key_a2] not in ids_v1.values()

    # The evolved attribute rides candidate B's SAME span id.
    cand_b_v2_span = next(
        s
        for s in spans_v2
        if s.name == "learning.candidate"
        and s.attributes["learning.candidate_id"] == "candidate::hash-1::1"
    )
    assert cand_b_v2_span.context.span_id == ids_v1[key_b]
    assert cand_b_v2_span.attributes["learning.candidate.status"] == "validated"


def test_different_session_ids_produce_different_trace_ids() -> None:
    """Two DIFFERENT session_id seeds ⇒ two DIFFERENT trace ids (no cross-session
    collision in the deterministic id space)."""
    trace = SessionTrace(
        session_id=SID,
        session=make_session(SID, learning_content_hash=HASH),
        candidates=[CandidateTrace(envelope=make_candidate(HASH, 0), evidence=[])],
    )

    trace_id_a, _ = _export(trace, session_id="sess-A")
    trace_id_b, _ = _export(trace, session_id="sess-B")

    assert trace_id_a != trace_id_b


# --- 5. None session ---------------------------------------------------------


def test_none_session_returns_none_and_emits_zero_spans() -> None:
    """A SessionTrace with `session=None` (reconstructor found no session doc) is a
    no-op: `export_session_trace` returns None and emits ZERO spans."""
    trace = SessionTrace(
        session_id=SID,
        session=None,
        candidates=[],
        errors=["session 'sess-1' not found in the session store"],
    )

    trace_id, spans = _export(trace)

    assert trace_id is None
    assert spans == []


# --- 6. robustness -----------------------------------------------------------


def test_unsettled_leakage_renders_pending_without_raising() -> None:
    """An unsettled S3 self-check (`entity_scan={"result":"pending"}`) renders the
    leakage RESULT label as `pending` — `LeakageVerdict.from_doc` is never called."""
    env = make_candidate(HASH, 0, entity_scan={"result": "pending"})
    trace = SessionTrace(
        session_id=SID,
        session=make_session(SID, learning_content_hash=HASH),
        candidates=[CandidateTrace(envelope=env, evidence=[])],
    )

    _trace_id, spans = _export(trace)

    cand = _by_name(spans)["learning.candidate"][0]
    assert cand.attributes["learning.leakage.result"] == "pending"


def test_dedup_none_renders_unchecked_sentinel() -> None:
    """`envelope.dedup is None` (pre-S6) ⇒ the candidate span carries the single
    `learning.dedup.action == "unchecked"` sentinel and no other dedup labels."""
    env = replace(make_candidate(HASH, 0), dedup=None)
    trace = SessionTrace(
        session_id=SID,
        session=make_session(SID, learning_content_hash=HASH),
        candidates=[CandidateTrace(envelope=env, evidence=[])],
    )

    _trace_id, spans = _export(trace)

    cand = _by_name(spans)["learning.candidate"][0]
    assert cand.attributes["learning.dedup.action"] == "unchecked"
    assert "learning.dedup.layer" not in cand.attributes
    assert "learning.dedup.matched_id" not in cand.attributes
    assert "learning.dedup.similarity" not in cand.attributes


def test_malformed_or_missing_timestamps_still_export_all_spans() -> None:
    """Malformed/missing ISO timestamps on the session/candidate/evidence do NOT
    crash the export (`_iso_to_ns` returns None ⇒ the SDK falls back to wall-clock);
    all spans are still emitted."""
    session = make_session(SID, learning_content_hash=HASH)
    session.created_at = "not-a-timestamp"
    session.last_activity = "also-bad"
    env = replace(make_candidate(HASH, 0), created_at="garbage")
    ev = replace(make_evidence(EVIDENCE_REF), snapshotted_at="nope")
    trace = SessionTrace(
        session_id=SID,
        session=session,
        candidates=[CandidateTrace(envelope=env, evidence=[ev])],
    )

    trace_id, spans = _export(trace)

    assert trace_id is not None
    by_name = _by_name(spans)
    assert len(by_name["learning.session"]) == 1
    assert len(by_name["learning.candidate"]) == 1
    assert len(by_name["learning.evidence"]) == 1


def test_non_dict_generalization_under_verbose_omits_template_without_crashing() -> None:
    """A non-dict `generalization` in the payload does not crash under verbose=True —
    the template attr is simply absent while the other verbose attrs still appear."""
    env = make_candidate(HASH, 0)
    env = replace(
        env,
        payload={"intent": INTENT, "generalization": "not-a-dict", "resolves": RESOLVES},
    )
    trace = SessionTrace(
        session_id=SID,
        session=make_session(SID, learning_content_hash=HASH),
        candidates=[CandidateTrace(envelope=env, evidence=[])],
    )

    _trace_id, spans = _export(trace, verbose=True)

    cand = _by_name(spans)["learning.candidate"][0]
    assert "learning.generalization.template" not in cand.attributes
    # The other verbose attrs still resolve normally.
    assert cand.attributes["learning.extract.intent"] == INTENT
    assert cand.attributes["learning.extract.resolves"] == json.dumps(RESOLVES)


# --- 7. _DeterministicIdGenerator unit ---------------------------------------


def test_deterministic_id_generator_trace_id_is_nonzero_and_stable() -> None:
    """`generate_trace_id()` is a nonzero pure function of the seed: two calls on the
    SAME seed return the SAME value; different seeds return different values."""
    gen = _DeterministicIdGenerator(SID)
    first = gen.generate_trace_id()
    second = gen.generate_trace_id()

    assert first != 0
    assert first == second  # pure function of the seed — stable across calls

    other = _DeterministicIdGenerator("sess-other").generate_trace_id()
    assert other != first


def test_deterministic_id_generator_span_ids_are_nonzero_and_advance() -> None:
    """`generate_span_id()` mints a nonzero id that ADVANCES (distinct across
    successive calls, driven by the per-instance monotonic counter)."""
    gen = _DeterministicIdGenerator(SID)
    ids = [gen.generate_span_id() for _ in range(5)]

    assert all(span_id != 0 for span_id in ids)
    assert len(set(ids)) == len(ids)  # every successive id is distinct
