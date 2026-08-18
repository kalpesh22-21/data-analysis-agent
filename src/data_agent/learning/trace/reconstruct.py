"""Pure, injectable reconstruction of one session's learning-loop journey.

Joins the THREE durable stores a human would otherwise correlate by hand across three daemon
terminals: the SESSION store (what the session was, its `learning_status`, the trail it was
learned from), the CANDIDATE store (every candidate for that `content_hash`), and the AUDIT
store (the quotes each candidate's `evidence_refs` point at). The walk is ORDINAL rather than
query-based, so it needs no new store methods.

READ-ONLY by construction (D72): the real `get_session_with_cas` CREATES the doc on miss,
which is a write, so `_read_session` prefers the store's read-only accessor. Every failure is
collected into `SessionTrace.errors` rather than raised, so the CLI is robust to partial data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from data_agent.learning.audit.models import EvidenceSnapshot
from data_agent.learning.candidate.models import CandidateEnvelope, mint_candidate_id
from data_agent.runtime.session.models import SessionDoc

# Safety cap on the ordinal walk so a corrupt/adversarial store can never spin the
# loop forever (a real extraction emits a handful of candidates, never thousands).
_ORDINAL_CAP = 1000


@dataclass
class CandidateTrace:
    """One candidate envelope plus the resolved evidence snapshots it references."""

    envelope: CandidateEnvelope
    evidence: list[EvidenceSnapshot] = field(default_factory=list)


@dataclass
class SessionTrace:
    """The full reconstructed journey of one session (partial data tolerated)."""

    session_id: str
    session: SessionDoc | None = None
    candidates: list[CandidateTrace] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def _read_session(session_store, session_id: str) -> tuple[SessionDoc | None, object]:
    """Read the session doc READ-ONLY, returning `(doc | None, cas)`.

    Prefers the store's private `_get_doc`, because the public `get_session_with_cas` CREATES the
    doc on miss on the real store — which would be a write AND would masquerade a missing session
    as an empty one. Injected fakes without `_get_doc` fall back to the public method.
    """
    read_only = getattr(session_store, "_get_doc", None)
    if callable(read_only):
        return await read_only(session_id)
    result = await session_store.get_session_with_cas(session_id)
    if result is None:
        return None, None
    return result[0], result[1] if len(result) > 1 else None


async def reconstruct_session_trace(
    session_id: str,
    session_store,
    candidate_store,
    audit_store,
) -> SessionTrace:
    """Reconstruct the `SessionTrace` for *session_id* from the durable stores.

    Never raises for expected partial-data conditions (a missing session, no `content_hash`, an
    absent audit store, missing evidence refs): each is recorded in `SessionTrace.errors` and a
    best-effort trace is still returned.
    """
    trace = SessionTrace(session_id=session_id)

    # 1. Load the session (read-only; never create-on-miss).
    try:
        session, _cas = await _read_session(session_store, session_id)
    except Exception as exc:  # noqa: BLE001 — the CLI must never traceback on a store error.
        trace.errors.append(f"failed to load session {session_id!r}: {exc}")
        return trace
    if session is None:
        trace.errors.append(f"session {session_id!r} not found in the session store")
        return trace
    trace.session = session

    # 2. content_hash gate — no hash ⇒ the session never entered the learning loop.
    content_hash = session.learning_content_hash
    if not content_hash:
        trace.errors.append(
            "session has no learning_content_hash — it never entered the learning "
            "loop; there are no candidates to reconstruct"
        )
        return trace

    # 3. Ordinal walk over candidate::<content_hash>::<ordinal> until the first gap.
    hit_cap = True
    for ordinal in range(_ORDINAL_CAP):
        candidate_id = mint_candidate_id(content_hash, ordinal)
        try:
            envelope = await candidate_store.get(candidate_id)
        except Exception as exc:  # noqa: BLE001
            # L1: the walk is ABORTED here — ordinals ≥ this one were never read, so
            # the candidate list is a PREFIX, not the complete set. Say so, so a
            # truncated list is not mistaken for a full one.
            trace.errors.append(
                f"failed to read candidate {candidate_id!r}: {exc}; ordinal walk "
                f"aborted at ordinal {ordinal} — later candidates were not read, so "
                "the candidate list may be incomplete"
            )
            hit_cap = False
            break
        if envelope is None:
            hit_cap = False
            break
        candidate = CandidateTrace(envelope=envelope)
        await _resolve_evidence(candidate, audit_store, trace)
        trace.candidates.append(candidate)
    if hit_cap:
        trace.errors.append(
            f"ordinal walk hit the safety cap ({_ORDINAL_CAP}); candidate list may be truncated"
        )

    # 4. Note (once) when evidence could not be resolved for lack of an audit store.
    if audit_store is None and any(c.envelope.evidence_refs for c in trace.candidates):
        trace.errors.append(
            "audit store not configured — evidence quotes were not resolved (candidates "
            "carry only their evidence_refs)"
        )

    return trace


async def _resolve_evidence(candidate: CandidateTrace, audit_store, trace: SessionTrace) -> None:
    """Resolve a candidate's `evidence_refs` against the audit store, if one is present.

    Records a note for each missing or absent ref rather than raising.
    """
    if audit_store is None:
        return
    cid = candidate.envelope.candidate_id
    for ref in candidate.envelope.evidence_refs:
        try:
            snapshot = await audit_store.read(ref)
        except Exception as exc:  # noqa: BLE001
            trace.errors.append(f"failed to read evidence {ref!r} for {cid!r}: {exc}")
            continue
        if snapshot is None:
            trace.errors.append(f"evidence {ref!r} for {cid!r} is missing or expired")
            continue
        candidate.evidence.append(snapshot)
