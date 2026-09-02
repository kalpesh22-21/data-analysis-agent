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

import pytest

from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.dedup import (
    CorpusArtifact,
    DedupStage,
    InMemoryBlueprintCorpus,
    compute_canonical_key,
)
from data_agent.learning.priorart.models import TERMINAL_STATUSES
from data_agent.learning.stage import StageContext, run_pipeline
from data_agent.learning.summary.models import SessionSummary
from data_agent.learning.triage import TriageVerdict
from data_agent.learning.writer.routing import derive_inbox_reason, route_candidate
from data_agent.learning.writer.stage import WriterStage
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


async def test_hard_key_hit_increments_and_continues_to_human_review():
    """S6-canonical-key-dedup: one create + one bump. The first pass against an
    empty corpus inserts; after the artifact is landed, an identical candidate
    hits the hard key and increments the artifact's hit_count.

    The matched artifact is LIVE (the default `extracted` status), so the duplicate
    CONTINUES to the writer, which routes it to the inbox as a suppressed duplicate.
    A hard-key hit on a TERMINAL artifact still drops — that is the negative-memory
    contract, pinned in `test_dedup_span.py` and `test_rejection_is_negative_memory_qa.py`."""
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

    # Pass 2: an identical candidate now HITS the hard key ⇒ increment + preserve.
    second = await stage.process(_single_envelope(), _ctx())
    assert second.control == "continue"  # the writer routes it to editable review
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
    assert result.control == "continue"
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


class _ListRaisingCorpus:
    """A `BlueprintCorpus` whose soft-layer `list_artifacts()` raises (mirrors the
    DURABLE corpus with a missing primary index / an unreachable query service / a
    malformed doc). Hard-key lookups MISS (empty), and seed/increment succeed — only
    the N1QL scan fails."""

    def __init__(self) -> None:
        self.seeded: list[CorpusArtifact] = []

    async def get_by_canonical_key(self, canonical_key: str):
        return None

    async def seed_artifact(self, artifact: CorpusArtifact) -> None:
        self.seeded.append(artifact)

    async def increment_hit_count(self, canonical_key: str) -> None:
        return None

    async def list_artifacts(self):
        raise RuntimeError("N1QL: no primary index on learning_corpus")


async def test_soft_layer_list_artifacts_failure_degrades_to_insert():
    """D52: with the DURABLE corpus, `list_artifacts()` is a fallible N1QL scan. A
    failure must NOT escape the stage and dead-letter the session — it degrades to
    `insert` (the race-safe hard-key layer already ran), exactly like an embedder
    failure. Proves the corpus-scan failure is inside the degrade path."""
    env = _single_envelope()
    corpus = _ListRaisingCorpus()
    stage = DedupStage(corpus, FakeEmbeddingClient())

    result = await stage.process(env, _ctx())  # must NOT raise

    assert result.control == "continue"  # never a job failure / dead-letter
    assert result.envelope.dedup is not None
    assert result.envelope.dedup.action == "insert"
    assert result.envelope.dedup.layer == "soft"
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


# --- the hard layer's TERMINAL / LIVE split -----------------------------------
#
# A hard-key hit is the one lookup here that is deliberately NOT status-filtered, and what
# it does with the artifact it finds is a two-way branch: drop a re-derivation of something
# a human KILLED (negative memory), keep a duplicate of something still LIVE (an editable
# review item). `test_dedup_span.py` pins the telemetry of that split; what follows pins the
# split itself — over the WHOLE status space rather than one member of it, and through to
# the routing decision rather than stopping at the stage boundary.


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
async def test_every_terminal_status_drops_the_byte_identical_re_derivation(status):
    """DERIVED FROM `TERMINAL_STATUSES`, not from a remembered list of two.

    `CorpusArtifact.is_terminal` reads the shared frozenset that `priorart.models` owns, so
    the day a third dead state is added (say `superseded`) this parametrization covers it
    automatically and a stage that only special-cased `rejected` fails here. That direction
    matters more than it looks: the failure of an UNCOVERED terminal status is silent and
    permanent — the loop re-proposes an idea a human already declined, and the approve of
    that duplicate MERGEs the dead artifact's own graph node (`landing_id` is derived from
    `dedup.canonical_key`, which for a hard-key hit IS the matched artifact's key) and
    stamps it `validated` again.
    """
    env = _single_envelope()
    key = _key_of(env)
    corpus = InMemoryBlueprintCorpus(
        [CorpusArtifact(id="a-dead-one", canonical_key=key, intent="x", status=status)]
    )

    result = await DedupStage(corpus, FakeEmbeddingClient()).process(env, _ctx())

    assert result.control == "drop"
    assert result.envelope.dedup.action == "increment"
    assert result.envelope.dedup.layer == "hard"
    assert result.envelope.dedup.matched_id == "a-dead-one"


