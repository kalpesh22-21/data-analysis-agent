"""CouchbaseCandidateStore — the real `CandidateStore` (D101).

Its OWN `Cluster`, authenticated as `learning_candidates_writer` against the
dedicated `learning_candidates` bucket (`_default._default`) — a separate RBAC
boundary from the session/audit stores (D101, mirroring D95). `put` is a KV
upsert with the candidate TTL; `list_by_status` is a parameterized N1QL query
(needs the primary index provisioned by `scripts/learning-candidates-init.sh`);
`touch_scanned` / `stamp_drift` are TTL-preserving sub-document writes of the two
S9-owned bookkeeping fields.

Import-guarded exactly like `couchbase_store` / `couchbase_audit_store`: imports
with or without the SDK; constructing without it raises.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from ..config import LearningSettings
from .models import CandidateEnvelope, CandidateStatus
from .verdicts import DriftStamp

# Terminal lifecycle states persist INDEFINITELY (ui-inbox-type-archive contract
# §Retention): a rejected row is a durable D29 negative-training signal + the Archived
# reviewer view, validated/retired are settled records, and PROMOTED must survive so the
# Phase-3 idempotent re-emit (regenerate the MCP YAML for a lost/abandoned PR) always has
# its candidate — none may be TTL-evicted. Every other (transient) status keeps the TTL.
_TERMINAL_STATUSES = frozenset(
    {
        CandidateStatus.REJECTED,
        CandidateStatus.VALIDATED,
        CandidateStatus.RETIRED,
        CandidateStatus.PROMOTED,
    }
)

# The two sort keys `list_by_status` will ORDER BY, mapped from the caller's
# `order_by` Literal to a HARDCODED field name. The N1QL statement is built with an
# f-string (a sort key cannot be a named parameter), so the value that reaches the
# f-string must never be caller-controlled text: this dict is the allow-list, and an
# unknown key raises rather than interpolating. Same posture as the `order` direction.
_SORT_KEYS: dict[str, str] = {
    "created_at": "created_at",
    "last_scanned_at": "last_scanned_at",
}

try:  # pragma: no cover - exercised only when the couchbase SDK is installed
    from acouchbase.cluster import Cluster
    from couchbase.auth import PasswordAuthenticator
    from couchbase.exceptions import DocumentNotFoundException
    from couchbase.options import (
        ClusterOptions,
        GetOptions,
        MutateInOptions,
        QueryOptions,
        UpsertOptions,
    )
    from couchbase.subdocument import upsert as sd_upsert

    COUCHBASE_AVAILABLE = True
except ImportError:  # pragma: no cover
    COUCHBASE_AVAILABLE = False


class CouchbaseCandidateStore:
    """Real `CandidateStore` backed by the dedicated `learning_candidates` bucket."""

    def __init__(self, settings: LearningSettings, cluster: Any = None) -> None:
        if not COUCHBASE_AVAILABLE:
            raise RuntimeError(
                "The 'couchbase' package is not installed. "
                "Install it (see pyproject.toml) to use CouchbaseCandidateStore."
            )
        self._settings = settings
        self._cluster = cluster or Cluster(
            settings.learning_candidates_connection_string,
            ClusterOptions(
                PasswordAuthenticator(
                    settings.learning_candidates_username,
                    settings.learning_candidates_password,
                )
            ),
        )
        self._bucket_name = settings.learning_candidates_bucket
        bucket = self._cluster.bucket(self._bucket_name)
        self._collection = bucket.default_collection()
        self._ttl = timedelta(seconds=settings.learning_candidates_ttl_seconds)

    async def put(self, envelope: CandidateEnvelope) -> None:
        # Terminal rows persist with NO TTL (expiry=0); transient rows keep the
        # candidate TTL (ui-inbox-type-archive contract §Retention).
        expiry = (
            timedelta(0)
            if envelope.status in _TERMINAL_STATUSES
            else self._ttl
        )
        await self._collection.upsert(
            envelope.candidate_id, envelope.to_doc(), UpsertOptions(expiry=expiry)
        )

    async def get(self, candidate_id: str) -> CandidateEnvelope | None:
        try:
            result = await self._collection.get(candidate_id, GetOptions())
        except DocumentNotFoundException:
            return None
        return CandidateEnvelope.from_doc(result.content_as[dict])

    async def list_by_status(
        self,
        status: str,
        *,
        limit: int = 100,
        order: Literal["asc", "desc"] = "asc",
        order_by: Literal["created_at", "last_scanned_at"] = "created_at",
    ) -> list[CandidateEnvelope]:
        # `created_at` ASC (default) is the small self-draining review queue; DESC
        # (newest-first) is the durable rejected archive so LIMIT trims OLD history,
        # not present rejects. `last_scanned_at` ASC is the S9 cron's ROTATION read:
        # least-recently-examined first, and — because a never-scanned candidate omits
        # the key entirely (`to_doc` emits it only when set) — MISSING sorts FIRST in
        # the N1QL total collation order (MISSING < NULL < FALSE < TRUE < number <
        # string < array < object < binary), so brand-new work is picked up on the very
        # next cycle instead of queueing behind everything already examined.
        #
        # DELIBERATELY NO freshness predicate in the WHERE. Ordering alone spends the
        # LIMIT on the most-overdue rows (nothing is fetched-then-discarded in Python),
        # and a cutoff comparison would have to compare ISO-8601 timestamps as STRINGS.
        # That is only sound when every writer emits the identical offset format; one
        # row stamped `+05:30` (or `Z`, or without microseconds) by any other clock
        # would compare wrong and could be excluded FOREVER — re-creating, silently,
        # the exact permanent starvation this ordering exists to fix. A mis-formatted
        # stamp under ORDER BY is merely sorted early or late; it is never dropped.
        direction = "DESC" if order == "desc" else "ASC"
        sort_key = _SORT_KEYS[order_by]  # allow-listed; never caller text (see above)
        statement = (
            f"SELECT c.* FROM `{self._bucket_name}` c "
            "WHERE c.status = $status "
            f"ORDER BY c.{sort_key} {direction} LIMIT $limit"
        )
        result = self._cluster.query(
            statement,
            QueryOptions(named_parameters={"status": status, "limit": int(limit)}),
        )
        out: list[CandidateEnvelope] = []
        async for row in result:
            out.append(CandidateEnvelope.from_doc(row))
        return out

    async def touch_scanned(self, candidate_id: str, at: str) -> None:
        """Sub-document write of the S9 scan cursor — see `CandidateStore.touch_scanned`.

        `mutate_in` (not `upsert`) is what makes the two guarantees real: it writes the
        ONE `last_scanned_at` path server-side, so it can never revert a concurrent
        inbox transition the way re-putting a stale scanned envelope would, and
        `preserve_expiry=True` keeps the document's existing TTL, so stamping a
        permanently-held candidate every cycle does not renew its 90-day retention
        clock into immortality (`put` deliberately sets the TTL fresh per write; this
        write is bookkeeping, not a lifecycle event, and must not restart that clock).

        A document that expired or was superseded between the scan read and this write
        is a tolerated no-op — there is nothing left to rotate."""
        await self._stamp_path(candidate_id, "last_scanned_at", at)

    async def stamp_drift(self, candidate_id: str, drift: DriftStamp) -> None:
        """Sub-document write of the S9 drift verdict — see `CandidateStore.stamp_drift`.

        Same mechanism as `touch_scanned`, for the same reasons plus one specific to this
        field: a full-envelope `put` of a cycle-start snapshot would RESURRECT a document
        that `supersede` deleted in between (a redelivered session re-extracting), because
        `put` is an upsert. `mutate_in` defaults to REPLACE semantics, so a missing
        document raises `DocumentNotFoundException` and is swallowed as a no-op — the
        deleted candidate stays deleted."""
        await self._stamp_path(candidate_id, "drift", drift.to_doc())

    async def _stamp_path(self, candidate_id: str, path: str, value: Any) -> None:
        """One TTL-preserving sub-document upsert of a single S9-owned path.

        `preserve_expiry=True` is load-bearing, not a nicety: `put` deliberately sets the
        candidate TTL fresh on every write, so bookkeeping stamps issued on a schedule
        would renew a parked candidate's 90-day retention clock indefinitely and make it
        immortal. Bookkeeping must not restart the retention clock.

        A document that expired, or that `supersede` removed between the scan read and
        this write, is a tolerated no-op — never a resurrection, never an error."""
        try:
            await self._collection.mutate_in(
                candidate_id,
                [sd_upsert(path, value)],
                MutateInOptions(preserve_expiry=True),
            )
        except DocumentNotFoundException:
            return

    async def supersede(self, content_hash: str) -> None:
        # N1QL-SELECT the stale candidate ids (query_select), then KV-remove each
        # (data_writer) — avoids needing query_delete on the writer role. A doc
        # already gone (concurrent removal) is a tolerated no-op.
        statement = (
            f"SELECT META(c).id AS id FROM `{self._bucket_name}` c "
            "WHERE c.content_hash = $content_hash"
        )
        result = self._cluster.query(
            statement, QueryOptions(named_parameters={"content_hash": content_hash})
        )
        ids = [row["id"] async for row in result]
        for candidate_id in ids:
            try:
                await self._collection.remove(candidate_id)
            except DocumentNotFoundException:
                continue
