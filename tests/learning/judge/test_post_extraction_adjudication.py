"""The POST-extraction adjudication (plan §3b), inside the S6 dedup stage.

The second judge call runs ONLY when the soft-layer cosine lands in the ambiguous band.
Outside it the answer is obvious and free: below the band nothing is close, above it the
deterministic layers and the merge routing already have an opinion, and paying a model
to confirm either is pure cost.

The bar here is LOWER than the pre-extraction one, deliberately, and the reason is
visible in what each stage is shown — `prompt.py::candidate_brief` carries the
generalized template, the grain and the rule ids, none of which exist yet on the other
side of the extractor.

**Plumbing, not judgement.** The verdicts are scripted; see `test_judge_limits_qa.py`.

Slugs:
  * J-post-band-only          — below/above the band costs NO model call.
  * J-post-insert-is-judged   — an `insert` verdict returns no MATCHED card but the
                                judge still sees the cards the banding rejected; a 0.75
                                near-match is exactly the ambiguous case.
  * J-post-drop               — a `duplicate` at/above the post bar drops the candidate,
                                and it does NOT seed a corpus artifact on the way out.
  * J-post-stamped            — every verdict, drop or not, is stamped on the envelope.
  * J-post-fail-open          — a judge that raises leaves the candidate alone.
  * J-post-never-promotes     — the judge can only remove; it never softens a `merge`.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from data_agent.learning.audit import InMemoryAuditStore
from data_agent.learning.audit.judgement import post_extraction_ref
from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.dedup import DedupStage, InMemoryBlueprintCorpus
from data_agent.learning.judge import CoverageJudge, JudgeConfig
from data_agent.learning.priorart import InMemoryPriorArtIndex
from data_agent.learning.stage import StageContext
from data_agent.learning.triage import TriageVerdict
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
from data_agent.runtime.model.scripted_client import ScriptedModelClient

from .helpers import card, make_envelope, make_summary, verdict_turn

_INTENT = "total earnings for a department"


def _ctx() -> StageContext:
    return StageContext(
        summary=make_summary(), verdict=TriageVerdict(decision="keep", reason="K1")
    )


def _stage(
    turns: list,
    *,
    score: float,
    tier: str = "mcp",
    card_id: str = "bp::abc",
    audit: InMemoryAuditStore | None = None,
    config: JudgeConfig | None = None,
    corpus: InMemoryBlueprintCorpus | None = None,
) -> tuple[DedupStage, ScriptedModelClient, InMemoryAuditStore, InMemoryBlueprintCorpus]:
    """A dedup stage whose SOFT layer will score the one seeded card at *score*.

    The prior-art fake's `scores` map is keyed on the exact query text, which for the
    soft layer is the candidate's `intent` — so the band position is a stated intent
    rather than an artifact of token overlap.
    """
    client = ScriptedModelClient(turns)
    store = audit if audit is not None else InMemoryAuditStore()
    index = InMemoryPriorArtIndex(
        [card(card_id, intent="something else entirely", tier=tier)],
        scores={(_INTENT, card_id): score},
    )
    judge = CoverageJudge(
        client, store, prior_art=index, config=config or JudgeConfig()
    )
    blueprint_corpus = corpus if corpus is not None else InMemoryBlueprintCorpus()
    stage = DedupStage(
        blueprint_corpus,
        FakeEmbeddingClient(),
        prior_art=index,
        judge=judge,
    )
    return stage, client, store, blueprint_corpus


def _keyed_envelope() -> CandidateEnvelope:
    """An envelope that CAN mint the frozen hard key (it carries a
    `canonical_ast_norm` + the three structured inputs), so `_seed_on_insert` has
    something to key on."""
    env = make_envelope()
    gen = env.payload["generalization"]
    return replace(
        env,
        payload={
            **env.payload,
            "resolves": {},
            "generalization": {**gen, "canonical_ast_norm": "SELECT 2"},
        },
    )


async def test_a_score_below_the_band_costs_no_model_call() -> None:
    stage, client, audit, _corpus = _stage([verdict_turn()], score=0.40)
    result = await stage.process(make_envelope(), _ctx())
    assert result.control == "continue"
    assert client.calls_made == 0
    assert audit.judgements == ()
    assert result.envelope.judge is None


async def test_a_score_above_the_band_costs_no_model_call() -> None:
    """Above 0.97 the deterministic layers and the merge routing already have an
    opinion; a model asked to confirm it is spending money on a settled question."""
    stage, client, audit, _corpus = _stage([verdict_turn()], score=0.99)
    result = await stage.process(make_envelope(), _ctx())
    assert client.calls_made == 0
    assert audit.judgements == ()
    # And dedup's own verdict is untouched: 0.99 is above the merge threshold.
    assert result.envelope.dedup is not None
    assert result.envelope.dedup.action == "merge"


async def test_an_insert_band_near_match_is_still_judged() -> None:
    """0.75 sits below the conflict threshold, so `_adjudicate_cards` returns NO matched
    card and dedup calls it `insert`. That is precisely the ambiguous case the judge is
    for, which is why the stage hands over the whole union rather than the matched
    card."""
    stage, client, _audit, _corpus = _stage([verdict_turn("new", covered_by="")], score=0.75)
    result = await stage.process(make_envelope(), _ctx())
    assert client.calls_made == 1
    assert result.envelope.dedup is not None
    assert result.envelope.dedup.action == "insert"
    assert result.control == "continue"


async def test_a_duplicate_in_band_drops_the_candidate() -> None:
    stage, _client, audit, corpus = _stage(
        [verdict_turn("duplicate", covered_by="bp::abc", confidence=0.80)], score=0.90
    )
    env = make_envelope()
    result = await stage.process(env, _ctx())
    assert result.control == "drop"

    (stored,) = audit.judgements
    # CONTENT-keyed. The candidate_id is still ON the row (a human needs it), but it is
    # deliberately not the key — a re-extraction can reorder candidates and an id-keyed
    # cache would then serve one candidate a verdict rendered about another.
    assert stored.judgement_ref == post_extraction_ref(stored.fingerprint)
    assert env.candidate_id not in stored.judgement_ref
    assert stored.dropped is True
    assert stored.stage == "post_extraction"
    assert stored.candidate_id == env.candidate_id
    assert stored.threshold == 0.75


async def test_a_dropped_candidate_never_seeds_a_corpus_artifact() -> None:
    """`_seed_on_insert` registers an artifact at `hit_count=1` so the cross-session
    counter can accrue. Seeding one for work we are discarding would leave a counter
    ticking for a blueprint nobody kept — and, worse, make it visible as in-flight prior
    art to the next session."""
    corpus = InMemoryBlueprintCorpus()
    stage, _client, _audit, _corpus = _stage(
        [verdict_turn("duplicate", covered_by="bp::abc", confidence=0.90)],
        score=0.75,  # the `insert` band, where seeding would otherwise happen
        corpus=corpus,
    )
    result = await stage.process(_keyed_envelope(), _ctx())
    assert result.control == "drop"
    assert await corpus.list_artifacts() == []


async def test_a_surviving_candidate_still_seeds_as_before() -> None:
    """The control for the test above. `_seed_on_insert` needs a real hard key, so this
    envelope carries a `canonical_ast_norm` the other one does not — otherwise "nothing
    was seeded" would be true for both and would prove nothing about the judge."""
    corpus = InMemoryBlueprintCorpus()
    stage, _client, _audit, _corpus = _stage(
        [verdict_turn("new", covered_by="")], score=0.75, corpus=corpus
    )
    result = await stage.process(_keyed_envelope(), _ctx())
    assert result.control == "continue"
    assert len(await corpus.list_artifacts()) == 1