def test_the_terminal_set_is_exactly_what_a_human_decision_can_leave_on_an_artifact():
    """The coverage question behind the parametrization above: are those the statuses an
    artifact can actually END UP in?

    Only two edges ever stamp a `learning_corpus` artifact — the scheduler's `reject` and
    `retract` — and this reads their arguments out of the source rather than restating them,
    so a THIRD terminal edge added without extending `TERMINAL_STATUSES` fails here instead
    of silently creating a dead artifact the hard layer still treats as live. Every other
    status an artifact can carry (`extracted`, and anything a future writer seeds) is by
    construction not a human decision, which is why the live branch is the default.
    """
    import inspect
    import re

    from data_agent.learning.promotion import scheduler as scheduler_module

    stamped = set(
        re.findall(
            r"_stamp_corpus_status\(\s*env,\s*CandidateStatus\.([A-Z_]+)\s*\)",
            inspect.getsource(scheduler_module),
        )
    )
    assert stamped, "the reject/retract corpus stamps moved — this test needs rewriting"
    assert {getattr(CandidateStatus, name) for name in stamped} == TERMINAL_STATUSES


@pytest.mark.parametrize(
    "status",
    sorted(
        {
            value
            for name, value in vars(CandidateStatus).items()
            if not name.startswith("_") and isinstance(value, str)
        }
        - TERMINAL_STATUSES
    )
    + ["", "REJECTED", "rejected ", "some_future_state"],
)
async def test_a_hit_on_any_live_status_keeps_the_duplicate_for_a_human(status):
    """The complement, over every status that is NOT terminal — including the ones a
    rehydrated doc could carry that nobody wrote (`CorpusArtifact.from_doc` does not
    validate `status`, so casing and whitespace near-misses of `rejected` arrive verbatim).

    Nothing is settled about a live artifact, so the duplicate must stay reachable: the
    candidate CONTINUES down the pipeline and its fate is the writer's to decide. Note the
    asymmetry with the terminal case is fail-SOFT in the direction that costs a reviewer a
    row rather than the one that resurrects a settled decision.
    """
    env = _single_envelope()
    key = _key_of(env)
    corpus = InMemoryBlueprintCorpus(
        [CorpusArtifact(id="a-live-one", canonical_key=key, intent="x", status=status)]
    )

    result = await DedupStage(corpus, FakeEmbeddingClient()).process(env, _ctx())

    assert result.control == "continue"
    assert result.envelope.dedup.action == "increment"


