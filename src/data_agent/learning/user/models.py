"""UserKnowledgeRecord — the per-user committed knowledge row (S8, D17).

The ONE learning target allowed to carry entities (05 §Write targets): a durable,
per-user fact stored in the access-controlled per-user bucket and surfaced only in
that user's context. Built from a `user_knowledge` `CandidateEnvelope`; keyed
deterministically off the candidate id so a re-commit UPSERTs (idempotent, D17).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


def mint_record_id(user_id: str, candidate_id: str) -> str:
    """Deterministic per-user record key: `userknow::<user_id>::<candidate_id>`.
    Idempotent — a redelivered candidate commits to the SAME key."""
    return f"userknow::{user_id}::{candidate_id}"


@dataclass(frozen=True)
class UserKnowledgeRecord:
    record_id: str
    user_id: str
    statement: str
    fact_type: str | None
    scope: str
    structured: dict[str, Any] | None
    source_session: str
    source_trace: str
    evidence_refs: tuple[str, ...]
    committed_at: str = ""

    def to_doc(self) -> dict[str, Any]:
        return {
            "_id": self.record_id,
            "record_id": self.record_id,
            "user_id": self.user_id,
            "statement": self.statement,
            "fact_type": self.fact_type,
            "scope": self.scope,
            "structured": self.structured,
            "provenance": {
                "source_session": self.source_session,
                "source_trace": self.source_trace,
                "evidence_ref": list(self.evidence_refs),
            },
            "committed_at": self.committed_at or _now(),
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> UserKnowledgeRecord:
        prov = doc.get("provenance", {})
        return cls(
            record_id=doc["record_id"],
            user_id=doc["user_id"],
            statement=doc.get("statement", ""),
            fact_type=doc.get("fact_type"),
            scope=doc.get("scope", "user"),
            structured=doc.get("structured"),
            source_session=prov.get("source_session", ""),
            source_trace=prov.get("source_trace", ""),
            evidence_refs=tuple(prov.get("evidence_ref", []) or []),
            committed_at=doc.get("committed_at", ""),
        )

    @classmethod
    def from_candidate(cls, env: Any) -> UserKnowledgeRecord:
        """Project a `user_knowledge` `CandidateEnvelope` into a committable
        record. Reads only the Locked `user_knowledge` payload fields — tolerant
        of the fixture shape (`statement`/`scope`/`user_id`) plus the optional
        `fact_type`/`structured`."""
        payload = env.payload
        user_id = payload.get("user_id", "")
        return cls(
            record_id=mint_record_id(user_id, env.candidate_id),
            user_id=user_id,
            statement=payload.get("statement", ""),
            fact_type=payload.get("fact_type"),
            scope=payload.get("scope", "user"),
            structured=payload.get("structured"),
            source_session=env.source_session,
            source_trace=env.source_trace,
            evidence_refs=env.evidence_refs,
            committed_at=_now(),
        )
