"""Learning-loop models — the D30 reference envelope, the D96 state machine,
and the D96 `content_hash` idempotency key.

Nothing here touches request-path data beyond READING a `SessionDoc`: the
envelope is a *reference* (D30 — session id + hashes, never the transcript), and
`compute_content_hash` is a pure function of the transcript-identifying subset of
the doc (§5), used to make enqueue and consume idempotent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from data_agent.runtime.session.models import SessionDoc


class LearningStatus:
    """The six `learning_status` states (D96 §3). `active` is the default already
    carried by `SessionDoc`; `done`/`dead_letter` are terminal."""

    ACTIVE = "active"
    PENDING = "pending"
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    DEAD_LETTER = "dead_letter"


# Legal forward transitions (design §3 ownership table). `dead_letter` is
# reachable from `queued` or `processing` (a poison job can die at either point —
# transition #5) and ALSO from `pending`: if a sweeper crashes AFTER XADD but
# BEFORE the `pending → queued` CAS, the message exists on the stream while the
# session is still `pending`; the consumer can reclaim that message, exhaust N
# deliveries, and dead-letter a still-`pending` session (via the `assert_from=
# False` escape hatch). The terminal states have no successors.
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    LearningStatus.ACTIVE: frozenset({LearningStatus.PENDING}),
    LearningStatus.PENDING: frozenset({LearningStatus.QUEUED, LearningStatus.DEAD_LETTER}),
    LearningStatus.QUEUED: frozenset({LearningStatus.PROCESSING, LearningStatus.DEAD_LETTER}),
    LearningStatus.PROCESSING: frozenset({LearningStatus.DONE, LearningStatus.DEAD_LETTER}),
    LearningStatus.DONE: frozenset(),
    LearningStatus.DEAD_LETTER: frozenset(),
}

# The statuses the sweeper scans/claims (design §6): only sessions not yet
# handed to a consumer are eligible, so a completed/in-flight job is never
# re-enqueued unless its content actually changes (a later-slice concern).
SWEEPABLE_STATUSES: list[str] = [LearningStatus.ACTIVE, LearningStatus.PENDING]


def _canonical_json(obj: Any) -> str:
    """Canonical JSON per D96 §5: sorted keys, UTF-8, no insignificant
    whitespace. Deterministic across processes and Python runs."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_content_hash(doc: SessionDoc) -> str:
    """The D96 idempotency key: `sha256_hex(canonical_json({...}))` over EXACTLY
    the transcript-identifying subset of the session doc (design §5).

    Included: `session_id`, the ordered `(turn_index, role, content)` of every
    message, and the ordered `(turn_index, tool_call_id, tool_name,
    canonical_json(args), status, error_code)` of every tool-trail entry.

    Deliberately EXCLUDED (so the hash is stable across the lifecycle
    transitions themselves and independent of storage/timestamp noise):
    `learning_status` (circular — it changes as the machine advances),
    every timestamp (`created_at`/`last_activity`/per-entry `ts`),
    `result_full_ref` (a storage pointer), `result_preview` (a derived view),
    `pause_checkpoint`, `context_summary_cache`, and all `provenance` sets (a
    re-parse artifact). What remains is the transcript the Slice-2 extractor
    mines (D46's full tool I/O trail).
    """
    payload = {
        "session_id": doc.session_id,
        "messages": [[m.turn_index, m.role, m.content] for m in doc.messages],
        "tool_trail": [
            [
                e.turn_index,
                e.tool_call_id,
                e.tool_name,
                _canonical_json(e.args),
                e.status,
                e.error_code,
            ]
            for e in doc.tool_trail
        ],
    }
    canonical = _canonical_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LearningJob:
    """The D30 reference envelope carried on the stream — session id + hashes +
    a best-effort CAS snapshot, NEVER the transcript and NEVER the raw JWT or
    `column_scope` (only a `scope_ref` id/hash travels — D25/D30).

    `cas` is advisory (design §3): it is a snapshot for the Slice-2 loader's
    optimistic read and is stale the instant transition #2 bumps the doc; the
    authoritative single-writer mechanism is the consumer's own fresh CAS at
    transition #3.
    """

    session_id: str
    couchbase_doc_id: str
    content_hash: str
    cas: str | None = None
    user_id: str | None = None
    scope_ref: str | None = None
    trace_id: str | None = None
    session_closed_at: str | None = None
    # W3C `traceparent` of the SWEEPER's `learning.enqueue` span (the per-session
    # trace ROOT). Injected at enqueue and extracted at consume so the consumer's
    # `learning.consume`/`triage`/`extract` spans join the SAME Phoenix trace as the
    # enqueue that produced them (cross-process span chaining). Distinct from
    # `trace_id` (a bare request-path id): this is the D25-shape trace-context
    # carrier, not a session grouping key. Optional — a job enqueued WITHOUT a tracer
    # carries `None`, and the consumer then starts a normal root span (fail-open).
    traceparent: str | None = None

    @classmethod
    def from_doc(
        cls,
        doc: SessionDoc,
        *,
        content_hash: str,
        cas: Any = None,
        user_id: str | None = None,
        scope_ref: str | None = None,
        trace_id: str | None = None,
        traceparent: str | None = None,
    ) -> LearningJob:
        """Build the reference envelope from a `SessionDoc`. Slice 1's session
        doc carries no user/scope/trace identity, so those default to `None`;
        they are threaded through by later slices without changing the transport.
        """
        return cls(
            session_id=doc.session_id,
            couchbase_doc_id=f"session::{doc.session_id}",
            content_hash=content_hash,
            cas=None if cas is None else str(cas),
            user_id=user_id,
            scope_ref=scope_ref,
            trace_id=trace_id,
            session_closed_at=doc.last_activity,
            traceparent=traceparent,
        )

    # --- flat string (de)serialization for a Redis Streams entry (D30). The
    # SAME encoding is used by the in-memory fake so both queues carry an
    # identical wire shape. `None` optionals are dropped (absent key) rather
    # than encoded as "", so a round-trip preserves `None` vs empty-string. ---

    def to_fields(self) -> dict[str, str]:
        fields: dict[str, str] = {
            "session_id": self.session_id,
            "couchbase_doc_id": self.couchbase_doc_id,
            "content_hash": self.content_hash,
        }
        for key in ("cas", "user_id", "scope_ref", "trace_id", "session_closed_at", "traceparent"):
            value = getattr(self, key)
            if value is not None:
                fields[key] = value
        return fields

    @classmethod
    def from_fields(cls, fields: dict[str, str]) -> LearningJob:
        return cls(
            session_id=fields["session_id"],
            couchbase_doc_id=fields["couchbase_doc_id"],
            content_hash=fields["content_hash"],
            cas=fields.get("cas"),
            user_id=fields.get("user_id"),
            scope_ref=fields.get("scope_ref"),
            trace_id=fields.get("trace_id"),
            session_closed_at=fields.get("session_closed_at"),
            traceparent=fields.get("traceparent"),
        )
