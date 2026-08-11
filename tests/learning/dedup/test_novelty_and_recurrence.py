"""S6's two plan-§4 side outputs: the novelty stamp and the dormant recurrence counter.

Both ride along on the soft layer because it is the ONLY place in the pipeline that has
already paid for an embed of the candidate's intent and a query against the graph.

Slugs:
  * S6-novelty-measured-against-LANDED — the graph half only, never sibling candidates.
  * S6-novelty-unmeasured-is-not-novel — a degraded read is reported, never faked.
  * S6-recurrence-counts-paraphrases   — the loose counter the hard key cannot be.
  * S6-recurrence-is-dormant           — accrued now, weighted 0.0 in the gate.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.dedup import (
    CorpusArtifact,
    DedupStage,
    InMemoryBlueprintCorpus,
    compute_canonical_key,
)
from data_agent.learning.priorart import TIER_MCP, InMemoryPriorArtIndex, PriorArtCard
from data_agent.learning.stage import StageContext
from data_agent.learning.summary.models import SessionSummary
from data_agent.learning.triage import TriageVerdict
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"


def _ctx() -> StageContext:
    summary = SessionSummary(
        session_id="sess-fixture", user_id="u1", scope_ref="scope-1",
        trace_id="trace-fixture", content_hash="hash-fixture", turns=(),
        tool_calls=(), blueprint_usages=(), askuser_exchanges=(),
        failed_fixed_sql=(), accepted_signal="no_correction",
    )
    return StageContext(summary=summary, verdict=TriageVerdict(decision="keep", reason="K1"))


def _envelope() -> CandidateEnvelope:
    doc = json.loads((FIXTURES / "s4_enriched_blueprint.json").read_text())
    return CandidateEnvelope.from_doc(doc["single"]["envelope"])


def _intent(env: CandidateEnvelope, text: str) -> CandidateEnvelope:
    payload = dict(env.payload)
    payload["intent"] = text
    return replace(env, payload=payload)


def _hard_key(env: CandidateEnvelope) -> str:
    gen = env.payload["generalization"]
    return compute_canonical_key(
        env.payload.get("resolves") or {},
        gen.get("uses_rules") or [],
        gen.get("result_grain") or {},
        gen["canonical_ast_norm"],
    )


def _graph_card(card_id: str, intent: str) -> PriorArtCard:
    return PriorArtCard(
        id=card_id, kind="blueprint", tier=TIER_MCP, status="validated", verified=True,
        drift_status="clean", intent=intent, result_grain=(), uses_rules=(),
        structural_key="sha256:something-else", embedding_model="all-mpnet-base-v2",
        similarity=0.0, model_matched=True,
    )


# --- novelty ------------------------------------------------------------------


async def test_novelty_is_measured_against_landed_artifacts_only():
    """THE ordering property. If novelty were measured against the union, an in-flight
    SIBLING candidate from a concurrent session would make the second sighting of an idea
    look redundant and the first look novel — an answer that depends on which of two
    concurrent sessions was processed first, and that gets the sign of the evidence
    backwards (corroboration is evidence FOR an idea).

    Here the bucket holds a near-identical sibling and the graph holds nothing. Novelty
    must be maximal and MEASURED."""
    env = _intent(_envelope(), "total earnings by department for a year")
    corpus = InMemoryBlueprintCorpus(
        [
            CorpusArtifact(
                id="sibling",
                canonical_key="sha256:a-different-key",
                intent="total earnings by department for a year",
            )
        ]
    )
    stage = DedupStage(corpus, FakeEmbeddingClient(), prior_art=InMemoryPriorArtIndex([]))

    result = await stage.process(env, _ctx())

    assert result.envelope.novelty is not None
    assert result.envelope.novelty.measured is True
    assert result.envelope.novelty.novelty == 1.0  # the GRAPH holds nothing
    assert result.envelope.novelty.compared_against == 0


async def test_a_close_landed_artifact_drives_novelty_down():
    env = _intent(_envelope(), "total earnings by department")
    index = InMemoryPriorArtIndex(
        [_graph_card("bp::landed", "total earnings by department")],
        scores={("total earnings by department", "bp::landed"): 0.9},
    )
    stage = DedupStage(InMemoryBlueprintCorpus(), FakeEmbeddingClient(), prior_art=index)

    result = await stage.process(env, _ctx())

    assert result.envelope.novelty.measured is True
    assert result.envelope.novelty.novelty == pytest.approx(0.1)
    assert result.envelope.novelty.compared_against == 1


async def test_an_unavailable_graph_reports_unmeasured_never_maximal_novelty():
    """The failure the whole `PriorArtUnavailableError` posture exists for, one layer up.
    "I could not look" and "nothing like this exists" must never render as the same
    number: the second is the strongest possible reason to show a human, and inventing it
    out of an outage would push exactly the wrong candidates to the top of the queue."""
    env = _intent(_envelope(), "total earnings by department")
    stage = DedupStage(
        InMemoryBlueprintCorpus(), FakeEmbeddingClient(),
        prior_art=InMemoryPriorArtIndex([], fail=True),
    )

    result = await stage.process(env, _ctx())

    assert result.envelope.novelty is not None
    assert result.envelope.novelty.measured is False


async def test_no_prior_art_index_wired_leaves_novelty_unmeasured():
    """A deployment with no graph is supported. It must say so rather than claim every
    candidate is a discovery."""
    stage = DedupStage(InMemoryBlueprintCorpus(), FakeEmbeddingClient())

    result = await stage.process(_intent(_envelope(), "anything"), _ctx())

    assert result.envelope.novelty is not None and result.envelope.novelty.measured is False


async def test_a_structural_twin_is_zero_novelty_and_measured():
    """Layer 2 short-circuits the soft layer, so this is the only place a structurally
    redundant candidate can be told apart from one nobody could measure. A structural-key
    hit is an IDENTITY claim against a landed graph node — novelty 0.0, measured."""
    from data_agent.runtime.blueprint.structural_key import structural_key_from_templates

    env = _intent(_envelope(), "total earnings by department")
    gen = env.payload["generalization"]
    key = structural_key_from_templates(gen.get("result_grain"), gen.get("sql_template"), [])
    assert key, "the fixture must produce a structural key for this test to mean anything"
    twin = replace(_graph_card("bp::twin", "a differently-worded intent"), structural_key=key)
    stage = DedupStage(
        InMemoryBlueprintCorpus(), FakeEmbeddingClient(),
        prior_art=InMemoryPriorArtIndex([twin]),
    )

    result = await stage.process(env, _ctx())

    assert result.envelope.novelty == result.envelope.novelty.__class__(
        novelty=0.0, measured=True, compared_against=1
    )


# --- the dormant recurrence counter -------------------------------------------


async def test_a_paraphrase_bumps_the_soft_recurrence_counter():
    """The counter the hard key cannot be. `hit_count` requires byte-identical
    normalized-AST equality, which is why nothing has ever been corroborated; this counts
    "somebody asked this again, in different SQL"."""
    env = _intent(_envelope(), "total earnings by department")
    artifact = CorpusArtifact(
        id="prior", canonical_key="sha256:some-other-key",
        intent="total earnings by department",  # identical text ⇒ cosine 1.0 in the fake
    )
    corpus = InMemoryBlueprintCorpus([artifact])
    stage = DedupStage(corpus, FakeEmbeddingClient(), recurrence_threshold=0.9)

    await stage.process(env, _ctx())

    assert corpus.recurrence_calls == ["sha256:some-other-key"]
    assert corpus.get_sync("sha256:some-other-key").recurrence_count == 1


async def test_a_distant_artifact_is_not_a_recurrence():
    env = _intent(_envelope(), "total earnings by department")
    corpus = InMemoryBlueprintCorpus(
        [
            CorpusArtifact(
                id="prior", canonical_key="sha256:other",
                intent="headcount of contractors joining next quarter",
            )
        ]
    )
    stage = DedupStage(corpus, FakeEmbeddingClient(), recurrence_threshold=0.9)

    await stage.process(env, _ctx())

    assert corpus.recurrence_calls == []


async def test_a_candidates_own_artifact_is_never_counted_as_its_own_recurrence():
    """A redelivery of the same session would otherwise inflate the counter against
    itself — the same self-comparison the caller already excludes for the cosine band."""
    env = _intent(_envelope(), "total earnings by department")
    key = _hard_key(env)
    corpus = InMemoryBlueprintCorpus(
        [CorpusArtifact(id="self", canonical_key=key, intent="total earnings by department")]
    )
    stage = DedupStage(corpus, FakeEmbeddingClient(), recurrence_threshold=0.9)

    await stage.process(env, _ctx())

    assert corpus.recurrence_calls == []


async def test_a_rejected_artifact_never_accrues_recurrence():
    """Negative memory (plan §5): a human declined this idea. Accruing evidence FOR
    re-proposing it is precisely the "the same declined idea returns indefinitely"
    failure the terminal stamp exists to stop."""
    env = _intent(_envelope(), "total earnings by department")
    corpus = InMemoryBlueprintCorpus(
        [
            CorpusArtifact(
                id="declined", canonical_key="sha256:declined",
                intent="total earnings by department", status="rejected",
            )
        ]
    )
    stage = DedupStage(corpus, FakeEmbeddingClient(), recurrence_threshold=0.9)

    await stage.process(env, _ctx())

    assert corpus.recurrence_calls == []


async def test_a_failing_recurrence_write_never_costs_the_candidate():
    """The counter is weighted 0.0 today. It must never be the reason a candidate fails
    to be adjudicated."""

    class _Exploding(InMemoryBlueprintCorpus):
        async def increment_recurrence_count(self, canonical_key: str) -> None:
            raise RuntimeError("couchbase unreachable")

    env = _intent(_envelope(), "total earnings by department")
    corpus = _Exploding(
        [
            CorpusArtifact(
                id="prior", canonical_key="sha256:other",
                intent="total earnings by department",
            )
        ]
    )
    stage = DedupStage(corpus, FakeEmbeddingClient(), recurrence_threshold=0.9)

    result = await stage.process(env, _ctx())

    assert result.control in ("continue", "drop")  # the stage still produced a verdict
    assert result.envelope.dedup is not None


async def test_a_legacy_artifact_with_no_recurrence_field_loads_as_zero():
    """The corpus bucket is durable and is never migrated, so every artifact written
    before plan §4 lacks the key entirely. 0 is the honest reading — no soft recurrence
    has been RECORDED for it, which is different from asserting none happened."""
    legacy = CorpusArtifact.from_doc(
        {"id": "x", "canonical_key": "sha256:k", "intent": "i", "hit_count": 4}
    )
    assert legacy.recurrence_count == 0
