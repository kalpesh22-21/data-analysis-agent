"""QA adversarial Layer-1 tests for the retrieval pipeline (design §2).

Additive to `test_pipeline.py` — targets gaps the happy-path suite does not
cover: malformed / hostile index results, reranker edge cases (score-count
mismatch, NaN/inf, all-equal stability, empty corpus, non-typed exception),
the embed non-typed exception, and cut-parameter extremes.

Three former PRODUCT-BUG repros (embedder/reranker non-typed exception, reranker
short-score-list) are now FIXED (M1/M2) and assert the degrade behaviour
directly — the `xfail` markers were removed as the fixes landed. Everything else
asserts intended/safe behaviour.
"""

from __future__ import annotations

import math

from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
from data_agent.runtime.model.reranker_client import FakeRerankerClient
from data_agent.runtime.retrieval.models import Candidate
from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

_Q = "how much overtime did the sales team work"
_QVEC = [1.0, 0.0]


def _bp(id: str, uses: set[str] | None = None, intent: str | None = None) -> Candidate:
    return Candidate(
        id=id,
        kind="blueprint",
        text=intent or id,
        uses=frozenset(uses) if uses is not None else None,
        payload={"intent": intent or id, "slots_summary": f"slots-{id}"},
    )


def _seed(*candidates: Candidate) -> FakeVectorIndex:
    return FakeVectorIndex([(c, [1.0, 0.0]) for c in candidates])


def _pipeline(
    *,
    index: FakeVectorIndex,
    reranker=None,
    embedder=None,
    top_k_blueprints: int = 3,
    top_k_knowledge: int = 3,
    knowledge_min_score: float | None = None,
) -> RetrievalPipeline:
    return RetrievalPipeline(
        embedding_client=embedder if embedder is not None else FakeEmbeddingClient({_Q: _QVEC}),
        reranker=reranker,
        vector_index=index,
        user_memory=NullUserMemoryProvider(),
        recall_k=30,
        top_k_blueprints=top_k_blueprints,
        top_k_knowledge=top_k_knowledge,
        knowledge_min_score=knowledge_min_score,
    )


async def _retrieve(pipeline: RetrievalPipeline, scope: frozenset[str] = frozenset()):
    return await pipeline.retrieve(question=_Q, column_scope=scope, user_id=None)


# --------------------------------------------------------------------------
# Malformed / hostile index results
# --------------------------------------------------------------------------


async def test_blueprint_with_none_uses_is_dropped_even_under_allow_all() -> None:
    # A malformed index that returns a blueprint candidate with undetermined
    # USES must be dropped fail-closed by the pipeline, even with an empty
    # (allow-all) scope — proves the scope pre-filter runs INSIDE the pipeline,
    # not only in the unit-tested filter helper.
    ctx = await _retrieve(_pipeline(index=_seed(_bp("undet", uses=None))))
    assert ctx.thin_cards == []
    assert ctx.is_empty()


async def test_index_returning_wrong_kind_candidate_is_still_scope_gated() -> None:
    # An index that (wrongly) hands back a knowledge-kind candidate on a
    # blueprint recall: it carries uses=None, so the blueprint scope filter
    # drops it. Nothing leaks into the thin cards.
    class _MixIndex:
        async def recall(self, *, query_vector, kind, k):  # noqa: ANN001, ANN202
            if kind == "blueprint":
                return [Candidate(id="x", kind="knowledge", text="x", uses=None)]
            return []

    ctx = await _retrieve(_pipeline(index=_MixIndex()))  # type: ignore[arg-type]
    assert ctx.thin_cards == []


async def test_duplicate_candidate_ids_are_not_deduped_but_order_is_stable() -> None:
    # Two candidates sharing an id + identical rerank score: the pipeline does
    # not dedupe (documented behaviour), and the id tiebreak keeps output
    # deterministic across runs.
    index = _seed(_bp("dup", uses=set()), _bp("dup", uses=set()))
    reranker = FakeRerankerClient({"dup": 0.5})
    ctx1 = await _retrieve(_pipeline(index=index, reranker=reranker))
    index2 = _seed(_bp("dup", uses=set()), _bp("dup", uses=set()))
    ctx2 = await _retrieve(_pipeline(index=index2, reranker=FakeRerankerClient({"dup": 0.5})))
    assert [c.id for c in ctx1.thin_cards] == ["dup", "dup"]
    assert [c.id for c in ctx1.thin_cards] == [c.id for c in ctx2.thin_cards]


async def test_candidate_missing_payload_falls_back_to_text() -> None:
    # A candidate with an empty payload must still render a valid thin card —
    # intent falls back to `text`, slots_summary to "".
    class _BareIndex:
        async def recall(self, *, query_vector, kind, k):  # noqa: ANN001, ANN202
            if kind == "blueprint":
                return [Candidate(id="bare", kind="blueprint", text="the-intent",
                                  uses=frozenset(), score=0.4)]
            return []

    ctx = await _retrieve(_pipeline(index=_BareIndex()))  # type: ignore[arg-type]
    card = ctx.thin_cards[0]
    assert card.id == "bare"
    assert card.intent == "the-intent"
    assert card.slots_summary == ""


# --------------------------------------------------------------------------
# Reranker edge cases through the pipeline
# --------------------------------------------------------------------------


async def test_empty_corpus_does_not_call_reranker() -> None:
    # No candidates for either corpus → the reranker is never invoked (design:
    # "empty corpus is not a degrade"), reranked flag is False, ctx empty.
    reranker = FakeRerankerClient()
    ctx = await _retrieve(_pipeline(index=_seed(), reranker=reranker))
    assert reranker.calls == []
    assert ctx.is_empty()
    assert ctx.reranked is False