async def test_the_verdict_is_stamped_on_the_envelope_even_when_it_does_not_drop() -> None:
    """`new` and `existing-plus-delta` on a SURVIVING candidate are the two most useful
    rows in the dataset, and a human reading the review inbox wants to see that the
    machine already had an opinion about novelty."""
    stage, _client, _audit, _corpus = _stage(
        [verdict_turn("existing-plus-delta", covered_by="bp::abc", confidence=0.66)],
        score=0.90,
    )
    result = await stage.process(make_envelope(), _ctx())
    assert result.control == "continue"
    assert result.envelope.judge is not None
    assert result.envelope.judge.verdict == "existing-plus-delta"
    assert result.envelope.judge.covered_by_tier == "mcp"


async def test_the_stamped_verdict_round_trips_through_the_envelope_doc() -> None:
    stage, _client, _audit, _corpus = _stage(
        [verdict_turn("existing-plus-delta", covered_by="bp::abc", confidence=0.66)],
        score=0.90,
    )
    result = await stage.process(make_envelope(), _ctx())
    doc = result.envelope.to_doc()
    assert doc["judge"]["verdict"] == "existing-plus-delta"
    assert CandidateEnvelope.from_doc(doc).judge == result.envelope.judge


async def test_a_pre_slice_envelope_doc_round_trips_byte_identically() -> None:
    """Additive + OPTIONAL, mirroring `traceparent`/`verified`/`last_scanned_at`: the
    absence of the key means "the judge did not run", which must stay distinguishable
    from a stored `new` verdict."""
    env = make_envelope()
    assert "judge" not in env.to_doc()
    assert CandidateEnvelope.from_doc(env.to_doc()).judge is None


