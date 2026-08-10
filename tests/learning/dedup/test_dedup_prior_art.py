"""S6 dedup — the CROSS-TIER prior-art layer (PriorArt Slice 2).

The bug this closes: `DedupStage` compared each candidate only against the
`learning_corpus` bucket, which `_seed_on_insert` seeds itself — so it contained ONLY
what the loop had already minted. The MCP canon the agent recalls and the landed
learning tier were invisible, and a session re-deriving `bp-total-earnings-by-department`
produced a duplicate that nothing noticed.

Slugs:
  * S6-canon-redundancy-drops    — a DETERMINISTIC structural-key hit on the MCP canon
                                   drops the candidate as `redundant_with_canon`, and
                                   never `increment`s a count the loop does not own.
  * S6-learning-twin-to-review   — the same hit against the LEARNING tier routes to a
                                   human as `merge` (no count to bump, no drop).
  * S6-soft-union                — the soft layer adjudicates the UNION of the graph
                                   index and the `learning_corpus` bucket, de-duplicated
                                   across the two and ranked by confidence.
  * S6-prior-art-fails-open      — an unavailable index leaves the bucket half working
                                   and the loop keeps running.
  * S6-terminal-artifacts-excluded — a rejected artifact is not live prior art.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.dedup import (
    CorpusArtifact,
    DedupStage,
    InMemoryBlueprintCorpus,
    compute_canonical_key,
)
from data_agent.learning.priorart import (
    TIER_LEARNING,
    TIER_MCP,
    TIER_UNSOURCED,
    InMemoryPriorArtIndex,
    PriorArtCard,
)
from data_agent.learning.stage import StageContext
from data_agent.learning.summary.models import SessionSummary
from data_agent.learning.triage import TriageVerdict
from data_agent.runtime.blueprint.structural_key import structural_key_from_templates
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


def _envelope() -> CandidateEnvelope:
    return CandidateEnvelope.from_doc(_load("s4_enriched_blueprint.json")["single"]["envelope"])


def _hard_key(env: CandidateEnvelope) -> str:
    gen = env.payload["generalization"]
    return compute_canonical_key(
        env.payload["resolves"],
        gen["uses_rules"],
        gen["result_grain"],
        gen["canonical_ast_norm"],
    )


def _structural_key(env: CandidateEnvelope) -> str:
    """The key the stage derives internally — computed here through the SAME safe entry
    point (`structural_key_from_templates`), which is what makes the test's expectation
    and the stage's derivation agree by construction rather than by transcription."""
    gen = env.payload["generalization"]
    return structural_key_from_templates(
        gen.get("result_grain"),
        gen.get("sql_template"),
        [(n["order"], n["sql_template"]) for n in gen.get("node_templates") or []],
    )


def _card(id_: str, *, tier: str, key: str, intent: str = "an existing blueprint",
          status: str = "validated") -> PriorArtCard:
    return PriorArtCard(
        id=id_,
        kind="blueprint",
        tier=tier,  # type: ignore[arg-type]
        status=status,
        verified=tier == TIER_MCP,
        drift_status="clean",
        intent=intent,
        result_grain=("department",),
        uses_rules=(),
        structural_key=key,
        embedding_model="all-mpnet-base-v2",
        similarity=0.0,
        model_matched=True,
    )


# --- S6-canon-redundancy-drops ------------------------------------------------


