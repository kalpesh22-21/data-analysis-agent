"""QA — IN-FLIGHT duplicate detection across BOTH soft sources (PriorArt Slice 2).

# The scenario

Two concurrent sessions propose the SAME blueprint with slightly different SQL. The two
deterministic layers both miss it, by construction:

  1. **Hard key** — a SHA-256 over `(resolves, uses_rules, result_grain,
     canonical_ast_norm)`. Slightly different SQL ⇒ a different `canonical_ast_norm` ⇒ a
     different digest. Miss.
  2. **Structural key** — resolved against `PriorArtIndex`, i.e. against NEO4J. Neither
     candidate has LANDED yet (landing happens at promotion, long after S6), so neither
     is in the graph. Miss.
  3. **Soft layer** — a cosine over `intent`. This is the only layer that can catch it,
     and only if it looks in the right place.

# HISTORY — this file pinned a regression, and now asserts its fix

As first written, this slice made `_soft_layer` return the prior-art index's answer and
reach the `learning_corpus` bucket ONLY when the index raised. That silently removed
working behaviour: `_seed_on_insert` registers a candidate's artifact in the bucket the
moment it is adjudicated `insert`, months before it lands, so the bucket is the ONLY
place an in-flight sibling is visible. The graph answered `[]` honestly and both sessions
were recorded as novel.

The tell was that the fail-open path still behaved correctly — it was merely unreachable
(`test_the_bucket_scan_is_reachable_...` below, which used to be named for the gap).

The fix makes the bucket a SECOND SOURCE rather than a fallback: `_soft_layer` queries
both, de-duplicates across them by the deterministic landing id, and bands the union.
These tests now assert that, and the three wirings below — no index, index wired, index
failing — must all reach the SAME verdict, because the bucket half is what answers in
every one of them.

The cosine used here (0.9645) is not invented: it is the measured `all-mpnet-base-v2`
similarity between the two intent strings below, taken from the live D71 embedding
service at `http://localhost:18003/embed`. It clears the 0.95 merge threshold.

Slugs:
  * S6-inflight-dupes-are-caught  — the union catches the paraphrase pair.
  * S6-soft-union-is-wiring-invariant — all three wirings agree.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.dedup import DedupStage, InMemoryBlueprintCorpus
from data_agent.learning.priorart import InMemoryPriorArtIndex
from data_agent.learning.stage import StageContext
from data_agent.learning.summary.models import SessionSummary
from data_agent.learning.triage import TriageVerdict
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"

# The two sessions' intents — a paraphrase pair, the realistic shape of "two analysts
# asked the same question five minutes apart".
INTENT_A = "total earnings by department for calendar year 2024"
INTENT_B = "total earnings per department in the 2024 calendar year"

# MEASURED against the live embedding service (see the module docstring), not chosen.
LIVE_COSINE = 0.9645421121837473

# Two unit vectors separated by exactly that cosine, so `_cosine` in the stage reproduces
# the live number without needing the live service.
_THETA = math.acos(LIVE_COSINE)
_VECTORS = {
    INTENT_A: [1.0, 0.0],
    INTENT_B: [math.cos(_THETA), math.sin(_THETA)],
}


def _ctx() -> StageContext:
    summary = SessionSummary(
        session_id="sess-qa",
        user_id="u1",
        scope_ref="scope-1",
        trace_id="trace-qa",
        content_hash="hash-qa",
        turns=(),
        tool_calls=(),
        blueprint_usages=(),
        askuser_exchanges=(),
        failed_fixed_sql=(),
        accepted_signal="no_correction",
    )
    return StageContext(summary=summary, verdict=TriageVerdict(decision="keep", reason="K1"))


def _envelope(*, candidate_id: str, intent: str, sql: str) -> CandidateEnvelope:
    """One session's candidate: same question, DIFFERENT SQL — so the hard key and the
    structural key both differ, which is the whole premise of the gap."""
    doc = json.loads((FIXTURES / "s4_enriched_blueprint.json").read_text())
    env = CandidateEnvelope.from_doc(doc["single"]["envelope"])
    payload = dict(env.payload)
    gen = dict(payload["generalization"])
    gen["sql_template"] = sql
    gen["canonical_ast_norm"] = sql  # a different template ⇒ a different frozen digest
    payload["generalization"] = gen
    payload["intent"] = intent
    return replace(env, candidate_id=candidate_id, payload=payload)


SQL_A = "SELECT department, sum(amount) AS total FROM payroll.f WHERE year = {year} GROUP BY department"
SQL_B = "SELECT department, sum(amount) AS total FROM payroll.f WHERE year = {year} GROUP BY 1"


def _sessions() -> tuple[CandidateEnvelope, CandidateEnvelope]:
    return (
        _envelope(candidate_id="cand-session-a", intent=INTENT_A, sql=SQL_A),
        _envelope(candidate_id="cand-session-b", intent=INTENT_B, sql=SQL_B),
    )


# --- S6-inflight-dupes-are-caught ---------------------------------------------


async def test_with_no_index_the_bucket_catches_an_in_flight_paraphrase():
    """`prior_art=None` — a SUPPORTED production configuration (the factory logs a
    warning and carries on), and the behaviour of any deployment without neo4j. The soft
    layer scans `learning_corpus`, which `_seed_on_insert` has already populated with
    session A's artifact, and session B is routed to a human at the live cosine.

    This is the reference verdict the two tests below must match."""
    env_a, env_b = _sessions()
    corpus = InMemoryBlueprintCorpus()
    stage = DedupStage(corpus, FakeEmbeddingClient(_VECTORS), prior_art=None)

    first = await stage.process(env_a, _ctx())
    assert first.envelope.dedup.action == "insert"
    # A's artifact is now in the bucket, in-flight and un-landed.
    assert corpus.seed_calls == [first.envelope.dedup.canonical_key]

    second = await stage.process(env_b, _ctx())

    assert second.envelope.dedup.action == "merge"
    assert second.envelope.dedup.matched_id == "cand-session-a"
    assert second.envelope.dedup.similarity >= 0.95


# --- S6-soft-union-is-wiring-invariant ----------------------------------------


async def test_with_the_index_wired_the_bucket_half_still_catches_the_paraphrase():
    """WAS a pinned known limitation; now the assertion of the fix.

    The index is wired and answers honestly — the graph contains no in-flight candidate,
    so `search` returns `[]`. That must NOT be mistaken for "no prior art exists": the
    bucket half of the union runs regardless, finds session A's un-landed artifact, and
    routes session B to a human.

    The index IS still asked (it is the only thing that can see the canon), and its empty
    answer simply contributes nothing to the union."""
    env_a, env_b = _sessions()
    corpus = InMemoryBlueprintCorpus()
    index = InMemoryPriorArtIndex([])  # the graph: no in-flight candidate is in it
    stage = DedupStage(corpus, FakeEmbeddingClient(_VECTORS), prior_art=index)

    first = await stage.process(env_a, _ctx())
    second = await stage.process(env_b, _ctx())

    assert first.envelope.dedup.action == "insert"
    assert second.envelope.dedup.action == "merge"
    assert second.envelope.dedup.matched_id == "cand-session-a"
    assert second.envelope.dedup.similarity >= 0.95
    # Only session A was seeded: B is going to a human, so it starts no rival hit count.
    assert corpus.seed_calls == [first.envelope.dedup.canonical_key]
    # The index WAS consulted both times — the union asks both sources every time.
    assert [c[0] for c in index.search_calls] == [INTENT_A, INTENT_B]


async def test_the_bucket_half_answers_identically_when_the_index_is_unavailable():
    """The degraded path is now a strict SUBSET of the healthy one, not a different code
    path — which is precisely the property whose absence let the regression hide: the
    fallback had the right behaviour all along, it was simply unreachable."""
    env_a, env_b = _sessions()
    corpus = InMemoryBlueprintCorpus()
    embedder = FakeEmbeddingClient(_VECTORS)

    await DedupStage(corpus, embedder, prior_art=InMemoryPriorArtIndex([])).process(
        env_a, _ctx()
    )
    degraded = DedupStage(corpus, embedder, prior_art=InMemoryPriorArtIndex([], fail=True))

    result = await degraded.process(env_b, _ctx())

    assert result.envelope.dedup.action == "merge"
    assert result.envelope.dedup.matched_id == "cand-session-a"


async def test_all_three_wirings_reach_the_same_verdict():
    """The invariant that makes the regression impossible to reintroduce quietly: no
    index, index wired, index failing — the in-flight paraphrase is caught in all three,
    because the bucket half is what answers in every one of them. If a future change
    makes these disagree, the union has been broken again."""
    verdicts = []
    for prior_art in (None, InMemoryPriorArtIndex([]), InMemoryPriorArtIndex([], fail=True)):
        env_a, env_b = _sessions()
        corpus = InMemoryBlueprintCorpus()
        stage = DedupStage(corpus, FakeEmbeddingClient(_VECTORS), prior_art=prior_art)
        await stage.process(env_a, _ctx())
        second = await stage.process(env_b, _ctx())
        verdicts.append((second.envelope.dedup.action, second.envelope.dedup.matched_id))

    assert verdicts == [("merge", "cand-session-a")] * 3
