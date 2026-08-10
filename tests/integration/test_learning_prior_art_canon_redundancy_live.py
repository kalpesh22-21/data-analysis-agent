"""Layer-2 integration — does the cross-tier prior-art layer ACTUALLY FIRE against the
real seeded canon? (PriorArt Slice 2, QA items 1/3/5.)

`tests/integration/test_learning_prior_art_live.py` proves the READER works, but it
proves it against six hand-written `patest-` nodes whose `structural_key` the TEST wrote.
That leaves the question the whole slice turns on unanswered: does a real learning
candidate, adjudicated by the real `DedupStage`, get dropped against a real MCP-canon
blueprint written by the real seeder? Every link in that chain is a place the key can
silently fail to match, and none of them are exercised by a test that seeds its own key:

  * the SEEDER must derive and persist a `structural_key` at all (the graph predated the
    writer once — a node with no key is invisible to a layer that looks keys up, and
    NOTHING in the reader's own suite would notice);
  * the seeder's derivation (canon YAML: `SUM(...)` uppercase, `result_grain:
    [Department]` a bare capitalised list) and the stage's derivation (LLM-authored
    `sum(...)` lowercase, the D56 `{columns: [department], verifiable: true}` stamp) must
    render the SAME digest across those systematic authoring differences;
  * the stage must route that hit to `redundant_with_canon` + `drop`.

So this module seeds the REAL fixture corpus through the REAL production writer
(`load_corpus`, additive `gc=False`, MERGE-by-id — the same call
`scripts/seed_neo4j_corpus.py` makes) and then drives the REAL `DedupStage` over the
REAL `Neo4jPriorArtIndex`. Nothing is hand-stamped and nothing is deleted; the seed is
idempotent, so this test also IS the recipe for putting the graph into a state where the
feature can fire.

    docker compose -f docker-compose.integration.yml up -d --wait neo4j embedding-api
    NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_USER=neo4j \
    NEO4J_TEST_PASSWORD=testpassword \
    EMBEDDING_TEST_URL=http://localhost:18003/embed \
        uv run pytest tests/integration/test_learning_prior_art_canon_redundancy_live.py -v
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from neo4j import AsyncGraphDatabase

from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.dedup import CorpusArtifact, DedupStage, InMemoryBlueprintCorpus
from data_agent.learning.priorart.neo4j_index import Neo4jPriorArtIndex
from data_agent.learning.stage import StageContext
from data_agent.learning.summary.models import SessionSummary
from data_agent.learning.triage import TriageVerdict
from data_agent.runtime.blueprint.structural_key import (
    structural_key_from_templates,
    structural_key_recipe,
)
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.retrieval.corpus_loader import (
    _seed_structural_key,
    load_corpus,
    load_seed_fixtures,
    resolve_blueprint_references,
)

pytestmark = pytest.mark.skipif(
    not (os.environ.get("NEO4J_TEST_URI") and os.environ.get("EMBEDDING_TEST_URL")),
    reason="Requires a live neo4j + embedding API (set NEO4J_TEST_URI + EMBEDDING_TEST_URL).",
)

_MODEL = "all-mpnet-base-v2"
_CORPUS_FIXTURES = Path(__file__).parents[1] / "fixtures" / "corpus"
_LEARNING_FIXTURES = Path(__file__).parents[1] / "fixtures" / "learning"

# The canon blueprint this module re-derives. Chosen because it is the WORST case for the
# cross-tier key rather than the easiest: it aggregates (`SUM` uppercase in the YAML), it
# joins two tables, and its `result_grain` is the capitalised bare list `[ Department ]`
# against the learning tier's lowercase `{columns: [...], verifiable: bool}` stamp. If the
# fold and the grain normalization are not both right, this one misses.
_CANON_ID = "bp-overtime-by-department"

# The SAME query as `_CANON_ID`, authored the way the S4 extractor authors: one line,
# lowercase `sum(...)`, and the D56 grain stamp. Byte-different from the YAML in every
# way except meaning.
_LLM_AUTHORED_SQL = (
    "SELECT e.department_name AS department, sum(p.amount) AS earnings "
    "FROM dbpcm_warehouse.payroll AS p "
    "JOIN dbpcm_warehouse.employee AS e ON e.employee_code = p.employee_code "
    "WHERE p.register_type = 'EARN' AND p.type_code = {type_code} "
    "AND e.department_name = {department} AND p.pay_period_end_date = {pay_period} "
    "GROUP BY e.department_name"
)
_LLM_GRAIN = {"columns": ["department"], "verifiable": True}

# A structurally DIFFERENT query with a near-identical intent — the control for item 5.
_UNRELATED_SQL = (
    "SELECT e.department_name AS department, count(DISTINCT p.employee_code) AS paid "
    "FROM dbpcm_warehouse.payroll AS p "
    "JOIN dbpcm_warehouse.employee AS e ON e.employee_code = p.employee_code "
    "WHERE p.pay_period_end_date = {pay_period} "
    "GROUP BY e.department_name"
)


def _uri() -> str:
    return os.environ["NEO4J_TEST_URI"]


def _auth() -> tuple[str, str]:
    return (
        os.environ.get("NEO4J_TEST_USER", "neo4j"),
        os.environ.get("NEO4J_TEST_PASSWORD", "testpassword"),
    )


def _embedder() -> HttpEmbeddingClient:
    return HttpEmbeddingClient(
        url=os.environ["EMBEDDING_TEST_URL"], api_key="", model=_MODEL
    )


@pytest.fixture
async def seeded_canon():
    """Seed the real fixture canon through the real writer, yield the driver + embedder.

    ADDITIVE and IDEMPOTENT: `gc=False` never deletes, `corpus_sha=""` never skips and
    never stamps the `:CorpusMeta` singleton (so this fixture cannot make an unrelated
    B1 self-heal think the corpus changed), and every write is a MERGE by id. Teardown
    removes nothing, because everything written here IS the corpus the graph is supposed
    to hold — unlike the `MATCH (n) DETACH DELETE n` suites, this one leaves the graph
    strictly more coherent than it found it."""
    driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
    embedder = _embedder()
    blueprints, knowledge = load_seed_fixtures(_CORPUS_FIXTURES)
    await load_corpus(
        driver,
        embedder,
        blueprints,
        knowledge,
        model_id=_MODEL,
        ensure_schema=True,
        corpus_sha="",
        gc=False,
    )
    try:
        yield driver, embedder
    finally:
        await driver.close()


def _index(driver, embedder, **kwargs) -> Neo4jPriorArtIndex:
    return Neo4jPriorArtIndex(
        driver=driver, embedding_client=embedder, expected_model=_MODEL, **kwargs
    )


def _ctx() -> StageContext:
    summary = SessionSummary(
        session_id="sess-live",
        user_id="u1",
        scope_ref="scope-1",
        trace_id="trace-live",
        content_hash="hash-live",
        turns=(),
        tool_calls=(),
        blueprint_usages=(),
        askuser_exchanges=(),
        failed_fixed_sql=(),
        accepted_signal="no_correction",
    )
    return StageContext(summary=summary, verdict=TriageVerdict(decision="keep", reason="K1"))


def _candidate(*, sql: str, grain: dict, intent: str) -> CandidateEnvelope:
    """A real S4 candidate envelope with its generalization swapped for *sql*/*grain*.

    `canonical_ast_norm` is deliberately left as the fixture's own value: the FROZEN hard
    key must MISS (it cannot match across authoring paths — that is the premise of the
    whole slice), so the candidate has to reach layer 2 on its own merits."""
    doc = json.loads((_LEARNING_FIXTURES / "s4_enriched_blueprint.json").read_text())
    env = CandidateEnvelope.from_doc(doc["single"]["envelope"])
    payload = dict(env.payload)
    gen = dict(payload["generalization"])
    gen["sql_template"] = sql
    gen["result_grain"] = grain
    gen["uses_rules"] = ["gross_earnings"]
    payload["generalization"] = gen
    payload["intent"] = intent
    return replace(env, payload=payload)


# --- item 1a: the feature is not inert -----------------------------------------


async def test_every_seeded_canon_blueprint_carries_a_structural_key(seeded_canon):
    """THE inertness probe. `get_by_structural_key` is the only layer that can emit the
    `redundant_with_canon` drop, and it is an equality match on a node property: a canon
    tier with no `structural_key` makes the feature silently unreachable — every lookup
    misses, every candidate looks novel, and no log fires because a miss is a normal
    answer.

    Asserted over the WHOLE canon rather than one node, and against a freshly re-derived
    key rather than a remembered constant, so it also catches the half-failure: a corpus
    seeded by an OLDER recipe whose stored digests no longer match what the running code
    computes (which is exactly the split `structural_key_recipe` was stamped to make
    visible).

    References are resolved before re-deriving, exactly as `load_corpus` does (plan
    §2b). Skipping that step made this test fail against a CORRECTLY seeded graph: a
    `composes` node may name another blueprint rather than carry SQL, so the unresolved
    fixture mints a key over the composite's remaining nodes only — a different query
    from the one the loader keyed and the executor runs."""
    driver, _ = seeded_canon
    blueprints, _knowledge = load_seed_fixtures(_CORPUS_FIXTURES)
    expected = {bp.id: _seed_structural_key(bp) for bp in resolve_blueprint_references(blueprints)}
    assert all(expected.values()), "a fixture blueprint mints no key at all"

    async with driver.session() as session:
        result = await session.run(  # type: ignore[arg-type]
            "MATCH (b:Blueprint) WHERE b.source = 'mcp' "
            "RETURN b.id AS id, b.structural_key AS key, "
            "b.structural_key_recipe AS recipe"
        )
        stored = {row["id"]: row for row in await result.data()}

    for bp_id, key in expected.items():
        row = stored.get(bp_id)
        assert row is not None, f"{bp_id} is not in the graph"
        assert row["key"] == key, (
            f"{bp_id}: stored structural_key does not match the running recipe — "
            "cross-tier prior art is silently dead for this blueprint"
        )
        assert row["recipe"] == structural_key_recipe()


async def test_the_seeder_and_the_stage_mint_the_same_key_across_authoring_paths(
    seeded_canon,
):
    """The two derivations meet here and nowhere else. The canon YAML and the LLM
    template differ in casing, whitespace, line count AND grain shape; the digest must
    not. Compared against the value the GRAPH holds, not against a second in-process
    computation, so a seeder that writes a different key than it derives is caught."""
    driver, _ = seeded_canon
    candidate_key = structural_key_from_templates(_LLM_GRAIN, _LLM_AUTHORED_SQL, [])
    assert candidate_key, "the LLM-authored template mints no key"

    async with driver.session() as session:
        result = await session.run(  # type: ignore[arg-type]
            "MATCH (b:Blueprint {id: $id}) RETURN b.structural_key AS key",
            {"id": _CANON_ID},
        )
        rows = await result.data()

    assert rows and rows[0]["key"] == candidate_key


# --- item 1b: it fires, end to end ---------------------------------------------


async def test_a_candidate_rederiving_a_canon_blueprint_is_dropped_end_to_end(
    seeded_canon,
):
    """The claim the slice exists to make good, with no fake in the path: real candidate
    → real `DedupStage` → real `Neo4jPriorArtIndex` → real seeded canon node → DROP.

    The hard key MISSES (the canon has no `resolves`/`uses_rules` to hash and the
    candidate's `canonical_ast_norm` is its own), so this can only be layer 2."""
    driver, embedder = seeded_canon
    corpus = InMemoryBlueprintCorpus()
    stage = DedupStage(
        corpus, embedder, prior_art=_index(driver, embedder)
    )
    env = _candidate(
        sql=_LLM_AUTHORED_SQL,
        grain=_LLM_GRAIN,
        intent="earnings by department for a pay period filtered to overtime",
    )

    result = await stage.process(env, _ctx())

    assert result.control == "drop"
    verdict = result.envelope.dedup
    assert verdict is not None
    assert verdict.action == "redundant_with_canon"
    assert verdict.layer == "structural"
    assert verdict.matched_id == _CANON_ID
    assert verdict.similarity == 1.0
    # NOTHING was written to the learning corpus: no hit count invented for a
    # git-versioned blueprint, no artifact seeded that would later look promotable.
    assert corpus.increment_calls == []
    assert corpus.seed_calls == []


async def test_the_dropped_candidate_would_have_been_a_novel_insert_without_the_index(
    seeded_canon,
):
    """The counterfactual that sizes the bug. The SAME candidate, with the prior-art
    index simply not wired (a supported configuration, and the ONLY configuration before
    this slice), is adjudicated a brand-new blueprint and seeded into the corpus bucket
    to start accruing hits toward promotion. That is the duplicate the loop used to
    mint."""
    _driver, embedder = seeded_canon
    corpus = InMemoryBlueprintCorpus()
    stage = DedupStage(corpus, embedder, prior_art=None)
    env = _candidate(
        sql=_LLM_AUTHORED_SQL,
        grain=_LLM_GRAIN,
        intent="earnings by department for a pay period filtered to overtime",
    )

    result = await stage.process(env, _ctx())

    assert result.control == "continue"
    assert result.envelope.dedup.action == "insert"
    assert len(corpus.seed_calls) == 1


# --- item 5, live: a soft hit on the canon must not drop -----------------------


async def test_a_structurally_different_query_with_a_canon_like_intent_never_drops(
    seeded_canon,
):
    """The dangerous near-miss, over a REAL vector index: an intent phrased almost
    exactly like a canon blueprint's, but counting distinct employees rather than summing
    amounts — a genuinely different question. Its structural key misses, so the only
    evidence is a real cosine against real canon embeddings, and no cosine may drop."""
    driver, embedder = seeded_canon
    stage = DedupStage(
        InMemoryBlueprintCorpus(), embedder, prior_art=_index(driver, embedder)
    )
    env = _candidate(
        sql=_UNRELATED_SQL,
        grain=_LLM_GRAIN,
        intent=(
            "Earnings by department for a given pay period, filtered to a specific "
            "pay type (type_code) such as overtime"
        ),
    )

    result = await stage.process(env, _ctx())

    assert result.control == "continue"
    verdict = result.envelope.dedup
    assert verdict.action != "redundant_with_canon"
    assert verdict.layer == "soft"
    # The intent is the canon blueprint's own text, so the cosine really is at the top of
    # the band — this is the highest-evidence soft hit the corpus can produce, and it
    # still only routes to a human.
    assert verdict.action in {"merge", "conflict"}
    assert verdict.similarity >= 0.83


# --- item 3, live: unavailable ≠ "nothing exists" ------------------------------


async def _dead_index(embedder) -> tuple[Neo4jPriorArtIndex, object]:
    driver = AsyncGraphDatabase.driver(
        "bolt://127.0.0.1:1",
        auth=("neo4j", "nope"),
        connection_timeout=2.0,
        connection_acquisition_timeout=2.0,
        max_transaction_retry_time=2.0,
    )
    return (
        Neo4jPriorArtIndex(
            driver=driver, embedding_client=embedder, expected_model=_MODEL
        ),
        driver,
    )


async def test_an_unreachable_graph_leaves_the_loop_running_on_the_corpus_bucket(
    seeded_canon, caplog
):
    """Fail-OPEN, measured end to end rather than at the port. With the graph down the
    stage must fall back to EXACTLY the pre-slice behaviour — the brute-force
    `learning_corpus` scan — and must log loudly enough that the blind window is visible
    in an incident.

    The bucket carries a near-duplicate of this candidate's intent, so a working fallback
    produces a `merge` from the BUCKET; a fallback that silently treated "unavailable" as
    "nothing found" would produce `insert` and mint the duplicate."""
    _driver, embedder = seeded_canon
    corpus = InMemoryBlueprintCorpus(
        [
            CorpusArtifact(
                id="artifact-in-flight",
                canonical_key="sha256:some-other-key",
                intent="earnings by department for a pay period filtered to overtime",
            )
        ]
    )
    index, dead_driver = await _dead_index(embedder)
    stage = DedupStage(corpus, embedder, prior_art=index)
    env = _candidate(
        sql=_LLM_AUTHORED_SQL,
        grain=_LLM_GRAIN,
        intent="earnings by department for a pay period filtered to overtime",
    )

    try:
        with caplog.at_level("WARNING", logger="data_agent.learning.dedup.stage"):
            result = await stage.process(env, _ctx())
    finally:
        await dead_driver.close()  # type: ignore[attr-defined]

    # The loop kept running, and the answer came from the bucket.
    assert result.control == "continue"
    assert result.envelope.dedup.action == "merge"
    assert result.envelope.dedup.matched_id == "artifact-in-flight"
    # BOTH degrade paths logged — the structural lookup and the search — so an operator
    # sees the canon is invisible, not just that one query failed.
    messages = [r.getMessage() for r in caplog.records]
    assert any("structural lookup UNAVAILABLE" in m for m in messages), messages
    assert any("prior-art search UNAVAILABLE" in m for m in messages), messages


async def test_an_unreachable_graph_never_reports_the_canon_as_absent(seeded_canon):
    """The precise failure the `PriorArtUnavailableError` inversion exists to prevent: the
    SAME candidate that drops against a reachable graph must NOT be recorded as a novel
    insert-with-evidence when the graph is merely unreachable. It still inserts (the loop
    must not stop), but the verdict says `soft`/no match — never a claim that the canon
    was consulted and came back empty."""
    driver, embedder = seeded_canon
    env = _candidate(
        sql=_LLM_AUTHORED_SQL,
        grain=_LLM_GRAIN,
        intent="earnings by department for a pay period filtered to overtime",
    )

    live = await DedupStage(
        InMemoryBlueprintCorpus(), embedder, prior_art=_index(driver, embedder)
    ).process(env, _ctx())

    index, dead_driver = await _dead_index(embedder)
    try:
        degraded = await DedupStage(
            InMemoryBlueprintCorpus(), embedder, prior_art=index
        ).process(env, _ctx())
    finally:
        await dead_driver.close()  # type: ignore[attr-defined]

    assert live.control == "drop"
    assert degraded.control == "continue"
    assert degraded.envelope.dedup.layer == "soft"
    assert degraded.envelope.dedup.matched_id is None