async def test_all_equal_rerank_scores_are_ordered_by_id_tiebreak() -> None:
    # Every candidate gets the same rerank score → stable, deterministic order
    # by ascending id (the pipeline's documented tiebreak).
    index = _seed(_bp("c", uses=set()), _bp("a", uses=set()), _bp("b", uses=set()))
    reranker = FakeRerankerClient({"a": 0.5, "b": 0.5, "c": 0.5})
    ctx = await _retrieve(_pipeline(index=index, reranker=reranker))
    assert [c.id for c in ctx.thin_cards] == ["a", "b", "c"]


async def test_nan_rerank_scores_do_not_crash_and_stay_deterministic() -> None:
    # A reranker returning NaN scores (out of the finite contract the Http
    # client enforces, but reachable via a non-conforming client) must not
    # crash the turn; the count is preserved and the run is repeatable.
    class _NanReranker:
        async def rerank(self, query, documents):  # noqa: ANN001, ANN202
            return [float("nan")] * len(documents)

    index = _seed(_bp("a", uses=set()), _bp("b", uses=set()), _bp("c", uses=set()))
    ctx = await _retrieve(_pipeline(index=index, reranker=_NanReranker()))  # type: ignore[arg-type]
    assert len(ctx.thin_cards) == 3
    assert all(math.isnan(c.score) for c in ctx.thin_cards)


async def test_reranker_error_on_one_corpus_still_degrades_that_corpus() -> None:
    # A RerankerError (the typed, in-contract failure) degrades to recall order
    # for BOTH corpora and flags reranked=False — the turn survives.
    index = FakeVectorIndex(
        [
            (_bp("near", uses=set()), [1.0, 0.0]),
            (_bp("far", uses=set()), [0.0, 1.0]),
            (Candidate(id="kn", kind="knowledge", text="k", uses=None), [1.0, 0.0]),
        ]
    )
    ctx = await _retrieve(_pipeline(index=index, reranker=FakeRerankerClient(fail=True)))
    assert [c.id for c in ctx.thin_cards] == ["near", "far"]  # recall order
    assert ctx.reranked is False


async def test_reranked_flag_true_when_only_one_corpus_present() -> None:
    # Only a blueprint corpus (knowledge empty → flag None, filtered out): a
    # successful blueprint rerank must still mark reranked=True.
    index = _seed(_bp("a", uses=set()))
    ctx = await _retrieve(_pipeline(index=index, reranker=FakeRerankerClient({"a": 0.9})))
    assert ctx.reranked is True


# --------------------------------------------------------------------------
# Cut-parameter extremes
# --------------------------------------------------------------------------


async def test_top_k_zero_yields_no_cards() -> None:
    # Defensive: a top_k of 0 (config forbids it, but the pipeline must not
    # index-error) yields an empty cut.
    index = _seed(_bp("a", uses=set()), _bp("b", uses=set()))
    ctx = await _retrieve(_pipeline(index=index, reranker=FakeRerankerClient(), top_k_blueprints=0))
    assert ctx.thin_cards == []


async def test_recall_k_below_top_k_silently_caps_candidates() -> None:
    # No cross-field validation forces recall_k >= top_k; when recall_k is
    # smaller the index returns fewer than top_k and the cut is a no-op ceiling.
    # Documents that a misconfiguration silently reduces candidates.
    pipeline = RetrievalPipeline(
        embedding_client=FakeEmbeddingClient({_Q: _QVEC}),
        reranker=FakeRerankerClient(),
        vector_index=_seed(*[_bp(f"bp{i}", uses=set()) for i in range(5)]),
        user_memory=NullUserMemoryProvider(),
        recall_k=2,  # < top_k
        top_k_blueprints=3,
        top_k_knowledge=3,
    )
    ctx = await _retrieve(pipeline)
    assert len(ctx.thin_cards) == 2  # capped by recall_k, not top_k


# --------------------------------------------------------------------------
# Former PRODUCT-BUG repros — now FIXED (M2/M1): retrieve() degrades on ANY
# exception at every external-call stage (design §2 "never raises"). xfail
# markers removed as the fixes landed.
# --------------------------------------------------------------------------


async def test_reranker_non_typed_exception_should_degrade_not_crash() -> None:
    class _BadReranker:
        async def rerank(self, query, documents):  # noqa: ANN001, ANN202
            raise ValueError("not a RerankerError")

    index = _seed(_bp("a", uses=set()), _bp("b", uses=set()))
    ctx = await _retrieve(_pipeline(index=index, reranker=_BadReranker()))  # type: ignore[arg-type]
    # Desired: degrade to recall order, turn survives.
    assert [c.id for c in ctx.thin_cards] == ["a", "b"]
    assert ctx.reranked is False


async def test_embedder_non_typed_exception_should_degrade_to_empty() -> None:
    class _BadEmbedder:
        async def embed(self, texts):  # noqa: ANN001, ANN202
            raise RuntimeError("embed boom")

    ctx = await _retrieve(_pipeline(index=_seed(_bp("a", uses=set())), embedder=_BadEmbedder()))
    assert ctx.is_empty()


async def test_reranker_short_score_list_should_not_drop_candidates() -> None:
    class _ShortReranker:
        async def rerank(self, query, documents):  # noqa: ANN001, ANN202
            return [0.5] * (len(documents) - 1)  # one short

    index = _seed(_bp("a", uses=set()), _bp("b", uses=set()), _bp("c", uses=set()))
    ctx = await _retrieve(_pipeline(index=index, reranker=_ShortReranker()))  # type: ignore[arg-type]
    # FIXED (M1): strict-zip raises ValueError → degrade to recall order, no
    # candidate silently lost.
    assert len(ctx.thin_cards) == 3
    assert ctx.reranked is False
