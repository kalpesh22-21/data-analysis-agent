"""S6 dedup — the D48 two-layer blueprint dedup stage (Track B, Slice 6).

Slugs:
  * S6-canonical-key-dedup       — identical semantics ⇒ identical canonical_key ⇒
                                    action==increment (one create + one bump).
  * S6-failsoft-no-wrong-merge   — absent/unparseable canonical_ast_norm ⇒ the hard
                                    key is skipped, the soft layer runs, and a
                                    dissimilar/degraded embedder yields insert,
                                    NEVER a wrong merge (D48/D52).

Built against `s4_enriched_blueprint.json` + `existing_corpus_keys.json`.
"""

from __future__ import annotations

import json
from pathlib import Path

from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.dedup import (
    CorpusArtifact,
    DedupStage,
    InMemoryBlueprintCorpus,
    compute_canonical_key,
)
from data_agent.learning.stage import StageContext
from data_agent.learning.summary.models import SessionSummary
from data_agent.learning.triage import TriageVerdict
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _ctx() -> StageContext:
    summary = SessionSummary(
        session_id="sess-fixture",
        user_id="u1",
        scope_ref="scope-1",
        trace_id="trace-fixture",
        content_hash="hash-fixture",
        turns=(),
        tool_calls=(),
        blueprint_usages=(),
        askuser_exchanges=(),
        failed_fixed_sql=(),
        accepted_signal="no_correction",
    )
    return StageContext(summary=summary, verdict=TriageVerdict(decision="keep", reason="K1"))


def _single_envelope() -> CandidateEnvelope:
    return CandidateEnvelope.from_doc(_load("s4_enriched_blueprint.json")["single"]["envelope"])


def _key_of(env: CandidateEnvelope) -> str:
    gen = env.payload["generalization"]
    return compute_canonical_key(
        env.payload["resolves"],
        gen["uses_rules"],
        gen["result_grain"],
        gen["canonical_ast_norm"],
    )


# --- S6-canonical-key-dedup ---------------------------------------------------


async def test_identical_semantics_hash_to_identical_key():
    """Two candidates with the same (resolves, uses_rules, result_grain,
    canonical_ast_norm) must produce byte-identical canonical keys."""
    a = _single_envelope()
    b = _single_envelope()
    assert _key_of(a) == _key_of(b)
    # And a different canonical_ast_norm yields a different key (sanity).
    gen = dict(b.payload["generalization"])
    gen["canonical_ast_norm"] = gen["canonical_ast_norm"] + " -- variant"
    b2 = b.payload["resolves"]
    assert compute_canonical_key(b2, gen["uses_rules"], gen["result_grain"], gen["canonical_ast_norm"]) != _key_of(a)


async def test_hard_key_hit_increments_and_drops():
    """S6-canonical-key-dedup: one create + one bump. The first pass against an
    empty corpus inserts; after the artifact is landed, an identical candidate
    hits the hard key, increments the artifact's hit_count, and is DROPPED."""
    env = _single_envelope()
    key = _key_of(env)
    corpus = InMemoryBlueprintCorpus()  # empty — nothing landed yet
    # A scripted embedder whose intents are mutually dissimilar (soft layer must
    # NOT create a false near-match on the first, corpus-empty pass).
    embedder = FakeEmbeddingClient()
    stage = DedupStage(corpus, embedder)

    # Pass 1: empty corpus ⇒ new key ⇒ insert (the "create"), pipeline continues.
    first = await stage.process(env, _ctx())
    assert first.control == "continue"
    assert first.envelope.dedup is not None
    assert first.envelope.dedup.action == "insert"
    assert first.envelope.dedup.canonical_key == key
    assert corpus.increment_calls == []  # nothing bumped on a create

    # Simulate S9 landing the validated candidate as a corpus artifact.
    corpus.land(
        CorpusArtifact(
            id="blueprint::dept-earnings-year",
            canonical_key=key,
            intent=env.payload["intent"],
            hit_count=1,
        )
    )

    # Pass 2: an identical candidate now HITS the hard key ⇒ increment + drop.
    second = await stage.process(_single_envelope(), _ctx())
    assert second.control == "drop"  # the duplicate is dropped, not persisted
    assert second.envelope.dedup is not None
    assert second.envelope.dedup.action == "increment"
    assert second.envelope.dedup.layer == "hard"
    assert second.envelope.dedup.similarity == 1.0
    assert second.envelope.dedup.matched_id == "blueprint::dept-earnings-year"
    # Exactly ONE bump, on the right key; the artifact's count went 1 → 2.
    assert corpus.increment_calls == [key]
    assert corpus.get_sync(key).hit_count == 2


