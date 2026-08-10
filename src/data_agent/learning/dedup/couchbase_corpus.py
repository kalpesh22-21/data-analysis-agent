"""CouchbaseBlueprintCorpus — the DURABLE `BlueprintCorpus` (Wave 3b-(i), D48).

Its OWN `Cluster`, authenticated as `learning_corpus_writer` against the dedicated
`learning_corpus` bucket (`_default._default`) — a separate RBAC boundary from the
session/audit/candidate stores (mirroring D95/D101). Holds the landed blueprint
artifacts the S6 dedup hard key looks up, keyed by `canonical_key`; the S9 promotion
scheduler reads the same artifacts' `hit_count` (this store also duck-types the
`HitCountReader` port, §11).

**Correctness crux (D48 — the whole reason this store is durable, not in-memory):**

  * `increment_hit_count` is ATOMIC SERVER-SIDE — a sub-document counter
    (`mutate_in` + `SD.increment`), NEVER a read-modify-write. Two concurrent
    sessions each hitting the same hard key both apply a server-side +1, so the
    count converges to +2; a read-modify-write would race and lose an update,
    breaking the D48 "one create + one increment" invariant that feeds the T=3
    promotion threshold. A hit on a vanished doc (concurrent retire) is a tolerated
    no-op (`DocumentNotFoundException` swallowed), never a crash (fail-soft, D52).

  * `seed_artifact` is INSERT-WINS idempotent — `insert` + catch
    `DocumentExistsException` → no-op. Two concurrent first-sightings of the same
    key therefore produce EXACTLY one create; an `upsert` would clobber an
    already-accrued `hit_count` (a later insert-attempt must NOT reset the count),
    so `insert` is load-bearing, not a style choice.

`get_by_canonical_key` is a KV get by doc id; `list_artifacts` is an N1QL scan
(the soft layer embeds each artifact's `intent`).

Import-guarded exactly like `couchbase_candidate_store` / `couchbase_audit_store`:
the module imports with or without the `couchbase` SDK (so the unit suite stays
green with zero infra); constructing the store without the SDK raises.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..config import LearningSettings
from .corpus import CorpusArtifact

try:  # pragma: no cover - exercised only when the couchbase SDK is installed
    import couchbase.subdocument as subdoc
    from acouchbase.cluster import Cluster
    from couchbase.auth import PasswordAuthenticator
    from couchbase.exceptions import DocumentExistsException, DocumentNotFoundException
    from couchbase.options import (
        ClusterOptions,
        GetOptions,
        InsertOptions,
        QueryOptions,
    )

    COUCHBASE_AVAILABLE = True
except ImportError:  # pragma: no cover
    COUCHBASE_AVAILABLE = False


def _doc_id(canonical_key: str) -> str:
    """The KV doc id for an artifact — namespaced so the corpus bucket can be
    inspected/co-located cleanly. `canonical_key` is a `sha256:`-prefixed digest
    (well under Couchbase's 250-byte id limit)."""
    return f"corpus::{canonical_key}"


def _to_doc(artifact: CorpusArtifact) -> dict[str, Any]:
    """The persisted document for a landed artifact. `hit_count` is stored as a
    plain integer so the sub-document counter can atomically increment it.

    `status`/`source` are written from this slice on (PriorArtIndex Slice 1). Docs
    already in the bucket carry neither; `CorpusArtifact.from_doc` defaults them to the
    values those docs have always implicitly had, so no migration is needed and none of
    the sub-document mutations below are affected (they address `hit_count` only)."""
    return {
        "id": artifact.id,
        "canonical_key": artifact.canonical_key,
        "intent": artifact.intent,
        "hit_count": int(artifact.hit_count),
        "uses_rules": list(artifact.uses_rules),
        "status": artifact.status,
        "source": artifact.source,
    }


class CouchbaseBlueprintCorpus:
    """Real `BlueprintCorpus` backed by the dedicated `learning_corpus` bucket.

    Also duck-types the S9 `HitCountReader` port (`hit_count(canonical_key) -> int`)
    so the promotion scheduler reads the SAME durable artifacts the S6 stage seeds
    and increments — one source of truth for the cross-session count.
    """

    def __init__(self, settings: LearningSettings, cluster: Any = None) -> None:
        if not COUCHBASE_AVAILABLE:
            raise RuntimeError(
                "The 'couchbase' package is not installed. "
                "Install it (see pyproject.toml) to use CouchbaseBlueprintCorpus."
            )
        self._settings = settings
        self._cluster = cluster or Cluster(
            settings.learning_corpus_connection_string,
            ClusterOptions(
                PasswordAuthenticator(
                    settings.learning_corpus_username,
                    settings.learning_corpus_password,
                )
            ),
        )
        self._bucket_name = settings.learning_corpus_bucket
        bucket = self._cluster.bucket(self._bucket_name)
        self._collection = bucket.default_collection()
        self._ttl = (
            timedelta(seconds=settings.learning_corpus_ttl_seconds)
            if settings.learning_corpus_ttl_seconds > 0
            else None
        )

    async def get_by_canonical_key(self, canonical_key: str) -> CorpusArtifact | None:
        try:
            result = await self._collection.get(_doc_id(canonical_key), GetOptions())
        except DocumentNotFoundException:
            return None
        return CorpusArtifact.from_doc(result.content_as[dict])

    async def seed_artifact(self, artifact: CorpusArtifact) -> None:
        # INSERT-WINS idempotency (D48): `insert` fails on an existing key, so two
        # concurrent first-sightings yield EXACTLY one create; a later attempt is a
        # tolerated no-op. NEVER `upsert` — that would reset an accrued `hit_count`.
        options = InsertOptions(expiry=self._ttl) if self._ttl is not None else InsertOptions()
        try:
            await self._collection.insert(_doc_id(artifact.canonical_key), _to_doc(artifact), options)
        except DocumentExistsException:
            return

    async def increment_hit_count(self, canonical_key: str) -> None:
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

    async def set_status(self, canonical_key: str, status: str) -> None:
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
        """The S9 `HitCountReader` port: the landed artifact's cross-session
        `hit_count`, or 0 when no artifact is keyed here (nothing has accrued)."""
        artifact = await self.get_by_canonical_key(canonical_key)
        return artifact.hit_count if artifact is not None else 0
