"""Layer-1 tests for the retrieval pipeline core (design §2/§5) — all fakes.

Covers: embed→recall→scope-filter→rerank→cut ordering; the blueprint
`USES ⊄ scope` drop; exactly-N cuts; knowledge floor; the `reranked` flag; and
all three degrade-not-fail paths (no embedder, no/failed reranker, empty index).
"""

from __future__ import annotations

from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
from data_agent.runtime.model.reranker_client import FakeRerankerClient
from data_agent.runtime.retrieval.models import Candidate
from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

_Q = "how much overtime did the sales team work"
_QVEC = [1.0, 0.0]


def _bp(
    id: str, intent: str, uses: set[str], vec: list[float]
) -> tuple[Candidate, list[float]]:
    return (
        Candidate(
            id=id,
            kind="blueprint",
            text=intent,
            uses=frozenset(uses),
            payload={"intent": intent, "slots_summary": f"slots-of-{id}"},
        ),
        vec,
    )


def _kn(id: str, chunk: str, vec: list[float], title: str | None = None) -> tuple[Candidate, list[float]]:
    return (
        Candidate(id=id, kind="knowledge", text=chunk, uses=None, payload={"title": title}),
        vec,
    )


def _pipeline(
    *,
    embedder: FakeEmbeddingClient | None,
    reranker: FakeRerankerClient | None,
    index: FakeVectorIndex,
    top_k_blueprints: int = 3,
    top_k_knowledge: int = 3,
    knowledge_min_score: float | None = None,
) -> RetrievalPipeline:
    return RetrievalPipeline(
        embedding_client=embedder,
        reranker=reranker,
        vector_index=index,
        user_memory=NullUserMemoryProvider(),
        recall_k=30,
        top_k_blueprints=top_k_blueprints,
        top_k_knowledge=top_k_knowledge,
        knowledge_min_score=knowledge_min_score,
    )


async def test_full_pipeline_recall_order_then_rerank_reorders() -> None:
    # Recall similarity puts A first (qvec==A's vec), B second. Rerank flips it.
    index = FakeVectorIndex(
        [
            _bp("bp-a", "sales overtime rollup", {"w.t.c"}, [1.0, 0.0]),
            _bp("bp-b", "headcount by dept", {"w.t.c"}, [0.9, 0.1]),
        ]
    )
    embedder = FakeEmbeddingClient({_Q: _QVEC})
    reranker = FakeRerankerClient(
        {"sales overtime rollup": 0.1, "headcount by dept": 0.9}
    )
    pipeline = _pipeline(embedder=embedder, reranker=reranker, index=index)

    ctx = await pipeline.retrieve(question=_Q, column_scope=frozenset(), user_id=None)

    assert [c.id for c in ctx.thin_cards] == ["bp-b", "bp-a"]  # rerank reordered
    assert ctx.reranked is True
    assert embedder.calls == [[_Q]]  # exactly one embed of the question


async def test_thin_card_fields_come_from_payload() -> None:
    index = FakeVectorIndex([_bp("bp-a", "intent text", {"w.t.c"}, [1.0, 0.0])])
    pipeline = _pipeline(
        embedder=FakeEmbeddingClient({_Q: _QVEC}),
        reranker=FakeRerankerClient({"intent text": 0.5}),
        index=index,
    )
    ctx = await pipeline.retrieve(question=_Q, column_scope=frozenset(), user_id=None)
    card = ctx.thin_cards[0]
    assert card.id == "bp-a"
    assert card.intent == "intent text"
    assert card.slots_summary == "slots-of-bp-a"


async def test_scope_prefilter_drops_out_of_scope_blueprints() -> None:
    index = FakeVectorIndex(
        [
            _bp("in", "in scope", {"w.t.a"}, [1.0, 0.0]),
            _bp("out", "out of scope", {"w.t.b"}, [1.0, 0.0]),
            _bp("partial", "partial overlap", {"w.t.a", "w.t.b"}, [1.0, 0.0]),
        ]
    )
    pipeline = _pipeline(
        embedder=FakeEmbeddingClient({_Q: _QVEC}), reranker=None, index=index
    )
    ctx = await pipeline.retrieve(
        question=_Q, column_scope=frozenset({"w.t.a"}), user_id=None
    )
    assert [c.id for c in ctx.thin_cards] == ["in"]  # out + partial dropped


async def test_knowledge_not_scope_filtered() -> None:
    index = FakeVectorIndex([_kn("kn-1", "entity-agnostic fact", [1.0, 0.0])])
    pipeline = _pipeline(
        embedder=FakeEmbeddingClient({_Q: _QVEC}), reranker=None, index=index
    )
    ctx = await pipeline.retrieve(
        question=_Q, column_scope=frozenset({"w.t.a"}), user_id=None
    )
    assert [h.id for h in ctx.knowledge_hits] == ["kn-1"]


async def test_cut_to_top_k() -> None:
    index = FakeVectorIndex(
        [_bp(f"bp-{i}", f"intent {i}", {"w.t.c"}, [1.0, i / 100]) for i in range(6)]
        + [_kn(f"kn-{i}", f"chunk {i}", [1.0, i / 100]) for i in range(6)]
    )
    pipeline = _pipeline(
        embedder=FakeEmbeddingClient({_Q: _QVEC}),
        reranker=None,
        index=index,
        top_k_blueprints=3,
        top_k_knowledge=2,
    )
    ctx = await pipeline.retrieve(question=_Q, column_scope=frozenset(), user_id=None)
    assert len(ctx.thin_cards) == 3
    assert len(ctx.knowledge_hits) == 2