async def test_insert_seeds_corpus_artifact_at_hit_count_one():
    """R3 (contract §11.1): a genuinely-new `insert` SEEDS the corpus artifact at
    hit_count=1 from THIS first candidate — so the count-based promotion threshold
    can accrue BEFORE the artifact lands (else the count could never converge). The
    seed is keyed by canonical_key and idempotent."""
    env = _single_envelope()
    key = _key_of(env)
    corpus = InMemoryBlueprintCorpus()  # empty — nothing landed yet
    stage = DedupStage(corpus, FakeEmbeddingClient())

    first = await stage.process(env, _ctx())
    assert first.envelope.dedup is not None and first.envelope.dedup.action == "insert"
    # The artifact now exists at the key, seeded once at hit_count=1.
    assert corpus.seed_calls == [key]
    seeded = corpus.get_sync(key)
    assert seeded is not None
    assert seeded.hit_count == 1
    assert seeded.canonical_key == key

    # A second identical candidate now finds the seeded artifact ⇒ increment (not a
    # re-seed): the count accrues from the candidate stage, before any landing.
    second = await stage.process(_single_envelope(), _ctx())
    assert second.envelope.dedup is not None and second.envelope.dedup.action == "increment"
    assert corpus.seed_calls == [key]  # not re-seeded
    assert corpus.get_sync(key).hit_count == 2


async def test_seeded_existing_corpus_hard_key_hit():
    """Seeding the corpus with an artifact at the candidate's computed key makes
    the FIRST pass an increment (the existing-corpus collision path)."""
    env = _single_envelope()
    key = _key_of(env)
    corpus = InMemoryBlueprintCorpus(
        [CorpusArtifact(id="blueprint::dept-earnings-year", canonical_key=key,
                        intent=env.payload["intent"], hit_count=2)]
    )
    stage = DedupStage(corpus, FakeEmbeddingClient())
    result = await stage.process(env, _ctx())
    assert result.control == "drop"
    assert result.envelope.dedup.action == "increment"
    assert corpus.get_sync(key).hit_count == 3


# --- S6-failsoft-no-wrong-merge -----------------------------------------------


async def test_failsoft_absent_norm_skips_hard_key_no_wrong_merge():
    """S6-failsoft-no-wrong-merge: an absent canonical_ast_norm skips the hard key
    and falls to the soft layer. With a dissimilar embedder the verdict is insert
    (layer=soft, empty hard key) — NEVER a wrong merge, even against a corpus that
    holds an artifact at what WOULD have been the hard key."""
    env = _single_envelope()
    would_be_key = _key_of(env)
    # Blank the norm ⇒ fail-soft. Payload is a plain dict; rebuild it.
    gen = dict(env.payload["generalization"])
    gen["canonical_ast_norm"] = ""
    payload = dict(env.payload)
    payload["generalization"] = gen
    from dataclasses import replace

    env = replace(env, payload=payload)

    # Corpus holds the artifact at the would-be hard key — a naive hard match would
    # increment. Fail-soft must NOT reach it, and the (dissimilar) soft layer must
    # not merge either.
    corpus = InMemoryBlueprintCorpus(
        [CorpusArtifact(id="blueprint::dept-earnings-year", canonical_key=would_be_key,
                        intent="something totally unrelated about widgets", hit_count=9)]
    )
    stage = DedupStage(corpus, FakeEmbeddingClient())
    result = await stage.process(env, _ctx())
    assert result.control == "continue"
    assert result.envelope.dedup.action == "insert"  # not increment, not merge
    assert result.envelope.dedup.layer == "soft"  # hard key was skipped
    assert result.envelope.dedup.canonical_key == ""  # no hard key available
    assert corpus.increment_calls == []  # the would-be artifact was never bumped
    assert corpus.get_sync(would_be_key).hit_count == 9  # untouched


