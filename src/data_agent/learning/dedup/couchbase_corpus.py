"""CouchbaseBlueprintCorpus — the DURABLE `BlueprintCorpus` (Wave 3b-(i), D48).

Its OWN `Cluster`, authenticated as `learning_corpus_writer` against the dedicated
`learning_corpus` bucket — a separate RBAC boundary from the session/audit/candidate stores.
It also duck-types the S9 `HitCountReader`/`RecurrenceCountReader` ports, so the scheduler
and the dedup stage read one source of truth.

CORRECTNESS CRUX (the whole reason this store is durable, not in-memory):
`increment_hit_count` is ATOMIC SERVER-SIDE — a sub-document counter, NEVER a
read-modify-write — so two concurrent sessions on the same hard key converge to +2 instead
of racing and losing an update; and `seed_artifact` is INSERT-WINS idempotent, because an
`upsert` would clobber an already-accrued `hit_count`. Both treat a vanished doc as a
tolerated no-op (D52).

Import-guarded (the module imports without the `couchbase` SDK; constructing the store
raises), gated per public coroutine by `CouchbaseConnectGate`, and built through
`CouchbaseStoreBase` so `__init__` does no I/O.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.couchbase_connect import (
    CouchbaseStoreBase,
    get_or_none,
)

from ..config import LearningSettings
from .corpus import CorpusArtifact

# The availability flag + the shared SDK symbols live in `couchbase_connect`; these
# are the extra types only this store writes with.
try:  # pragma: no cover - exercised only when the couchbase SDK is installed
    import couchbase.subdocument as subdoc
    from couchbase.exceptions import DocumentExistsException, DocumentNotFoundException
    from couchbase.options import InsertOptions, QueryOptions
except ImportError:  # pragma: no cover
    pass


def _doc_id(canonical_key: str) -> str:
    """The KV doc id for an artifact — namespaced so the corpus bucket can be
    inspected/co-located cleanly. `canonical_key` is a `sha256:`-prefixed digest
    (well under Couchbase's 250-byte id limit)."""
    return f"corpus::{canonical_key}"


def _to_doc(artifact: CorpusArtifact) -> dict[str, Any]:
    """The persisted document for a landed artifact.

    `hit_count` is stored as a plain integer so the sub-document counter can atomically
    increment it. Docs written before `status`/`source` existed carry neither, and
    `CorpusArtifact.from_doc` defaults them, so no migration is needed.
    """
    return {
        "id": artifact.id,
        "canonical_key": artifact.canonical_key,
        "intent": artifact.intent,
        "hit_count": int(artifact.hit_count),
        "uses_rules": list(artifact.uses_rules),
        "status": artifact.status,
        "source": artifact.source,
        # Written from plan §4 on, as a plain integer for the same reason `hit_count` is:
        # the sub-document counter below increments it server-side.
        "recurrence_count": int(artifact.recurrence_count),
    }


class CouchbaseBlueprintCorpus(CouchbaseStoreBase):
    """Real `BlueprintCorpus` backed by the dedicated `learning_corpus` bucket.

    Also duck-types the S9 `HitCountReader` port, so the promotion scheduler reads the SAME
    durable artifacts the S6 stage seeds and increments — one source of truth for the
    cross-session count.
    """

    def __init__(self, settings: LearningSettings, cluster: Any = None) -> None:
        self._settings = settings
        # KV-only default collection = the base's default `_bind_collections`. A
        # non-positive TTL means NO expiry here (`ttl_none_when_not_positive`), which
        # `seed_artifact` turns into `InsertOptions()` with no `expiry=`.
        self._init_couchbase_store(
            cluster=cluster,
            connection_string=settings.learning_corpus_connection_string,
            username=settings.learning_corpus_username,
            password=settings.learning_corpus_password,
            bucket=settings.learning_corpus_bucket,
            ttl_seconds=settings.learning_corpus_ttl_seconds,
            ttl_none_when_not_positive=True,
        )

    async def get_by_canonical_key(self, canonical_key: str) -> CorpusArtifact | None:
        await self._ensure_connected()
        result = await get_or_none(self._collection, _doc_id(canonical_key))
        return None if result is None else CorpusArtifact.from_doc(result.content_as[dict])

    async def seed_artifact(self, artifact: CorpusArtifact) -> None:
        await self._ensure_connected()
        # INSERT-WINS idempotency (D48): `insert` fails on an existing key, so two
        # concurrent first-sightings yield EXACTLY one create; a later attempt is a
        # tolerated no-op. NEVER `upsert` — that would reset an accrued `hit_count`.
        options = InsertOptions(expiry=self._ttl) if self._ttl is not None else InsertOptions()
        try:
            await self._collection.insert(_doc_id(artifact.canonical_key), _to_doc(artifact), options)
        except DocumentExistsException:
            return

    async def increment_hit_count(self, canonical_key: str) -> None:
        await self._ensure_connected()
        # ATOMIC server-side increment (D48): a sub-document counter mutation, NOT a
        # read-modify-write. Concurrent hits each apply a server-side +1 with no lost
        # updates. A vanished doc (concurrent retire) → DocumentNotFoundException,
        # swallowed as a tolerated no-op (fail-soft, D52) — never a crash.
        try:
            await self._collection.mutate_in(
                _doc_id(canonical_key), [subdoc.increment("hit_count", 1)]
            )
        except DocumentNotFoundException:
            return

    async def increment_recurrence_count(self, canonical_key: str) -> None:
        await self._ensure_connected()
        # The SOFT paraphrase counter (plan §4). Atomic server-side, exactly like
        # `increment_hit_count`, and for the same reason: several concurrent sessions can
        # be near the same artifact and a read-modify-write would lose updates.
        #
        # `create_parents=True` is LOAD-BEARING here in a way it is not for `hit_count`.
        # Every artifact seeded before this slice has NO `recurrence_count` path at all
        # and the corpus bucket is durable and never migrated, so a counter mutation
        # against a legacy document would otherwise fail on a missing path. With the flag
        # the path is created and initialized to the delta, which is the correct starting
        # value for "this is the first soft sighting we have recorded".
        #
        # NOT verified against a live Couchbase in this slice — the unit suite drives a
        # spec-recording double, so what is proven here is that the right spec is issued,
        # not that the server honours it on a legacy doc. A failure would be a lost soft
        # count on pre-slice artifacts only, on a counter that is weighted 0.0 today.
        try:
            await self._collection.mutate_in(
                _doc_id(canonical_key),
                [subdoc.increment("recurrence_count", 1, create_parents=True)],
            )
        except DocumentNotFoundException:
            return

    async def set_status(self, canonical_key: str, status: str) -> None:
        await self._ensure_connected()
        # NARROW sub-document write on the ONE field (PriorArtIndex Slice 2), for the
        # same three reasons `CandidateStore.stamp_drift` is one: a full-document upsert
        # would (a) clobber a `hit_count` another worker incremented between our read and
        # our write, (b) renew the TTL, and (c) RESURRECT a document something else
        # removed. `upsert` here is the SUB-DOCUMENT upsert (create-or-replace the
        # `status` path), not a document upsert — a missing DOCUMENT still raises
        # `DocumentNotFoundException`, which is swallowed as a tolerated no-op
        # (fail-soft, D52) exactly like `increment_hit_count`'s.
        try:
            await self._collection.mutate_in(
                _doc_id(canonical_key), [subdoc.upsert("status", status)]
            )
        except DocumentNotFoundException:
            return

    async def list_artifacts(self) -> list[CorpusArtifact]:
        await self._ensure_connected()
        # Default (NOT_BOUNDED) query consistency: a just-seeded artifact may not yet
        # be visible to this scan — fine for the S6 soft near-miss layer (a missed
        # near-duplicate degrades to `insert`, the fail-soft direction, D52).
        #
        # This is a full O(corpus) scan and the caller embeds EVERY row's intent, so one
        # candidate costs N+1 embeddings. It is no longer the primary path: with a
        # `PriorArtIndex` wired, `DedupStage` gets the same answer from ONE
        # approximate-nearest-neighbour call against the neo4j vector index. This
        # remains as the FAIL-OPEN fallback for a deployment with no graph configured
        # (or a graph that is transiently unreachable) — the loop must keep learning
        # when a read fails, and degraded-but-running beats stopped.
        statement = f"SELECT c.* FROM `{self._bucket_name}` c"
        result = self._cluster.query(statement, QueryOptions())
        out: list[CorpusArtifact] = []
        async for row in result:
            out.append(CorpusArtifact.from_doc(row))
        return out

    async def hit_count(self, canonical_key: str) -> int:
        """The S9 `HitCountReader` port: the artifact's cross-session count, or 0 when unkeyed."""
        await self._ensure_connected()
        artifact = await self.get_by_canonical_key(canonical_key)
        return artifact.hit_count if artifact is not None else 0

    async def recurrence_count(self, canonical_key: str) -> int:
        """The S9 `RecurrenceCountReader` port (plan §4): the artifact's SOFT paraphrase count, or 0.

        Duck-typed onto this store for the same reason `hit_count` is — the scheduler's corroboration
        gate weighs both counts, and they must come from the ONE set of artifacts the dedup stage
        writes rather than from two stores that could diverge.
        """
        await self._ensure_connected()
        artifact = await self.get_by_canonical_key(canonical_key)
        return artifact.recurrence_count if artifact is not None else 0
