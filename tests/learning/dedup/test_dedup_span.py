"""S6 dedup — the `learning.dedup` span that makes canon rediscovery a COUNT.

The plan asks for `redundant_with_canon` to be COUNTED, not merely logged, and the
reason is that the number means something quite different from what it looks like: a
high rate is a RETRIEVAL defect surfacing in the learning loop. The agent HAD a
blueprint for the question, failed to recall it, the analyst hand-wrote the SQL, and the
loop then re-derived what we already own. A log line is not a rate; a span attribute is.

Slug: S6-dedup-verdict-is-countable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.dedup import CorpusArtifact, DedupStage, InMemoryBlueprintCorpus
from data_agent.learning.priorart import TIER_MCP, InMemoryPriorArtIndex, PriorArtCard
from data_agent.learning.stage import StageContext
from data_agent.learning.summary.models import SessionSummary
from data_agent.learning.triage import TriageVerdict
from data_agent.runtime.blueprint.structural_key import structural_key_from_templates
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    exp._provider = provider  # keep the provider alive for the test
    return exp


@pytest.fixture
def tracer(exporter):
    return exporter._provider.get_tracer("learning-loop-test")


def _ctx() -> StageContext:
    return StageContext(
        summary=SessionSummary(
            session_id="s", user_id="u", scope_ref="sc", trace_id="t", content_hash="h",
            turns=(), tool_calls=(), blueprint_usages=(), askuser_exchanges=(),
            failed_fixed_sql=(), accepted_signal="no_correction",
        ),
        verdict=TriageVerdict(decision="keep", reason="K1"),
    )


def _envelope() -> CandidateEnvelope:
    doc = json.loads((FIXTURES / "s4_enriched_blueprint.json").read_text())
    return CandidateEnvelope.from_doc(doc["single"]["envelope"])


def _structural_key(env: CandidateEnvelope) -> str:
    gen = env.payload["generalization"]
    return structural_key_from_templates(
        gen.get("result_grain"),
        gen.get("sql_template"),
        [(n["order"], n["sql_template"]) for n in gen.get("node_templates") or []],
    )


def _card(id_: str, *, tier: str = TIER_MCP, key: str = "") -> PriorArtCard:
    return PriorArtCard(
        id=id_, kind="blueprint", tier=tier, status="validated", verified=True,  # type: ignore[arg-type]
        drift_status="clean", intent="an existing blueprint", result_grain=(),
        uses_rules=(), structural_key=key, embedding_model="all-mpnet-base-v2",
        similarity=0.0, model_matched=True,
    )


def _attrs(exporter) -> dict:
    spans = [s for s in exporter.get_finished_spans() if s.name == "learning.dedup"]
    assert len(spans) == 1, [s.name for s in exporter.get_finished_spans()]
    return dict(spans[0].attributes)


async def test_a_canon_redundancy_drop_is_emitted_as_a_countable_span(exporter, tracer):
    env = _envelope()
    index = InMemoryPriorArtIndex([_card("bp-canon", key=_structural_key(env))])
    stage = DedupStage(
        InMemoryBlueprintCorpus(), FakeEmbeddingClient(), prior_art=index, tracer=tracer
    )

    await stage.process(env, _ctx())

    attrs = _attrs(exporter)
    assert attrs["learning.dedup.action"] == "redundant_with_canon"
    assert attrs["learning.dedup.layer"] == "structural"
    assert attrs["learning.dedup.prior_art_tier"] == "mcp"
    assert attrs["session.id"] == env.source_session


async def test_a_soft_canon_near_match_is_countable_too(exporter, tracer):
    """The SOFTER signal, on the same measurement. `action=merge AND
    prior_art_tier=mcp` is a PROBABLE canon rediscovery — routed to a human rather than
    dropped, but part of the same retrieval-defect rate."""
    env = _envelope()
    intent = env.payload["intent"]
    index = InMemoryPriorArtIndex(
        [_card("bp-canon", key="sha256:unrelated")], scores={(intent, "bp-canon"): 0.99}
    )
    stage = DedupStage(
        InMemoryBlueprintCorpus(), FakeEmbeddingClient(), prior_art=index, tracer=tracer
    )

    await stage.process(env, _ctx())

    attrs = _attrs(exporter)
    assert attrs["learning.dedup.action"] == "merge"
    assert attrs["learning.dedup.layer"] == "soft"
    assert attrs["learning.dedup.prior_art_tier"] == "mcp"


async def test_the_tier_attribute_is_always_present_even_with_no_prior_art_match(
    exporter, tracer
):
    """Always set (to `""`, never omitted) so a Phoenix filter never has to special-case
    a missing key — the difference between "0 canon rediscoveries" and "no data"."""
    stage = DedupStage(InMemoryBlueprintCorpus(), FakeEmbeddingClient(), tracer=tracer)
    await stage.process(_envelope(), _ctx())
    attrs = _attrs(exporter)
    assert attrs["learning.dedup.action"] == "insert"
    assert attrs["learning.dedup.prior_art_tier"] == ""
    assert attrs["learning.dedup.matched_status"] == ""
    assert attrs["learning.dedup.matched_origin"] == ""


async def test_a_bucket_match_is_attributed_to_the_corpus_origin(exporter, tracer):
    """The concurrency signal. A soft match on an IN-FLIGHT sibling — visible only in
    `learning_corpus`, never in the graph — must be distinguishable from a match against
    something already landed, or "the union's bucket half is doing all the work" is
    invisible."""
    env = _envelope()
    intent = env.payload["intent"]
    sibling = "the same question, phrased differently"
    vec = [0.3, 0.4, 0.5]
    corpus = InMemoryBlueprintCorpus(
        [CorpusArtifact(id="cand-sibling", canonical_key="sha256:sib", intent=sibling)]
    )
    stage = DedupStage(
        corpus,
        FakeEmbeddingClient({intent: vec, sibling: vec}),
        prior_art=InMemoryPriorArtIndex([]),
        tracer=tracer,
    )

    await stage.process(env, _ctx())

    attrs = _attrs(exporter)
    assert attrs["learning.dedup.action"] == "merge"
    assert attrs["learning.dedup.matched_origin"] == "corpus"


async def test_a_graph_match_is_attributed_to_the_graph_origin(exporter, tracer):
    env = _envelope()
    intent = env.payload["intent"]
    index = InMemoryPriorArtIndex(
        [_card("bp-landed", key="sha256:sk")], scores={(intent, "bp-landed"): 0.99}
    )
    stage = DedupStage(
        InMemoryBlueprintCorpus(), FakeEmbeddingClient(), prior_art=index, tracer=tracer
    )

    await stage.process(env, _ctx())

    assert _attrs(exporter)["learning.dedup.matched_origin"] == "graph"


def _hard_key(env: CandidateEnvelope) -> str:
    from data_agent.learning.dedup import compute_canonical_key

    gen = env.payload["generalization"]
    return compute_canonical_key(
        env.payload["resolves"], gen["uses_rules"], gen["result_grain"], gen["canonical_ast_norm"]
    )


async def test_a_hard_key_increment_is_counted_on_the_same_span(exporter, tracer):
    env = _envelope()
    key = _hard_key(env)
    corpus = InMemoryBlueprintCorpus([CorpusArtifact(id="a1", canonical_key=key, intent="x")])
    stage = DedupStage(corpus, FakeEmbeddingClient(), tracer=tracer)

    await stage.process(env, _ctx())

    attrs = _attrs(exporter)
    assert attrs["learning.dedup.action"] == "increment"
    assert attrs["learning.dedup.layer"] == "hard"
    assert attrs["learning.dedup.matched_status"] == "extracted"  # a LIVE artifact


@pytest.mark.parametrize("status", ["rejected", "retired"])
async def test_re_deriving_a_declined_idea_is_a_separately_countable_increment(
    exporter, tracer, status
):
    """The third rate the span exists for. `get_by_canonical_key` deliberately does NOT
    filter terminal status — a byte-identical re-derivation of an idea a human REJECTED
    is the same idea, and dropping it is right. But "how often do people re-derive things
    we've already declined" is a datapoint the plan explicitly wants, and without
    `matched_status` it is indistinguishable from an ordinary hit-count bump against a
    live artifact. The two say opposite things about the loop."""
    env = _envelope()
    key = _hard_key(env)
    corpus = InMemoryBlueprintCorpus(
        [CorpusArtifact(id="a1", canonical_key=key, intent="x", status=status)]
    )
    stage = DedupStage(corpus, FakeEmbeddingClient(), tracer=tracer)

    result = await stage.process(env, _ctx())

    # Still DROPPED — the human said no to exactly this thing.
    assert result.control == "drop"
    assert result.envelope.dedup.action == "increment"
    attrs = _attrs(exporter)
    assert attrs["learning.dedup.matched_status"] == status


async def test_the_matched_status_of_a_prior_art_card_is_carried_too(exporter, tracer):
    """Not only the hard layer: a structural or soft match reports the matched artifact's
    status as well, so the attribute is uniformly queryable across all three layers."""
    env = _envelope()
    index = InMemoryPriorArtIndex([_card("bp-canon", key=_structural_key(env))])
    stage = DedupStage(
        InMemoryBlueprintCorpus(), FakeEmbeddingClient(), prior_art=index, tracer=tracer
    )

    await stage.process(env, _ctx())

    assert _attrs(exporter)["learning.dedup.matched_status"] == "validated"


async def test_the_span_is_shape_only_and_carries_no_content(exporter, tracer):
    """No `verbose` gate here, deliberately: there is nothing entity-bearing to gate, and
    a verbose branch would only create a place for someone to put the intent later. This
    asserts the absence, which is the part a future edit would break."""
    env = _envelope()
    stage = DedupStage(InMemoryBlueprintCorpus(), FakeEmbeddingClient(), tracer=tracer)
    await stage.process(env, _ctx())

    serialized = json.dumps(_attrs(exporter))
    assert env.payload["intent"] not in serialized
    assert env.payload["generalization"]["canonical_ast_norm"] not in serialized
    assert (env.extractor_rationale or "zzz-unset") not in serialized


async def test_no_tracer_wired_emits_nothing_and_still_adjudicates(exporter):
    stage = DedupStage(InMemoryBlueprintCorpus(), FakeEmbeddingClient())
    result = await stage.process(_envelope(), _ctx())
    assert result.envelope.dedup.action == "insert"
    assert exporter.get_finished_spans() == ()
