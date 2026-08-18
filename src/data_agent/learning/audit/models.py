"""EvidenceSnapshot — the entity-bearing audit record (D51/D95, §4.2).

This is the ONE place entity-bearing quotes live: the dedicated, access-controlled,
TTL-retained `learning_audit` bucket. The entity-FREE global stores carry ONLY the
`evidence_ref` (the KV key), never the snapshot, so they stay entity-free (D17) while audit
stays durable (D51).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EvidenceSnapshot:
    evidence_ref: str  # evidence::<session_id>::<uuid4> — the KV key
    session_id: str
    trace_id: str
    turn_ref: int  # SessionSummary turn index
    tool_call_ref: str  # D46 tool_call_id
    quote: str  # the entity-bearing turn/tool-call quote (D51)
    snapshotted_at: str  # ISO-8601

    def to_doc(self) -> dict[str, Any]:
        return {
            "evidence_ref": self.evidence_ref,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "turn_ref": self.turn_ref,
            "tool_call_ref": self.tool_call_ref,
            "quote": self.quote,
            "snapshotted_at": self.snapshotted_at,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> EvidenceSnapshot:
        return cls(
            evidence_ref=doc["evidence_ref"],
            session_id=doc["session_id"],
            trace_id=doc["trace_id"],
            turn_ref=int(doc["turn_ref"]),
            tool_call_ref=doc["tool_call_ref"],
            quote=doc["quote"],
            snapshotted_at=doc["snapshotted_at"],
        )
