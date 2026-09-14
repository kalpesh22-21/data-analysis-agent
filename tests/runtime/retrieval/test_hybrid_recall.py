"""Hybrid recall and bounded per-deliverable retrieval regressions."""

import json

import pytest

from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
from data_agent.runtime.model.reranker_client import FakeRerankerClient
from data_agent.runtime.retrieval.hybrid import fuse_candidates, keyword_query
from data_agent.runtime.retrieval.models import Candidate
from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
from data_agent.runtime.retrieval.tools import SearchBlueprintsTool
from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex, Neo4jVectorIndex
from tests.runtime.retrieval.test_read_tools import _creds


def candidate(ident, uses=frozenset({"db.t.amount"})):
    return Candidate(
        id=ident,
        kind="blueprint",
        text=ident,
        uses=uses,
        payload={"intent": ident, "slots_summary": ""},
    )


class HybridIndex(FakeVectorIndex):
    def __init__(self, semantic=(), lexical=(), fail_keywords=False):
        super().__init__([(c, [1.0, 0.0]) for c in semantic])
        self.lexical = list(lexical)
        self.keyword_calls = []
        self.fail_keywords = fail_keywords

    async def recall_keywords(self, *, query, k):
        self.keyword_calls.append((query, k))
        if self.fail_keywords:
            raise RuntimeError("private failure")
        return self.lexical[:k]


def pipeline(index, *, embed=True, recall_k=2, blueprint_recall_k=5, scores=None):
    return RetrievalPipeline(
        embedding_client=FakeEmbeddingClient() if embed else None,
        reranker=FakeRerankerClient(scores or {}),
        vector_index=index,
        user_memory=NullUserMemoryProvider(),
        recall_k=recall_k,
        blueprint_recall_k=blueprint_recall_k,
        top_k_blueprints=1,
        top_k_knowledge=1,
    )


async def test_lexical_only_match_survives_fusion_then_wins_reranking():
    index = HybridIndex([candidate("semantic")], [candidate("exact-acronym")])
    p = pipeline(index, scores={"semantic": 0.1, "exact-acronym": 1.0})
    cards, _ = await p.search_blueprints(
        question="acronym", column_scope=frozenset({"db.t.amount"}), k=1
    )
    assert [c.id for c in cards] == ["exact-acronym"]
    assert index.calls == [("blueprint", 5)]
    assert index.keyword_calls == [("acronym", 5)]


async def test_scope_filter_precedes_rerank_for_both_channels():
    bad = candidate("restricted", frozenset({"db.t.ssn"}))
    index = HybridIndex([bad, candidate("allowed")], [bad, candidate("keyword")])
    p = pipeline(index)
    seen = []

    async def rerank(question, candidates):
        seen.extend(candidates)
        return list(candidates), True

    p._rerank_corpus = rerank
    await p.search_blueprints(question="salary", column_scope=frozenset({"db.t.amount"}), k=2)
    assert {c.id for c in seen} == {"allowed", "keyword"}


@pytest.mark.parametrize("prefetch", [False, True])
async def test_keyword_retrieval_survives_embedding_unavailable(prefetch):
    index = HybridIndex([], [candidate("keyword")])
    p = pipeline(index, embed=False)
    if prefetch:
        result = await p.retrieve(
            question="salary", column_scope=frozenset({"db.t.amount"}), user_id=None
        )
        cards = result.thin_cards
    else:
        cards, _ = await p.search_blueprints(
            question="salary", column_scope=frozenset({"db.t.amount"}), k=1
        )
    assert [c.id for c in cards] == ["keyword"]
    assert not index.calls


async def test_keyword_failure_keeps_semantic_results():
    p = pipeline(HybridIndex([candidate("semantic")], fail_keywords=True))
    cards, _ = await p.search_blueprints(question="salary", column_scope=frozenset(), k=1)
    assert [c.id for c in cards] == ["semantic"]


def test_fusion_deduplicates_and_does_not_compare_channel_score_scales():
    a, b = candidate("a"), candidate("b")
    results = fuse_candidates([a, b], [b, b])
    assert [c.id for c in results] == ["b", "a"]
    assert len(results) == 2


