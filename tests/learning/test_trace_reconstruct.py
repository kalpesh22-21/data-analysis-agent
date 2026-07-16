"""Unit tests for `reconstruct_session_trace` — the read-only join across the
session / candidate / audit stores (learning/trace/reconstruct.py).

The reconstructor is READ-ONLY by construction (D72) and NEVER raises for expected
partial-data conditions (missing session, no content_hash, missing/absent audit
store, unresolved evidence): each is recorded in `SessionTrace.errors` and a
best-effort trace is still returned. These tests pin exactly that.

Candidates are found by an ORDINAL WALK — `mint_candidate_id(content_hash,
ordinal)` for ordinal = 0, 1, 2, … stopping at the first missing id — so seeding N
candidates means `put`-ing them at ordinals 0..N-1 keyed by the session's
`learning_content_hash` (the `make_candidate` builder does exactly that).
"""

from __future__ import annotations

from data_agent.learning.audit import InMemoryAuditStore
from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.verdicts import LeakageVerdict
from data_agent.learning.trace.reconstruct import SessionTrace, reconstruct_session_trace

from ._trace_fixtures import (
    CasOnlySessionStore,
    ReadOnlySessionStore,
    make_candidate,
    make_evidence,
    make_session,
)

HASH = "hash-1"
SID = "sess-1"


async def _seed_candidates(candidate_store, *envelopes):
    for env in envelopes:
        await candidate_store.put(env)


# --- 1. happy path -----------------------------------------------------------


async def test_happy_path_shape_evidence_and_no_errors():
    """A session with a content_hash + three candidates (varied statuses), each
    evidence ref resolving via the audit store, reconstructs into the expected
    SessionTrace shape: three candidates in ordinal order, evidence attached to the
    RIGHT candidate, and NO errors. Also proves the read-only path (no store write)."""
    session = make_session(SID, learning_content_hash=HASH, learning_status="done")
    session_store = ReadOnlySessionStore(session)

    candidates = InMemoryCandidateStore()
    audit = InMemoryAuditStore()

    ref_a = "evidence::sess-1::a"
    ref_b = "evidence::sess-1::b"
    await audit.snapshot(ref_a, make_evidence(ref_a, quote="Jane Doe earns $85,000"))
    await audit.snapshot(ref_b, make_evidence(ref_b, quote="Acme dept 0420 total"))

    await _seed_candidates(
        candidates,
        make_candidate(HASH, 0, status="extracted", evidence_refs=(ref_a,)),
        make_candidate(HASH, 1, status="candidate", evidence_refs=(ref_b,)),
        # A validated candidate carrying a SETTLED S5 leakage verdict (not pending).
        make_candidate(
            HASH,
            2,
            status="validated",
            evidence_refs=(),
            entity_scan=LeakageVerdict(result="pass", scanner="regex+ner+llm").to_doc(),
        ),
    )

    trace = await reconstruct_session_trace(SID, session_store, candidates, audit)

    assert isinstance(trace, SessionTrace)
    assert trace.session is not None
    assert trace.session.session_id == SID
    assert trace.errors == []

    assert [c.envelope.candidate_id for c in trace.candidates] == [
        "candidate::hash-1::0",
        "candidate::hash-1::1",
        "candidate::hash-1::2",
    ]
    assert [c.envelope.status for c in trace.candidates] == ["extracted", "candidate", "validated"]

    # Evidence is attached to the RIGHT candidate (not smeared across all of them).
    assert [s.quote for s in trace.candidates[0].evidence] == ["Jane Doe earns $85,000"]
    assert [s.quote for s in trace.candidates[1].evidence] == ["Acme dept 0420 total"]
    assert trace.candidates[2].evidence == []

    # Read-only (D72): the reconstructor used `_get_doc` and mutated NOTHING.
    assert session_store.get_doc_calls == [SID]
    assert session_store.write_calls == []


# --- 2. never entered the loop ----------------------------------------------


async def test_no_content_hash_yields_zero_candidates_and_a_note():
    """`learning_content_hash is None` ⇒ the session never entered the learning loop:
    zero candidates, a clear note, no ordinal walk, no crash."""
    session = make_session(SID, learning_content_hash=None, learning_status="active")
    session_store = ReadOnlySessionStore(session)
    candidates = InMemoryCandidateStore()

    trace = await reconstruct_session_trace(SID, session_store, candidates, InMemoryAuditStore())

    assert trace.session is not None
    assert trace.candidates == []
    assert any("never entered the learning loop" in e for e in trace.errors)
    # The walk was never entered — the candidate store was not queried at all.
    assert candidates.all_candidates() == []


# --- 3. missing session (read-only!) ----------------------------------------


async def test_missing_session_records_error_and_performs_no_write():
    """`_get_doc` returns `(None, None)` ⇒ `trace.session is None`, an error is
    recorded, a SessionTrace is STILL returned (no exception) — and CRITICALLY the
    store is never mutated (no create-on-miss): this whole module is read-only."""
    session_store = ReadOnlySessionStore(None)  # empty store — the session is absent
    candidates = InMemoryCandidateStore()

    trace = await reconstruct_session_trace(SID, session_store, candidates, InMemoryAuditStore())

    assert isinstance(trace, SessionTrace)
    assert trace.session is None
    assert any("not found" in e for e in trace.errors)

    # READ-ONLY proof: no create/write happened and the store stayed empty.
    assert session_store.write_calls == []
    assert session_store._docs == {}
    # The candidate walk never ran (no session ⇒ no content_hash to walk).
    assert candidates.all_candidates() == []


