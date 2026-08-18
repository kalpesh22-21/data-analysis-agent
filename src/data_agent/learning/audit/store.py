"""AuditStore — the `learning_audit` KV port (D95, §4.2; + the judge's record, §3b).

Mirrors the `SessionStore` port pattern: a Protocol, a Couchbase impl and an in-memory fake.

TWO RECORD FAMILIES, and the second is not KV-only. Evidence snapshots are read by
`evidence_ref` and never scanned, which is why this store was provisioned with no GSI. The
judge's `JudgeRecord` is written the same way — a KV upsert under a deterministic key, so the
redelivery read-through costs one `get` — but its POINT is the aggregate, which is a N1QL
question needing `query_select` plus an index on `(record_type, judged_at)`. That aggregate is
deliberately NOT a method on this port: no code path reads it, only a human or a dashboard
does, and a port method nothing calls is a method nothing tests.

`record_judgement`/`read_judgement` are additive — existing implementations without them keep
working, because the judge is optional — but any store handed to a WIRED judge must implement
both, which the factory's fail-fast enforces at composition.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from .judgement import JudgeRecord
from .models import EvidenceSnapshot


def mint_evidence_ref(session_id: str) -> str:
    """Pure key mint (no I/O): `evidence::<session_id>::<uuid4>`.

    The ref is non-entity-bearing (a session id is non-PII per D25, plus an opaque uuid), so it is
    safe to carry on the entity-free global candidate. Minting is side-effect free, so S3 can
    mint → attach → snapshot deterministically.
    """
    return f"evidence::{session_id}::{uuid.uuid4()}"


class AuditStore(Protocol):
    def mint_evidence_ref(self, session_id: str) -> str:
        """Mint a fresh `evidence_ref` KV key (pure — no I/O)."""
        ...

    async def snapshot(self, ref: str, snapshot: EvidenceSnapshot) -> None:
        """KV-upsert *snapshot* at *ref* with the audit TTL.

        Retention is the audit clock, set fresh on every write (§4.2).
        """
        ...

    async def read(self, ref: str) -> EvidenceSnapshot | None:
        """Read the snapshot at *ref* (for the S7 review UI), or `None` if
        absent/expired."""
        ...

    async def record_judgement(self, record: JudgeRecord) -> None:
        """KV-upsert *record* at `record.judgement_ref`, with the JUDGEMENT retention.

        MUST RAISE on failure. This is the one write in the learning plane whose failure has to be
        visible to its caller: the judge writes the record BEFORE it cancels an extraction and cancels
        only if the write returned. Every other audit write in this loop is fire-and-forget.

        THE GUARANTEE IS AN ACK, NOT DURABILITY. A plain KV upsert returns once the managed cache has
        the mutation; persistence and replication are asynchronous, so a node failure shortly after
        the ack loses the record AFTER the drop was taken. Closing that properly means a durability
        level, which costs latency on every judgement and is not exercisable on a single-node dev
        cluster, so it is deliberately NOT set today. Read the precondition as "the store accepted
        it", not "the store kept it".
        """
        ...

    async def read_judgement(self, ref: str) -> JudgeRecord | None:
        """Read the judgement at *ref*, or `None` if absent, expired or unrecognized.

        The redelivery read-through: a re-processed session finds its prior verdict here instead of
        paying for a second, non-idempotent model call. THREE things collapse to `None`, all meaning
        "no verdict on file, judge it" — absent, expired, or refused by `JudgeRecord.from_doc`. An
        EXCEPTION means "could not look", which the judge also treats as `None`, never as a drop: a
        failed read is indistinguishable from an absence, and the direction that fails safe is doing
        the work again.
        """
        ...