async def test_a_structural_hit_on_the_canon_drops_as_redundant_with_canon():
    """The verdict `increment` cannot express: there is no `learning_corpus` artifact
    behind a git-versioned MCP blueprint, and no hit count the loop is entitled to bump.
    The candidate is DROPPED, and the drop is a distinct, countable verdict — a high rate
    of it is a RETRIEVAL defect (the agent is not recalling what it already has)."""
    env = _envelope()
    corpus = InMemoryBlueprintCorpus()
    index = InMemoryPriorArtIndex(
        [_card("bp-total-earnings-by-department", tier=TIER_MCP, key=_structural_key(env))]
    )
    stage = DedupStage(corpus, FakeEmbeddingClient(), prior_art=index)

    result = await stage.process(env, _ctx())

    assert result.control == "drop"
    verdict = result.envelope.dedup
    assert verdict is not None
    assert verdict.action == "redundant_with_canon"
    assert verdict.layer == "structural"
    assert verdict.matched_id == "bp-total-earnings-by-department"
    # NOTHING was written to the learning corpus: no bogus increment, and — critically —
    # no seeded artifact either. Seeding here would record an MCP-canon blueprint as a
    # learning artifact and hand it a hit count that then feeds the promotion guard.
    assert corpus.increment_calls == []
    assert corpus.seed_calls == []
    assert corpus.get_sync(_hard_key(env)) is None


async def test_the_hard_key_still_wins_over_the_structural_layer():
    """Layer order is the design: the SHA-256 hard key is race-safe by construction, and
    that property is what makes the cross-session hit count sound. A candidate that hits
    BOTH must take the `increment` path, or two workers racing the same candidate could
    disagree about which layer adjudicated it."""
    env = _envelope()
    key = _hard_key(env)
    corpus = InMemoryBlueprintCorpus(
        [CorpusArtifact(id="artifact-1", canonical_key=key, intent="x")]
    )
    index = InMemoryPriorArtIndex([_card("bp-canon", tier=TIER_MCP, key=_structural_key(env))])
    stage = DedupStage(corpus, FakeEmbeddingClient(), prior_art=index)

    result = await stage.process(env, _ctx())

    assert result.envelope.dedup.action == "increment"
    assert result.envelope.dedup.layer == "hard"
    assert index.key_calls == []  # the structural layer was never consulted


async def test_an_unsourced_node_can_never_trigger_the_canon_drop():
    """A node with no `source` is a hand edit or a foreign writer (both real writers
    always stamp one). Prior art SEES it — that is the point of dropping the trust gate —
    but it is not evidence of canon, so it can only route to a human, never drop."""
    env = _envelope()
    index = InMemoryPriorArtIndex(
        [_card("bp-mystery", tier=TIER_UNSOURCED, key=_structural_key(env))]
    )
    stage = DedupStage(InMemoryBlueprintCorpus(), FakeEmbeddingClient(), prior_art=index)

    result = await stage.process(env, _ctx())

    assert result.control == "continue"
    assert result.envelope.dedup.action == "merge"


# --- S6-learning-twin-to-review -----------------------------------------------


async def test_a_structural_hit_on_the_learning_tier_routes_to_review():
    """Structurally the same query, but the match was found by the LOOSE key while the
    corpus bucket is keyed by the FROZEN one — there is no artifact to increment. A human
    glance (`merge` → inbox reason `dedup_conflict`) is the honest outcome."""
    env = _envelope()
    index = InMemoryPriorArtIndex(
        [_card("bp::sha256:whatever", tier=TIER_LEARNING, key=_structural_key(env))]
    )
    corpus = InMemoryBlueprintCorpus()
    stage = DedupStage(corpus, FakeEmbeddingClient(), prior_art=index)

    result = await stage.process(env, _ctx())

    assert result.control == "continue"
    assert result.envelope.dedup.action == "merge"
    assert result.envelope.dedup.layer == "structural"
    assert result.envelope.dedup.matched_id == "bp::sha256:whatever"
    # A `merge` is not an `insert`, so nothing is seeded — the candidate is going to a
    # human, and seeding would start a hit count for something we may merge away.
    assert corpus.seed_calls == []


async def test_a_terminal_prior_art_node_does_not_block_a_new_candidate():
    """S6-terminal-artifacts-excluded, graph side: a human REJECTED this blueprint, so
    it is not prior art at all and the candidate proceeds normally."""
    env = _envelope()
    index = InMemoryPriorArtIndex(
        [_card("bp-dead", tier=TIER_MCP, key=_structural_key(env), status="rejected")]
    )
    stage = DedupStage(InMemoryBlueprintCorpus(), FakeEmbeddingClient(), prior_art=index)

    result = await stage.process(env, _ctx())

    assert result.control == "continue"
    assert result.envelope.dedup.action == "insert"