# --- 4. no audit store -------------------------------------------------------


async def test_no_audit_store_leaves_evidence_empty_with_a_note():
    """`audit_store=None` ⇒ candidates are still reconstructed but carry NO resolved
    evidence, and a single note explains the audit store was unavailable. No crash."""
    session = make_session(SID, learning_content_hash=HASH)
    session_store = ReadOnlySessionStore(session)
    candidates = InMemoryCandidateStore()
    await _seed_candidates(
        candidates,
        make_candidate(HASH, 0, evidence_refs=("evidence::sess-1::a",)),
    )

    trace = await reconstruct_session_trace(SID, session_store, candidates, None)

    assert len(trace.candidates) == 1
    assert trace.candidates[0].evidence == []
    assert any("audit store not configured" in e for e in trace.errors)


# --- 5. unresolved evidence ref ---------------------------------------------


async def test_unresolved_evidence_ref_is_skipped_with_a_note():
    """An evidence ref the audit store cannot resolve (`read` ⇒ None) is skipped —
    the candidate keeps its OTHER resolved evidence, and a note flags the miss."""
    session = make_session(SID, learning_content_hash=HASH)
    session_store = ReadOnlySessionStore(session)
    candidates = InMemoryCandidateStore()
    audit = InMemoryAuditStore()

    present = "evidence::sess-1::present"
    missing = "evidence::sess-1::missing"  # never snapshotted ⇒ read() returns None
    await audit.snapshot(present, make_evidence(present, quote="a real quote"))

    await _seed_candidates(
        candidates,
        make_candidate(HASH, 0, evidence_refs=(present, missing)),
    )

    trace = await reconstruct_session_trace(SID, session_store, candidates, audit)

    (candidate,) = trace.candidates
    assert [s.quote for s in candidate.evidence] == ["a real quote"]
    assert any(missing in e and "missing or expired" in e for e in trace.errors)


# --- 7. ordinal walk stops at the first gap ---------------------------------


async def test_ordinal_walk_stops_at_first_gap_and_does_not_skip():
    """Candidates put at ordinals 0 and 1 but NOT 2 (with a decoy at ordinal 3):
    exactly TWO are found — the walk stops at the first missing id and never skips a
    gap to reach ordinal 3."""
    session = make_session(SID, learning_content_hash=HASH)
    session_store = ReadOnlySessionStore(session)
    candidates = InMemoryCandidateStore()
    await _seed_candidates(
        candidates,
        make_candidate(HASH, 0),
        make_candidate(HASH, 1),
        # ordinal 2 intentionally absent (the gap)
        make_candidate(HASH, 3),  # decoy that must NOT be reached
    )

    trace = await reconstruct_session_trace(SID, session_store, candidates, InMemoryAuditStore())

    assert [c.envelope.candidate_id for c in trace.candidates] == [
        "candidate::hash-1::0",
        "candidate::hash-1::1",
    ]
    assert trace.errors == []  # stopped cleanly at the gap; never hit the safety cap


# --- 8. fallback session-store shape ----------------------------------------


async def test_fallback_get_session_with_cas_only_still_reconstructs():
    """A session store exposing ONLY the public `get_session_with_cas` (no
    `_get_doc`) still reconstructs — the reconstructor falls back correctly."""
    session = make_session(SID, learning_content_hash=HASH)
    session_store = CasOnlySessionStore(session)
    candidates = InMemoryCandidateStore()
    await _seed_candidates(candidates, make_candidate(HASH, 0), make_candidate(HASH, 1))

    trace = await reconstruct_session_trace(SID, session_store, candidates, InMemoryAuditStore())

    assert trace.session is not None
    assert trace.session.session_id == SID
    assert len(trace.candidates) == 2
    assert trace.errors == []


async def test_fallback_store_missing_session_records_error():
    """The fallback path also handles a missing session (get_session_with_cas ⇒ None)
    without raising: session is None, an error is recorded."""
    session_store = CasOnlySessionStore(None)
    candidates = InMemoryCandidateStore()

    trace = await reconstruct_session_trace(SID, session_store, candidates, InMemoryAuditStore())

    assert trace.session is None
    assert any("not found" in e for e in trace.errors)


# --- 6. pending / unsettled leakage does not break reconstruction -----------


async def test_pending_leakage_entity_scan_reconstructs_without_raising():
    """A candidate whose `entity_scan` is the S3 `{"result": "pending"}` self-check
    (NOT a settled S5 verdict) reconstructs cleanly — the reconstructor never calls
    `LeakageVerdict.from_doc`, so the pending sentinel is carried through verbatim."""
    session = make_session(SID, learning_content_hash=HASH)
    session_store = ReadOnlySessionStore(session)
    candidates = InMemoryCandidateStore()
    await _seed_candidates(
        candidates,
        make_candidate(HASH, 0, entity_scan={"result": "pending"}),
    )

    trace = await reconstruct_session_trace(SID, session_store, candidates, InMemoryAuditStore())

    (candidate,) = trace.candidates
    assert candidate.envelope.entity_scan == {"result": "pending"}
    assert not LeakageVerdict.is_settled(candidate.envelope.entity_scan)