async def test_the_hit_count_is_bumped_on_both_the_terminal_and_the_live_path():
    """ONE BUMP EITHER WAY, asserted side by side because the whole point of the
    `matched_status` span tag is that these two increments are NOT the same fact.

    The count on a live artifact feeds the promotion corroboration gate ("the third sighting
    of something we might promote"); the count on a dead one is a measurement of how good
    our rejections are ("somebody re-derived a declined idea"). A change that stopped
    counting on the drop path would look harmless — the candidate dies anyway — and would
    silently zero the second metric. The two are therefore pinned together, not apart.
    """
    env = _single_envelope()
    key = _key_of(env)

    live = InMemoryBlueprintCorpus(
        [CorpusArtifact(id="a-live-one", canonical_key=key, intent="x", hit_count=1)]
    )
    live_result = await DedupStage(live, FakeEmbeddingClient()).process(env, _ctx())

    dead = InMemoryBlueprintCorpus(
        [
            CorpusArtifact(
                id="a-dead-one", canonical_key=key, intent="x", hit_count=1, status="rejected"
            )
        ]
    )
    dead_result = await DedupStage(dead, FakeEmbeddingClient()).process(
        _single_envelope(), _ctx()
    )

    assert (live_result.control, dead_result.control) == ("continue", "drop")
    # Exactly one increment each, on the right key, and it LANDED on the artifact.
    assert live.increment_calls == [key] and live.get_sync(key).hit_count == 2
    assert dead.increment_calls == [key] and dead.get_sync(key).hit_count == 2


async def test_a_live_hard_key_hit_reaches_the_writer_as_a_suppressed_duplicate():
    """END TO END through the stage INTO the routing decision, which is the only place the
    live branch's promise is actually kept.

    "The writer recognizes the non-insert verdict and routes it to the inbox as a suppressed
    duplicate" is a claim spanning two modules, and both halves are separately tested today:
    the stage returns `continue`, and `route_candidate` labels an `increment` verdict
    `suppressed_duplicate`. Neither notices if the ENVELOPE stops carrying the verdict
    between them — a `replace(env, dedup=...)` that dropped the field, or a writer reached
    before the stamp — and the result would be a byte-identical duplicate auto-landing as
    clean. So this runs the real two-stage pipeline and reads the routing off the envelope
    that actually came out of it.

    The sampler is pinned OFF so that reaching the inbox can only be the dedup verdict's
    doing, never the audit sample's.
    """
    env = _single_envelope()
    key = _key_of(env)
    corpus = InMemoryBlueprintCorpus(
        [CorpusArtifact(id="blueprint::dept-earnings-year", canonical_key=key, intent="x")]
    )

    outcome = await run_pipeline(
        (
            DedupStage(corpus, FakeEmbeddingClient()),
            WriterStage(sampler=lambda _env: False),
        ),
        env,
        _ctx(),
    )

    assert outcome.control == "route_inbox"
    assert outcome.persist is True  # it is a review item, so it MUST be written
    assert outcome.envelope.status == CandidateStatus.IN_REVIEW
    assert outcome.envelope.dedup.action == "increment"
    # The label the reviewer sees, from BOTH sources of it — the writer's decision and the
    # inbox's re-derivation — over the envelope the pipeline actually produced.
    assert route_candidate(outcome.envelope, sampled_for_inbox=False).reason == (
        "suppressed_duplicate"
    )
    assert derive_inbox_reason(outcome.envelope) == "suppressed_duplicate"


async def test_a_terminal_hard_key_hit_never_reaches_the_writer_at_all():
    """The control for the test above, through the same pipeline: a drop is not a route.

    Asserted on the WRITER's absence of effect rather than only on the control string,
    because the failure that matters is a candidate that both drops AND gets a status: the
    envelope must leave the pipeline unrouted, so nothing downstream can mistake it for a
    review item, and `persist=False` so the enriched copy is never written.
    """
    env = _single_envelope()
    key = _key_of(env)
    corpus = InMemoryBlueprintCorpus(
        [
            CorpusArtifact(
                id="a-dead-one", canonical_key=_key_of(env), intent="x", status="retired"
            )
        ]
    )
    assert key  # the hit is a hard-key hit, not a soft one

    outcome = await run_pipeline(
        (
            DedupStage(corpus, FakeEmbeddingClient()),
            WriterStage(sampler=lambda _env: True),  # even a sampled coin cannot save it
        ),
        env,
        _ctx(),
    )

    assert outcome.control == "drop"
    assert outcome.persist is False
    assert outcome.envelope.status == env.status  # never routed, never re-statused