# --- S6-prior-art-replaces-scan -----------------------------------------------


class _CountingCorpus(InMemoryBlueprintCorpus):
    """Counts `list_artifacts()` — the bucket half of the soft union."""

    def __init__(self, artifacts=None) -> None:
        super().__init__(artifacts)
        self.list_calls = 0

    async def list_artifacts(self):
        self.list_calls += 1
        return await super().list_artifacts()


async def test_the_soft_layer_queries_both_sources_and_adjudicates_the_union():
    """HISTORY. This test used to assert `corpus.list_calls == 0` — that the index had
    REPLACED the bucket scan. That was the regression: the bucket is where an IN-FLIGHT
    sibling candidate lives (`_seed_on_insert` puts it there long before it lands), and
    the graph cannot see one at all. The two sources are complementary, so both are
    queried and the union is banded.

    What survives from the original intent is the cost story: the index is ONE ANN call
    against the whole canon, and the bucket scan is bounded by `len(learning_corpus)` —
    artifacts the loop itself minted."""
    env = _envelope()
    intent = env.payload["intent"]
    corpus = _CountingCorpus(
        [CorpusArtifact(id=f"a{i}", canonical_key=f"sha256:{i}", intent="noise") for i in range(50)]
    )
    index = InMemoryPriorArtIndex(
        [_card("bp-near", tier=TIER_LEARNING, key="sha256:other-key", intent="unrelated")],
        scores={(intent, "bp-near"): 0.90},
    )
    embedder = FakeEmbeddingClient()
    stage = DedupStage(corpus, embedder, prior_art=index)

    result = await stage.process(env, _ctx())

    assert index.search_calls == [(intent, ("blueprint",), 5)]
    assert corpus.list_calls == 1  # the bucket half ran too — exactly once
    # The graph card still wins the union: the bucket artifacts are unrelated noise.
    assert result.envelope.dedup.action == "conflict"  # 0.83 <= 0.90 < 0.95
    assert result.envelope.dedup.layer == "soft"
    assert result.envelope.dedup.matched_id == "bp-near"


async def test_an_in_flight_sibling_in_the_bucket_beats_a_weaker_graph_match():
    """The union's whole point: the best answer can come from EITHER source, and the
    bucket's is the one no graph read can produce."""
    env = _envelope()
    intent = env.payload["intent"]
    sibling = "a paraphrase of the same question"
    vec = [0.1, 0.2, 0.3, 0.4]
    corpus = InMemoryBlueprintCorpus(
        [CorpusArtifact(id="cand-sibling", canonical_key="sha256:sibling", intent=sibling)]
    )
    index = InMemoryPriorArtIndex(
        [_card("bp-weak", tier=TIER_LEARNING, key="sha256:weak")],
        scores={(intent, "bp-weak"): 0.84},
    )
    stage = DedupStage(
        corpus, FakeEmbeddingClient({intent: vec, sibling: vec}), prior_art=index
    )

    result = await stage.process(env, _ctx())

    # The bucket sibling scores 1.0 (identical scripted vectors) and beats the 0.84 card.
    assert result.envelope.dedup.action == "merge"
    assert result.envelope.dedup.matched_id == "cand-sibling"