async def test_an_unlanded_in_flight_sibling_can_never_authorize_a_drop() -> None:
    """`PriorArtCard.origin` already states that a `learning_corpus` card "can at most
    route to a human": it is usually an IN-FLIGHT sibling candidate that has not landed
    and may yet be rejected, dropped, or fail its landing gates. Discarding an analyst's
    session because of an artifact that might never exist is a weaker basis than any
    other drop this system takes, so the judge honours the same rule the structural
    layer does."""
    from dataclasses import replace as _replace

    bucket_card = _replace(card("bp::inflight", tier="learning"), origin="corpus")
    client = ScriptedModelClient(
        [verdict_turn("duplicate", covered_by="bp::inflight", confidence=1.0)]
    )
    audit = InMemoryAuditStore()
    index = InMemoryPriorArtIndex(
        [bucket_card], scores={(_INTENT, "bp::inflight"): 0.90}
    )
    stage = DedupStage(
        InMemoryBlueprintCorpus(),
        FakeEmbeddingClient(),
        prior_art=index,
        judge=CoverageJudge(client, audit, prior_art=index, config=JudgeConfig()),
    )
    result = await stage.process(make_envelope(), _ctx())
    assert result.control == "continue"
    # Still recorded, so the rate of "we nearly dropped on an unlanded sibling" is
    # visible rather than silently swallowed.
    assert audit.judgements[0].dropped is False
    assert audit.judgements[0].assessment.verdict == "duplicate"


async def test_a_judge_that_raises_leaves_the_candidate_alone() -> None:
    class _BoomJudge:
        async def adjudicate_candidate(self, env, summary, cards):
            raise RuntimeError("judge exploded")

    stage = DedupStage(
        InMemoryBlueprintCorpus(),
        FakeEmbeddingClient(),
        prior_art=InMemoryPriorArtIndex([card()]),
        judge=_BoomJudge(),  # type: ignore[arg-type]
    )
    result = await stage.process(make_envelope(), _ctx())
    assert result.control == "continue"
    assert result.envelope.judge is None


async def test_no_judge_wired_behaves_exactly_as_before_the_slice() -> None:
    stage = DedupStage(
        InMemoryBlueprintCorpus(),
        FakeEmbeddingClient(),
        prior_art=InMemoryPriorArtIndex([card()]),
    )
    result = await stage.process(make_envelope(), _ctx())
    assert result.control == "continue"
    assert result.envelope.judge is None


async def test_the_judge_never_softens_a_merge_into_an_auto_land() -> None:
    """The invariant: the judge may only REMOVE work, never advance it past a gate. A
    `new` verdict on a candidate dedup banded as `merge` leaves the `merge` standing, so
    the writer still routes it to a human."""
    stage, _client, _audit, _corpus = _stage(
        [verdict_turn("new", covered_by="")], score=0.96
    )
    result = await stage.process(make_envelope(), _ctx())
    assert result.envelope.dedup is not None
    assert result.envelope.dedup.action == "merge"
    assert result.control == "continue"


@pytest.mark.parametrize(
    ("status", "control"), [("extracted", "continue"), ("rejected", "drop")]
)
async def test_a_hard_key_hit_short_circuits_before_the_judge_ever_runs(
    status: str, control: str
) -> None:
    """The deterministic, race-safe SHA-256 layer stays the first layer and is never
    displaced. A byte-identical re-derivation is settled by ARITHMETIC — no model call and
    no judgement record — whichever way it is settled.

    Which way that is depends on the matched artifact, and both are asserted here so the
    short-circuit cannot be read as a property of the drop alone: a hit on a TERMINAL
    artifact drops (the human said no to exactly this thing), a hit on a LIVE one
    continues to the writer as a suppressed duplicate. Neither consults the judge."""
    from data_agent.learning.dedup import CorpusArtifact, compute_canonical_key

    env = make_envelope()
    gen = env.payload["generalization"]
    key = compute_canonical_key(
        {}, gen["uses_rules"], gen["result_grain"], "SELECT 1"
    )
    env = replace(
        env,
        payload={
            **env.payload,
            "resolves": {},
            "generalization": {**gen, "canonical_ast_norm": "SELECT 1"},
        },
    )
    corpus = InMemoryBlueprintCorpus()
    await corpus.seed_artifact(
        CorpusArtifact(
            id="art-1", canonical_key=key, intent=_INTENT, hit_count=1, status=status
        )
    )
    stage, client, audit, _corpus = _stage(
        [verdict_turn("duplicate")], score=0.90, corpus=corpus
    )
    result = await stage.process(env, _ctx())
    assert result.control == control
    assert result.envelope.dedup is not None
    assert result.envelope.dedup.layer == "hard"
    assert client.calls_made == 0
    assert audit.judgements == ()