@pytest.mark.parametrize("optional_query", [{}, {"query": None}, {"query": ""}, {"query": "  "}])
async def test_deliverables_are_separately_embedded_ranked_and_share_card_budget(optional_query):
    index = HybridIndex([candidate("one"), candidate("two"), candidate("three")])
    p = pipeline(index)
    tool = SearchBlueprintsTool(pipeline=p, default_k=3, max_k=8)
    result = await tool.run(
        {
            **optional_query,
            "deliverables": ["salary by department in 2025", "monthly hires in 2024"],
            "k": 3,
        },
        _creds(),
    )
    assert result.status == "ok"
    assert {tuple(c) for c in p._embedding_client.calls} == {
        ("salary by department in 2025",),
        ("monthly hires in 2024",),
    }
    groups = result.result_full["searches"]
    assert [len(g["blueprint_ids"]) for g in groups] == [2, 1]
    assert [g["deliverable"] for g in groups] == [1, 2]
    assert result.result_full["count"] <= 3
    assert "monthly hires in 2024" not in json.dumps(result.result_full)


@pytest.mark.parametrize(
    "args",
    [
        {"query": "x", "deliverables": ["y"]},
        {"deliverables": []},
        {"deliverables": ["a"] * 5},
        {"deliverables": ["a", "a"]},
        {"deliverables": [""]},
        {"deliverables": [5]},
    ],
)
async def test_invalid_deliverable_batch_performs_no_search(args):
    index = HybridIndex()
    tool = SearchBlueprintsTool(pipeline=pipeline(index), default_k=3, max_k=8)
    result = await tool.run(args, _creds())
    assert result.error_code == "RETRIEVAL_TOOL_INVALID_ARGS"
    assert not index.calls and not index.keyword_calls


def test_keyword_text_cannot_inject_lucene_operators():
    assert keyword_query("salary OR *:* -(ssn)") == '"salary" OR "or" OR "ssn"'
    assert keyword_query("") == ""


async def test_neo4j_keyword_query_keeps_trust_gates_and_uses_bound_parameters():
    index = Neo4jVectorIndex(
        url="bolt://unused", auth=("x", "y"), expected_model="model", driver=object()
    )
    captured = []

    async def run(query, params):
        captured.append((query, params))
        return [{"id": "good", "text": "salary", "uses": ["db.t.amount"], "score": 2.0}]

    index._run = run
    found = await index.recall_keywords(query="salary OR *:*", k=7)
    assert found[0].id == "good"
    query, params = captured[0]
    assert "node.source = 'mcp'" in query and "drift_status" in query and "node.status" in query
    assert params["k"] == 7 and params["query"] == '"salary" OR "or"'
    assert "salary" not in query


def test_keyword_query_is_bounded_even_for_unbounded_legacy_single_query():
    assert keyword_query("x" * 10000) == ""
    assert len(keyword_query(" ".join("word" + str(i) for i in range(100))).split(" OR ")) == 32


def test_grouped_preview_does_not_reference_hidden_cards():
    from data_agent.runtime.dispatch.tool_dispatcher import _cap_nontabular_result

    raw = {
        "count": 2,
        "blueprints": [{"id": "a", "intent": "short"}, {"id": "b", "intent": "x" * 10000}],
        "searches": [
            {"deliverable": 1, "blueprint_ids": ["a"]},
            {"deliverable": 2, "blueprint_ids": ["b"]},
        ],
    }
    capped, truncated = _cap_nontabular_result(raw, 500)
    assert truncated and capped["count"] == 1
    assert (
        capped["searches"][1]["blueprint_ids"] == [] and capped["searches"][1]["omitted_count"] == 1
    )
    assert raw["searches"][1]["blueprint_ids"] == ["b"]


async def test_scope_conflicting_duplicate_cannot_replace_allowed_payload():
    index = HybridIndex([candidate("same")], [candidate("same", frozenset({"db.t.ssn"}))])
    p = pipeline(index)
    cards, _ = await p.search_blueprints(
        question="salary", column_scope=frozenset({"db.t.amount"}), k=1
    )
    assert len(cards) == 1 and cards[0].uses == frozenset({"db.t.amount"})
