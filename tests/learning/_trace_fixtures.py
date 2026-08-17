"""Shared Layer-1 builders + session-store fakes for the trace reconstruct/render
tests (learning/trace).

Infra-free. Reuses the real `InMemoryCandidateStore` / `InMemoryAuditStore` fakes
and the real model classes — the only NEW doubles here are two session-store
shapes the reconstructor's `_read_session` must handle:

  * `ReadOnlySessionStore`  — exposes the private `_get_doc` accessor (the PRIMARY,
    truly read-only path: `(None, None)` on miss, never create-on-miss). It ALSO
    carries the real Couchbase footgun methods (`get_session_with_cas`,
    `get_or_create_session`) that CREATE on miss and record into `write_calls`, so a test
    can prove the reconstructor took the read-only path and mutated nothing.
  * `CasOnlySessionStore` — exposes ONLY the public `get_session_with_cas` (no
    `_get_doc`), to cover the fallback branch. Read-only: returns the doc, never
    creates.
"""

from __future__ import annotations

import copy

from data_agent.learning.audit.models import EvidenceSnapshot
from data_agent.learning.candidate.models import CandidateEnvelope, mint_candidate_id
from data_agent.runtime.session.models import SessionDoc, TrailEntry

TS = "2026-07-01T00:00:00+00:00"


# --- model builders ----------------------------------------------------------


def make_trail_entry(
    *,
    turn_index: int = 0,
    tool_call_id: str = "tc1",
    tool_name: str = "runQuery",
    args: dict | None = None,
    status: str = "ok",
) -> TrailEntry:
    return TrailEntry(
        turn_index=turn_index,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        args={"sql": "SELECT sum(gross_pay) FROM payroll.payroll_fact"} if args is None else args,
        status=status,
        error_code=None,
        provenance=frozenset(),
        result_preview=None,
        result_full_ref=None,
        ts=TS,
    )


def make_session(
    session_id: str = "sess-1",
    *,
    learning_content_hash: str | None = "hash-1",
    learning_status: str = "done",
    tool_trail: list[TrailEntry] | None = None,
) -> SessionDoc:
    return SessionDoc(
        session_id=session_id,
        created_at=TS,
        last_activity=TS,
        learning_status=learning_status,
        tool_trail=list(tool_trail if tool_trail is not None else [make_trail_entry()]),
        learning_content_hash=learning_content_hash,
    )


def make_candidate(
    content_hash: str,
    ordinal: int,
    *,
    status: str = "extracted",
    type_: str = "blueprint",
    evidence_refs: tuple[str, ...] = (),
    intent: str = "total earnings for a department in a given year",
    entity_scan: dict | None = None,
    session_id: str = "sess-1",
) -> CandidateEnvelope:
    """Build a candidate whose `candidate_id` is `mint_candidate_id(content_hash,
    ordinal)` — the ONLY id the ordinal walk will look up — so seeding at ordinal N
    is a plain `store.put(make_candidate(hash, N))`."""
    payload: dict = {}
    if type_ == "blueprint":
        payload = {
            "intent": intent,
            "kind": "single",
            "resolves": {"earnings": "payroll.payroll_fact.gross_pay"},
        }
    if entity_scan is None:
        # The S3 preliminary self-check sentinel (NOT a settled S5 verdict).
        entity_scan = {"result": "pending", "hits": [], "self_check_contains_entities": False}
    return CandidateEnvelope(
        candidate_id=mint_candidate_id(content_hash, ordinal),
        type=type_,
        status=status,
        payload=payload,
        source_session=session_id,
        source_trace="trace-1",
        evidence_refs=tuple(evidence_refs),
        extractor_rationale="reusable department-earnings report",
        entity_scan=entity_scan,
        confidence=0.9,
        proposed_action="new",
        depends_on=(),
        content_hash=content_hash,
        created_at=TS,
    )


def make_evidence(
    ref: str,
    *,
    session_id: str = "sess-1",
    quote: str = "Jane Doe earns $85,000",
    turn_ref: int = 0,
    tool_call_ref: str = "tc1",
) -> EvidenceSnapshot:
    return EvidenceSnapshot(
        evidence_ref=ref,
        session_id=session_id,
        trace_id="trace-1",
        turn_ref=turn_ref,
        tool_call_ref=tool_call_ref,
        quote=quote,
        snapshotted_at=TS,
    )


# --- session-store fakes -----------------------------------------------------


class ReadOnlySessionStore:
    """Session-store fake exposing the read-only `_get_doc` accessor (primary path).

    `_get_doc` is a pure dict lookup: `(deepcopy, cas)` on hit, `(None, None)` on
    miss — it NEVER creates. The `get_session_with_cas` / `get_or_create_session` methods
    reproduce the real Couchbase create-on-miss footgun and record into
    `write_calls`, so a test can assert the reconstructor never touched them.
    """

    def __init__(self, doc: SessionDoc | None = None, *, cas: int = 7) -> None:
        self._docs: dict[str, SessionDoc] = {}
        if doc is not None:
            self._docs[doc.session_id] = doc
        self._cas = cas
        self.get_doc_calls: list[str] = []
        self.write_calls: list[tuple[str, str]] = []

    async def _get_doc(self, session_id: str) -> tuple[SessionDoc | None, object]:
        self.get_doc_calls.append(session_id)
        doc = self._docs.get(session_id)
        if doc is None:
            return None, None
        return copy.deepcopy(doc), self._cas

    # --- footgun methods: a correct (read-only) reconstruct MUST NOT call these ---
    async def get_session_with_cas(self, session_id: str) -> tuple[SessionDoc, int]:
        self.write_calls.append(("get_session_with_cas", session_id))
        if session_id not in self._docs:
            self._docs[session_id] = SessionDoc(
                session_id=session_id, created_at=TS, last_activity=TS
            )
        return copy.deepcopy(self._docs[session_id]), self._cas

    async def get_or_create_session(self, session_id: str) -> SessionDoc:
        self.write_calls.append(("get_or_create_session", session_id))
        doc = SessionDoc(session_id=session_id, created_at=TS, last_activity=TS)
        self._docs[session_id] = doc
        return doc


class CasOnlySessionStore:
    """Session-store fake exposing ONLY the public `get_session_with_cas` (no
    `_get_doc`) — exercises the reconstructor's fallback branch. Read-only:
    returns the seeded doc, `None` on miss (never creates)."""

    def __init__(self, doc: SessionDoc | None = None, *, cas: int = 3) -> None:
        self._doc = doc
        self._cas = cas

    async def get_session_with_cas(self, session_id: str) -> tuple[SessionDoc, int] | None:
        if self._doc is None or self._doc.session_id != session_id:
            return None
        return copy.deepcopy(self._doc), self._cas