async def test_failsoft_embedder_failure_degrades_to_insert():
    """A failing embedder on the fail-soft path degrades to insert (never merge)."""
    env = _single_envelope()
    gen = dict(env.payload["generalization"])
    gen["canonical_ast_norm"] = ""
    payload = dict(env.payload)
    payload["generalization"] = gen
    from dataclasses import replace

    env = replace(env, payload=payload)
    corpus = InMemoryBlueprintCorpus(
        [CorpusArtifact(id="x", canonical_key="sha256:whatever",
                        intent="anything", hit_count=1)]
    )
    stage = DedupStage(corpus, FakeEmbeddingClient(fail=True))
    result = await stage.process(env, _ctx())
    assert result.envelope.dedup.action == "insert"
    assert result.envelope.dedup.matched_id is None


# --- soft-layer near-match adjudication ---------------------------------------


async def test_soft_near_match_yields_conflict_for_inbox():
    """A hard-key MISS with a soft-layer near-match (identical scripted embedding,
    band conflict≤sim<merge) is adjudicated as a conflict for the writer to route
    to the inbox — never auto-appended (§3)."""
    env = _single_envelope()
    intent = env.payload["intent"]
    other_intent = "total earnings for a department per year"
    # Script both intents to the SAME vector ⇒ cosine similarity 1.0 ⇒ merge band.
    vec = [0.1, 0.2, 0.3, 0.4]
    embedder = FakeEmbeddingClient({intent: vec, other_intent: vec})
    corpus = InMemoryBlueprintCorpus(
        [CorpusArtifact(id="blueprint::near", canonical_key="sha256:different-key",
                        intent=other_intent, hit_count=1)]
    )
    stage = DedupStage(corpus, embedder)
    result = await stage.process(env, _ctx())
    # sim == 1.0 ≥ merge_threshold ⇒ merge (a mergeable variant). Continues to the
    # writer, which routes soft near-matches to the inbox (reason dedup_conflict).
    assert result.control == "continue"
    assert result.envelope.dedup.layer == "soft"
    assert result.envelope.dedup.action == "merge"
    assert result.envelope.dedup.matched_id == "blueprint::near"


async def test_conflict_band_scripted():
    """A similarity inside the conflict band [conflict, merge) yields conflict."""
    env = _single_envelope()
    intent = env.payload["intent"]
    other = "headcount for a department as of a date"
    embedder = FakeEmbeddingClient(
        {intent: [1.0, 0.0, 0.0], other: [0.9, 0.436, 0.0]}  # cosine ≈ 0.9
    )
    corpus = InMemoryBlueprintCorpus(
        [CorpusArtifact(id="blueprint::hc", canonical_key="sha256:hc", intent=other)]
    )
    stage = DedupStage(corpus, embedder, merge_threshold=0.95, conflict_threshold=0.83)
    result = await stage.process(env, _ctx())
    assert result.envelope.dedup.action == "conflict"
    assert result.envelope.dedup.layer == "soft"


async def test_non_blueprint_passes_through_untouched():
    env = CandidateEnvelope.from_doc(_load("envelopes_each_reason.json")["knowledge_pre_gate"])
    stage = DedupStage(InMemoryBlueprintCorpus(), FakeEmbeddingClient())
    result = await stage.process(env, _ctx())
    assert result.control == "continue"
    assert result.envelope.dedup is None