async def test_a_landed_artifact_is_not_counted_twice_across_the_two_sources():
    """Once a candidate LANDS, the same blueprint exists in both stores under different
    ids — the bucket keys it by `canonical_key`, the node by `bp::<canonical_key>`. The
    graph copy wins: it is the richer projection (real tier, status, `verified`,
    structural key) where the bucket card has only a name and a cosine. Without the join
    the same artifact would occupy two slots in the union and `matched_id` would be
    whichever scored marginally higher — nondeterministic across embedder versions."""
    env = _envelope()
    intent = env.payload["intent"]
    key = "sha256:landed-twin"
    vec = [0.1, 0.2, 0.3, 0.4]
    corpus = InMemoryBlueprintCorpus(
        [CorpusArtifact(id="cand-original", canonical_key=key, intent=intent)]
    )
    index = InMemoryPriorArtIndex(
        [_card(f"bp::{key}", tier=TIER_LEARNING, key="sha256:sk")],
        scores={(intent, f"bp::{key}"): 0.97},
    )
    stage = DedupStage(
        corpus, FakeEmbeddingClient({intent: vec}), prior_art=index
    )

    result = await stage.process(env, _ctx())

    assert result.envelope.dedup.matched_id == f"bp::{key}"
    assert result.envelope.dedup.similarity == 0.97  # the GRAPH card's score, verbatim


async def test_the_landing_prefix_matches_the_landing_writer():
    """The join above derives the landed node id from the artifact's canonical key, using
    a COPY of the landing writer's prefix (importing `promotion.landing` would drag the
    whole sqlglot corpus-loader into this stage). A drift between the two would silently
    disable the dedup join — both copies would still work, they would just stop agreeing.
    One definition plus an identity test is the house rule for a mirror constant."""
    from data_agent.learning.dedup.stage import _BLUEPRINT_LANDING_PREFIX
    from data_agent.learning.promotion.landing import _LANDING_PREFIX

    assert _BLUEPRINT_LANDING_PREFIX == _LANDING_PREFIX["blueprint"]


async def test_a_bucket_artifact_can_never_produce_the_canon_drop():
    """A `learning_corpus` document could carry `source: "mcp"` — nothing validates it —
    and the card projection reports that tier honestly rather than laundering it. The
    protection is STRUCTURAL, not a filter: `redundant_with_canon` is emitted only by
    layer 2, which reads `get_by_structural_key` (the graph) and never sees a bucket
    card at all. Pinned because "the soft layer cannot drop" is an invariant a future
    edit could break without touching this projection."""
    env = _envelope()
    intent = env.payload["intent"]
    vec = [0.5, 0.5]
    artifact = CorpusArtifact(
        id="cand-mislabelled",
        canonical_key="sha256:mislabelled",
        intent=intent,
        source=TIER_MCP,  # a data error, faithfully reported
    )
    stage = DedupStage(
        InMemoryBlueprintCorpus([artifact]),
        FakeEmbeddingClient({intent: vec}),
        prior_art=InMemoryPriorArtIndex([]),
    )

    result = await stage.process(env, _ctx())

    assert result.control == "continue"  # NOT dropped
    assert result.envelope.dedup.action == "merge"
    assert result.envelope.dedup.action != "redundant_with_canon"


async def test_the_union_is_ranked_by_confidence_not_by_source_order():
    """`_adjudicate_cards` sorts rather than trusting `cards[0]`. It used to rely on the
    port's best-first contract, which cannot make a UNION of two independently-ordered
    lists ordered — and no test forced the contract either way."""
    env = _envelope()
    intent = env.payload["intent"]
    cards = [
        _card("bp-low", tier=TIER_LEARNING, key="sha256:a"),
        _card("bp-high", tier=TIER_LEARNING, key="sha256:b"),
    ]
    index = InMemoryPriorArtIndex(
        cards, scores={(intent, "bp-low"): 0.84, (intent, "bp-high"): 0.99}
    )
    stage = DedupStage(InMemoryBlueprintCorpus(), FakeEmbeddingClient(), prior_art=index)

    result = await stage.process(env, _ctx())

    assert result.envelope.dedup.matched_id == "bp-high"
    assert result.envelope.dedup.action == "merge"


