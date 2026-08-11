"""AuditStore — the `learning_audit` KV port (D95, §4.2; + the judge's record, §3b).

Mirrors the `SessionStore` port pattern: a Protocol + a real Couchbase impl
(`couchbase_audit_store`) + an in-memory fake (`memory_audit_store`). S2 stands the
client up and Layer-2-validates it, but writes NO evidence (§4.3) — the first
`snapshot` call is S3's.

**Two record families now, and the second one is not KV-only.** Evidence snapshots are
read by `evidence_ref` and never scanned, which is why this store was provisioned with
no GSI. The coverage judge's `JudgeRecord` (plan §3b) is written the same way — a KV
upsert under a deterministic key, so the redelivery read-through costs one `get` — but
its POINT is the aggregate: "how many candidates did we drop last quarter, and do the
verdicts skew to `existing-plus-delta`?" That is a N1QL question, and answering it needs
`query_select` plus an index on `(record_type, judged_at)` — both added to
`scripts/learning-audit-init.sh`. The aggregate is deliberately NOT a method on this
port: no code path in the loop reads it, only a human with `cbq` or a dashboard does,
and a port method nothing calls is a method nothing tests.

`record_judgement` and `read_judgement` are additive. Every existing implementation of
this Protocol that does not have them keeps working, because the judge is optional and
the consumer never calls them without one wired — but any store handed to a WIRED judge
must implement both, and the factory's fail-fast is what enforces that at composition.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from .judgement import JudgeRecord
from .models import EvidenceSnapshot


def mint_evidence_ref(session_id: str) -> str:
    """Pure key mint (no I/O): `evidence::<session_id>::<uuid4>`.

    The ref is non-entity-bearing (a session id is non-PII per D25 + an opaque
    uuid), so it is safe to carry on the entity-free global candidate (D95). S3
    can mint → attach → snapshot deterministically because minting is side-effect
    free."""
    return f"evidence::{session_id}::{uuid.uuid4()}"


class AuditStore(Protocol):
    def mint_evidence_ref(self, session_id: str) -> str:
        """Mint a fresh `evidence_ref` KV key (pure — no I/O)."""
        ...

    async def snapshot(self, ref: str, snapshot: EvidenceSnapshot) -> None:
        """KV-upsert *snapshot* at *ref* with the audit TTL (retention is the
        audit clock, set fresh on every write — §4.2)."""
        ...

    async def read(self, ref: str) -> EvidenceSnapshot | None:
        """Read the snapshot at *ref* (for the S7 review UI), or `None` if
        absent/expired."""
        ...

    async def record_judgement(self, record: JudgeRecord) -> None:
        """KV-upsert *record* at `record.judgement_ref`, with the JUDGEMENT retention.

        MUST RAISE on failure. This is the one write in the learning plane whose
        failure has to be visible to its caller, and the reason is the drop: the judge
        writes the record BEFORE it cancels an extraction, and cancels only if the
        write returned. A record write that failed silently would produce exactly the
        outcome the mitigation exists to prevent — work discarded with nothing to
        show for it. Every other audit write in this loop is fire-and-forget; this one
        is not.

        **THE GUARANTEE IS AN ACK, NOT DURABILITY, and the precondition is only as
        strong as that.** A plain Couchbase KV upsert returns once the managed cache
        has the mutation; persistence to disk and replication to a replica are
        asynchronous. A node failure shortly after the ack loses the record AFTER the
        drop was taken — the same invisible loss, reopened one layer down. Closing it
        properly means a durability level (`ServerDurability(MAJORITY)`), which costs
        latency on every judgement and is not exercisable on the single-node dev
        cluster, so it is deliberately NOT set today and is named here rather than
        implied away. Read the precondition as "the store accepted it", not "the store
        kept it".
        """
        ...

    async def read_judgement(self, ref: str) -> JudgeRecord | None:
        """Read the judgement at *ref*, or `None` if absent/expired/unrecognized.

        The redelivery read-through: a re-processed session finds its prior verdict
        here instead of paying for a second, non-idempotent model call.

        THREE things collapse to `None`, all meaning "no verdict on file, judge it":
        the document is absent, it has expired, or `JudgeRecord.from_doc` refused it
        (a stored verdict outside the vocabulary — see there for why that is a refusal
        and not a coercion). An EXCEPTION means "could not look", which the judge also
        treats as `None`. Never as a drop — a failed read is indistinguishable from an
        absence, and the direction that fails safe is doing the work again.
        """
        ...