async def test_knowledge_min_score_floor_drops_low_hits() -> None:
    index = FakeVectorIndex(
        [_kn("hi", "good chunk", [1.0, 0.0]), _kn("lo", "junk chunk", [1.0, 0.0])]
    )
    reranker = FakeRerankerClient({"good chunk": 5.0, "junk chunk": -3.0})
    pipeline = _pipeline(
        embedder=FakeEmbeddingClient({_Q: _QVEC}),
        reranker=reranker,
        index=index,
        knowledge_min_score=0.0,
    )
    ctx = await pipeline.retrieve(question=_Q, column_scope=frozenset(), user_id=None)
    assert [h.id for h in ctx.knowledge_hits] == ["hi"]  # lo below the floor


# --- Degrade path 1: no embedder / embedder failure → empty ---


async def test_no_embedder_returns_empty() -> None:
    index = FakeVectorIndex([_bp("bp-a", "x", {"w.t.c"}, [1.0, 0.0])])
    pipeline = _pipeline(embedder=None, reranker=None, index=index)
    ctx = await pipeline.retrieve(question=_Q, column_scope=frozenset(), user_id=None)
    assert ctx.is_empty()
    assert ctx.reranked is False


async def test_embedder_error_returns_empty() -> None:
    index = FakeVectorIndex([_bp("bp-a", "x", {"w.t.c"}, [1.0, 0.0])])
    pipeline = _pipeline(
        embedder=FakeEmbeddingClient(fail=True), reranker=None, index=index
    )
    ctx = await pipeline.retrieve(question=_Q, column_scope=frozenset(), user_id=None)
    assert ctx.is_empty()


# --- Degrade path 2: no reranker / rerank failure → recall order, reranked=False ---


async def test_no_reranker_uses_recall_order() -> None:
    index = FakeVectorIndex(
        [
            _bp("near", "near", {"w.t.c"}, [1.0, 0.0]),  # sim 1.0
            _bp("far", "far", {"w.t.c"}, [0.0, 1.0]),  # sim 0.0
        ]
    )
    pipeline = _pipeline(
        embedder=FakeEmbeddingClient({_Q: _QVEC}), reranker=None, index=index
    )
    ctx = await pipeline.retrieve(question=_Q, column_scope=frozenset(), user_id=None)
    assert [c.id for c in ctx.thin_cards] == ["near", "far"]  # recall order
    assert ctx.reranked is False


async def test_reranker_error_uses_recall_order() -> None:
    index = FakeVectorIndex(
        [
            _bp("near", "near", {"w.t.c"}, [1.0, 0.0]),
            _bp("far", "far", {"w.t.c"}, [0.0, 1.0]),
        ]
    )
    pipeline = _pipeline(
        embedder=FakeEmbeddingClient({_Q: _QVEC}),
        reranker=FakeRerankerClient(fail=True),
        index=index,
    )
    ctx = await pipeline.retrieve(question=_Q, column_scope=frozenset(), user_id=None)
    assert [c.id for c in ctx.thin_cards] == ["near", "far"]
    assert ctx.reranked is False


# --- Degrade path 3: index unavailable/empty → empty per corpus ---


async def test_empty_index_returns_empty() -> None:
    pipeline = _pipeline(
        embedder=FakeEmbeddingClient({_Q: _QVEC}),
        reranker=FakeRerankerClient(),
        index=FakeVectorIndex(fail=True),
    )
    ctx = await pipeline.retrieve(question=_Q, column_scope=frozenset(), user_id=None)
    assert ctx.is_empty()
    assert ctx.reranked is False


async def test_index_failure_of_one_corpus_does_not_block_the_other() -> None:
    # A raising index still degrades cleanly to empty (never raises out).
    class _RaisingIndex:
        async def recall(self, *, query_vector, kind, k):  # noqa: ANN001, ANN202
            raise RuntimeError("neo4j down")

    pipeline = _pipeline(
        embedder=FakeEmbeddingClient({_Q: _QVEC}),
        reranker=FakeRerankerClient(),
        index=_RaisingIndex(),  # type: ignore[arg-type]
    )
    ctx = await pipeline.retrieve(question=_Q, column_scope=frozenset(), user_id=None)
    assert ctx.is_empty()


async def test_reranker_called_once_per_nonempty_corpus() -> None:
    index = FakeVectorIndex(
        [
            _bp("bp-a", "bp intent", {"w.t.c"}, [1.0, 0.0]),
            _kn("kn-a", "kn chunk", [1.0, 0.0]),
        ]
    )
    reranker = FakeRerankerClient({"bp intent": 0.5, "kn chunk": 0.5})
    pipeline = _pipeline(
        embedder=FakeEmbeddingClient({_Q: _QVEC}), reranker=reranker, index=index
    )
    await pipeline.retrieve(question=_Q, column_scope=frozenset(), user_id=None)
    # One rerank call for blueprints, one for knowledge — the QUERY is the raw
    # question, documents are the candidate texts (never mixed corpora).
    assert [call[0] for call in reranker.calls] == [_Q, _Q]
    assert sorted(doc for _, docs in reranker.calls for doc in docs) == [
        "bp intent",
        "kn chunk",
    ]