async def test_the_soft_layer_bands_on_confidence_not_the_raw_cosine():
    """A model-skewed card's cosine compares two DIFFERENT vector spaces. Banding on the
    raw score would let a meaningless 0.99 route a genuinely-new blueprint to the inbox as
    a duplicate; banding on `confidence` (discounted) makes it an `insert`."""
    env = _envelope()
    intent = env.payload["intent"]
    skewed = replace(
        _card("bp-stale", tier=TIER_LEARNING, key="sha256:other"),
        embedding_model="text-embedding-3-small",
        model_matched=False,
    )
    index = InMemoryPriorArtIndex([skewed], scores={(intent, "bp-stale"): 0.99})
    stage = DedupStage(InMemoryBlueprintCorpus(), FakeEmbeddingClient(), prior_art=index)

    result = await stage.process(env, _ctx())

    assert result.envelope.dedup.action == "insert"
    assert result.envelope.dedup.similarity < 0.83


async def test_a_soft_canon_near_match_is_reviewed_never_dropped():
    """A cosine over intent PROSE is not an identity claim. Dropping a candidate on one
    would make the loop's most consequential decision on its weakest evidence — canon
    redundancy is settled by the deterministic key (layer 2), not here."""
    env = _envelope()
    intent = env.payload["intent"]
    index = InMemoryPriorArtIndex(
        [_card("bp-canon", tier=TIER_MCP, key="sha256:some-other-key")],
        scores={(intent, "bp-canon"): 0.99},
    )
    stage = DedupStage(InMemoryBlueprintCorpus(), FakeEmbeddingClient(), prior_art=index)

    result = await stage.process(env, _ctx())

    assert result.control == "continue"
    assert result.envelope.dedup.action == "merge"
    assert result.envelope.dedup.action != "redundant_with_canon"


# --- S6-prior-art-fails-open --------------------------------------------------


async def test_an_unavailable_index_leaves_the_bucket_half_of_the_union_working():
    """FAIL-OPEN. Never stop learning because a graph read failed.

    Now that the bucket is a SECOND SOURCE rather than a fallback, the degraded path is a
    strict SUBSET of the healthy one rather than a different code path — which is the
    property that made the regression possible to miss in the first place: the fallback
    still had the right behaviour, it was just unreachable. What is lost while the index
    is down is exactly the graph half (the canon and the landed tier), and that is stated
    in the log rather than inferred."""
    env = _envelope()
    intent = env.payload["intent"]
    other = "total earnings for a department per year"
    corpus = _CountingCorpus(
        [CorpusArtifact(id="artifact-near", canonical_key="sha256:other", intent=other)]
    )
    vec = [0.1, 0.2, 0.3, 0.4]
    stage = DedupStage(
        corpus,
        FakeEmbeddingClient({intent: vec, other: vec}),
        prior_art=InMemoryPriorArtIndex(fail=True),
    )

    result = await stage.process(env, _ctx())

    assert result.control == "continue"  # never a dead-lettered session
    assert corpus.list_calls == 1  # the bucket half still ran
    assert result.envelope.dedup.action == "merge"
    assert result.envelope.dedup.matched_id == "artifact-near"


async def test_an_unavailable_index_does_not_block_the_structural_layer_from_falling_through():
    """A failed structural lookup must be a NON-answer, not a miss and not a hit: the
    stage falls through to the soft layer, which is what "we could not look" should cost."""
    env = _envelope()
    corpus = _CountingCorpus()
    stage = DedupStage(corpus, FakeEmbeddingClient(), prior_art=InMemoryPriorArtIndex(fail=True))

    result = await stage.process(env, _ctx())

    assert result.envelope.dedup.action == "insert"
    assert result.envelope.dedup.layer == "soft"
    assert corpus.seed_calls == [_hard_key(env)]  # the loop kept learning


async def test_no_index_wired_is_byte_identical_to_the_pre_slice_behaviour():
    """`prior_art` is deliberately OUTSIDE the factory's all-or-nothing unit. Absent, the
    stage must behave exactly as it did before — a fully correct, if blinkered, loop."""
    env = _envelope()
    corpus = _CountingCorpus()
    stage = DedupStage(corpus, FakeEmbeddingClient())
    result = await stage.process(env, _ctx())
    assert result.envelope.dedup.action == "insert"
    assert result.envelope.dedup.layer == "soft"
    assert corpus.seed_calls == [_hard_key(env)]


