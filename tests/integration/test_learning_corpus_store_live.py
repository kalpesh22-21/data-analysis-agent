"""Layer-2 integration — CouchbaseBlueprintCorpus against a LIVE Couchbase
`learning_corpus` bucket (Wave 3b-(i), D48).

The proofs that only real Couchbase can give (the whole reason this store is
durable, not in-memory):
  - ATOMIC increment: two CONCURRENT `increment_hit_count` calls on the same key
    land EXACTLY +2 (a server-side sub-document counter, no lost update) — a
    read-modify-write would race and lose one;
  - INSERT-WINS seed: two CONCURRENT `seed_artifact` first-sightings produce EXACTLY
    one create (`hit_count` stays 1), never a clobber;
  - fail-soft increment on a vanished key (no crash);
  - RBAC boundary (D48/D17): `learning_corpus_writer` can read/write its own bucket
    but is DENIED on `agent_sessions` AND `learning_candidates` (isolation).

Provision first (idempotent):
    docker compose -f docker-compose.integration.yml up -d --wait couchbase
    ./scripts/couchbase-init.sh && ./scripts/learning-candidates-init.sh \
        && ./scripts/learning-corpus-init.sh
Run:
    RUN_COUCHBASE_TESTS=1 \
    LEARNING_CORPUS_CONNECTION_STRING=couchbase://localhost \
    LEARNING_CORPUS_USERNAME=learning_corpus_writer \
    LEARNING_CORPUS_PASSWORD=corpus-writer-pass \
        uv run pytest tests/integration/test_learning_corpus_store_live.py -v
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from data_agent.learning.config import LearningSettings
from data_agent.learning.dedup.corpus import CorpusArtifact
from data_agent.learning.dedup.couchbase_corpus import _doc_id
from data_agent.runtime.couchbase_connect import COUCHBASE_AVAILABLE

pytestmark = pytest.mark.skipif(
    not COUCHBASE_AVAILABLE or not os.environ.get("RUN_COUCHBASE_TESTS"),
    reason="Requires the 'couchbase' package AND a live Couchbase with the "
    "learning_corpus bucket + RBAC user (run scripts/learning-corpus-init.sh; "
    "set RUN_COUCHBASE_TESTS=1).",
)


def _settings(**overrides) -> LearningSettings:
    return LearningSettings(_env_file=None, **overrides)


def _artifact(key: str, *, hit_count: int = 1) -> CorpusArtifact:
    return CorpusArtifact(
        id=f"candidate::{uuid.uuid4().hex[:8]}",
        canonical_key=key,
        intent="total earnings for a department in a year",
        hit_count=hit_count,
        uses_rules=("active_employee",),
    )


@pytest.fixture
async def corpus():
    from data_agent.learning.dedup.couchbase_corpus import CouchbaseBlueprintCorpus

    store = CouchbaseBlueprintCorpus(_settings())
    await store.connect()
    created: list[str] = []
    store._created = created  # type: ignore[attr-defined]
    yield store
    from couchbase.exceptions import DocumentNotFoundException

    for key in created:
        try:
            await store._collection.remove(_doc_id(key))
        except DocumentNotFoundException:
            pass


# --- atomic increment (the D48 correctness crux) ----------------------------


async def test_two_concurrent_increments_land_exactly_plus_two(corpus):
    """Two CONCURRENT increments on the same key ⇒ EXACTLY +2 (no lost update).
    A read-modify-write would race and land +1 — the whole reason this is a
    server-side sub-document counter."""
    key = f"sha256:live-inc-{uuid.uuid4().hex[:8]}"
    await corpus.seed_artifact(_artifact(key))  # hit_count = 1
    corpus._created.append(key)

    await asyncio.gather(
        corpus.increment_hit_count(key),
        corpus.increment_hit_count(key),
    )

    art = await corpus.get_by_canonical_key(key)
    assert art is not None
    assert art.hit_count == 3  # 1 seeded + 2 concurrent increments, none lost


async def test_increment_on_vanished_key_is_noop(corpus):
    # A hit adjudicated against a key with no artifact ⇒ tolerated no-op (D52).
    await corpus.increment_hit_count(f"sha256:vanished-{uuid.uuid4().hex[:8]}")


# --- insert-wins idempotent seed --------------------------------------------


async def test_two_concurrent_seeds_create_exactly_one(corpus):
    """Two CONCURRENT first-sightings of the same key ⇒ EXACTLY one create; the
    later insert loses (DocumentExists) and is a no-op — never a clobber."""
    key = f"sha256:live-seed-{uuid.uuid4().hex[:8]}"
    corpus._created.append(key)

    await asyncio.gather(
        corpus.seed_artifact(_artifact(key, hit_count=1)),
        corpus.seed_artifact(_artifact(key, hit_count=1)),
    )

    art = await corpus.get_by_canonical_key(key)
    assert art is not None
    assert art.hit_count == 1  # exactly one create; no double-seed, no clobber


async def test_seed_then_increment_then_seed_preserves_count(corpus):
    """A late seed after an increment must NOT reset the accrued count (insert-wins,
    never upsert) — the D48 'one create + one increment' invariant."""
    key = f"sha256:live-preserve-{uuid.uuid4().hex[:8]}"
    corpus._created.append(key)

    await corpus.seed_artifact(_artifact(key, hit_count=1))
    await corpus.increment_hit_count(key)  # → 2
    await corpus.seed_artifact(_artifact(key, hit_count=1))  # no-op (already exists)

    art = await corpus.get_by_canonical_key(key)
    assert art is not None
    assert art.hit_count == 2  # the increment survived the late seed


# --- terminal-status stamp (PriorArt Slice 2) --------------------------------


async def test_set_status_is_a_narrow_write_that_preserves_an_accrued_count(corpus):
    """The S9 reject/retract edges stamp the artifact so a declined idea stops surfacing
    as live prior art. It MUST be a sub-document write, not a document upsert: a full
    upsert would clobber a `hit_count` another worker incremented between the scan read
    and this write, and would renew the TTL. Only real Couchbase can show that."""
    key = f"sha256:live-status-{uuid.uuid4().hex[:8]}"
    corpus._created.append(key)
    await corpus.seed_artifact(_artifact(key, hit_count=1))
    await corpus.increment_hit_count(key)  # → 2

    await corpus.set_status(key, "rejected")

    art = await corpus.get_by_canonical_key(key)
    assert art is not None
    assert art.status == "rejected"
    assert art.is_terminal
    assert art.hit_count == 2  # the accrued counter survived the stamp
    assert art.intent == "total earnings for a department in a year"  # nothing else moved
    assert art.uses_rules == ("active_employee",)


async def test_set_status_is_idempotent_and_re_stampable(corpus):
    key = f"sha256:live-restatus-{uuid.uuid4().hex[:8]}"
    corpus._created.append(key)
    await corpus.seed_artifact(_artifact(key))

    await corpus.set_status(key, "rejected")
    await corpus.set_status(key, "retired")

    assert (await corpus.get_by_canonical_key(key)).status == "retired"


async def test_set_status_never_resurrects_a_vanished_artifact(corpus):
    """`mutate_in` has REPLACE semantics on the DOCUMENT: stamping an artifact that was
    removed raises `DocumentNotFoundException`, which is swallowed. A document upsert
    would CREATE a `rejected` artifact with no history — a phantom the promotion guard
    would then read a `hit_count` of 1 from."""
    key = f"sha256:live-gone-{uuid.uuid4().hex[:8]}"
    await corpus.set_status(key, "rejected")  # must not raise
    assert await corpus.get_by_canonical_key(key) is None


async def test_a_pre_slice_document_without_status_reads_as_a_live_artifact(corpus):
    """The `learning_corpus` bucket is durable and never migrated. A doc written before
    `status`/`source` existed carries neither key, and must still load as the live
    learning artifact it has always been — not as a terminal one, which would silently
    delete it from prior art."""
    key = f"sha256:live-legacy-{uuid.uuid4().hex[:8]}"
    corpus._created.append(key)
    await corpus._collection.upsert(
        _doc_id(key),
        {"id": "candidate::legacy", "canonical_key": key, "intent": "legacy", "hit_count": 3},
    )

    art = await corpus.get_by_canonical_key(key)
    assert art is not None
    assert art.status == "extracted"
    assert art.source == "learning"
    assert not art.is_terminal
    assert art.hit_count == 3


# --- RBAC boundary (D48/D17) ------------------------------------------------


async def test_corpus_writer_denied_on_other_buckets(corpus):
    """`learning_corpus_writer` is scoped to `learning_corpus` ONLY: it can write its
    own bucket (positive control) but is DENIED a write to BOTH `agent_sessions` and
    `learning_candidates`. A write probe is used so a plain not-found can never
    masquerade as access."""
    from acouchbase.cluster import Cluster
    from couchbase.auth import PasswordAuthenticator
    from couchbase.exceptions import DocumentNotFoundException
    from couchbase.options import ClusterOptions

    settings = _settings()
    cluster = Cluster(
        settings.learning_corpus_connection_string,
        ClusterOptions(
            PasswordAuthenticator(
                settings.learning_corpus_username, settings.learning_corpus_password
            )
        ),
    )
    await cluster.on_connect()
    try:
        # Positive control: writer CAN write+read its own bucket.
        own = cluster.bucket(settings.learning_corpus_bucket).default_collection()
        probe = f"corpus::rbac-probe::{uuid.uuid4().hex[:8]}"
        await own.upsert(probe, {"ok": True})
        assert (await own.get(probe)).content_as[dict] == {"ok": True}
        await own.remove(probe)

        # Denial on agent_sessions (write probe).
        sessions = cluster.bucket("agent_sessions").scope("_default").collection("sessions")
        with pytest.raises(Exception) as sess_exc:  # noqa: PT011 - SDK maps authz to varied types
            await sessions.upsert(f"session::leak-{uuid.uuid4().hex[:8]}", {"leak": True})
        assert not isinstance(sess_exc.value, DocumentNotFoundException)

        # Denial on learning_candidates (write probe) — a sibling store, still off-limits.
        candidates = cluster.bucket("learning_candidates").default_collection()
        with pytest.raises(Exception) as cand_exc:  # noqa: PT011
            await candidates.upsert(f"candidate::leak-{uuid.uuid4().hex[:8]}", {"leak": True})
        assert not isinstance(cand_exc.value, DocumentNotFoundException)
    finally:
        await cluster.close()