# --- S6-terminal-artifacts-excluded (bucket side) ------------------------------


async def test_the_bucket_half_of_the_union_skips_terminal_artifacts():
    """`CorpusArtifact` gained `status` in slice 1 and the scheduler now writes it. If
    the fallback scan ignored it, a candidate a human explicitly declined would come back
    as prior art forever — which is the same bug on the other side of the port."""
    env = _envelope()
    intent = env.payload["intent"]
    other = "total earnings for a department per year"
    vec = [0.1, 0.2, 0.3, 0.4]
    corpus = InMemoryBlueprintCorpus(
        [
            CorpusArtifact(
                id="artifact-rejected",
                canonical_key="sha256:rejected",
                intent=other,
                status="rejected",
            ),
            CorpusArtifact(
                id="artifact-retired",
                canonical_key="sha256:retired",
                intent=other,
                status="retired",
            ),
        ]
    )
    stage = DedupStage(corpus, FakeEmbeddingClient({intent: vec, other: vec}))

    result = await stage.process(env, _ctx())

    # Both artifacts would have scored a perfect 1.0 and produced a `merge`.
    assert result.envelope.dedup.action == "insert"
    assert result.envelope.dedup.matched_id is None


async def test_a_live_artifact_alongside_a_terminal_one_still_matches():
    """The exclusion must be per-artifact, not "any terminal artifact disables the scan"."""
    env = _envelope()
    intent = env.payload["intent"]
    other = "total earnings for a department per year"
    vec = [0.1, 0.2, 0.3, 0.4]
    corpus = InMemoryBlueprintCorpus(
        [
            CorpusArtifact(id="dead", canonical_key="sha256:dead", intent=other, status="rejected"),
            CorpusArtifact(id="live", canonical_key="sha256:live", intent=other),
        ]
    )
    stage = DedupStage(corpus, FakeEmbeddingClient({intent: vec, other: vec}))
    result = await stage.process(env, _ctx())
    assert result.envelope.dedup.action == "merge"
    assert result.envelope.dedup.matched_id == "live"


# --- structural-key derivation (untrusted rehydrated JSON) --------------------


async def test_a_malformed_generalization_skips_the_structural_layer_without_raising():
    """The generalization is rehydrated JSON. `structural_key_from_templates` sorts node
    templates by `order` (mixed types break `<`) and renders `sql_template` as text, so a
    wrong type there must be an in-band skip, never a dead-lettered session."""
    for broken in (
        {"node_templates": "not-a-list"},
        {"node_templates": [{"order": "first", "sql_template": "SELECT 1"}]},
        {"node_templates": [{"order": True, "sql_template": "SELECT 1"}]},
        {"node_templates": [{"order": 0, "sql_template": None}]},
        {"node_templates": ["not-a-dict"]},
        {"sql_template": 42},
        {"result_grain": "department"},
        {"result_grain": [None]},
    ):
        env = _envelope()
        gen = {**env.payload["generalization"], **broken}
        env = replace(env, payload={**env.payload, "generalization": gen})
        index = InMemoryPriorArtIndex([_card("bp-canon", tier=TIER_MCP, key="sha256:anything")])
        stage = DedupStage(InMemoryBlueprintCorpus(), FakeEmbeddingClient(), prior_art=index)

        result = await stage.process(env, _ctx())  # must NOT raise

        assert result.envelope.dedup is not None
        assert result.envelope.dedup.action != "redundant_with_canon"


async def test_a_candidate_with_no_derivable_key_never_queries_the_index_for_one():
    """An empty structural key must not be sent as a query: it would match every
    un-keyable node in the graph, i.e. "everything already exists"."""
    env = _envelope()
    gen = {**env.payload["generalization"], "sql_template": None, "node_templates": []}
    env = replace(env, payload={**env.payload, "generalization": gen})
    index = InMemoryPriorArtIndex()
    stage = DedupStage(InMemoryBlueprintCorpus(), FakeEmbeddingClient(), prior_art=index)

    await stage.process(env, _ctx())

    assert index.key_calls == []
