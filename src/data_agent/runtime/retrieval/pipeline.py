"""RetrievalPipeline — embed → recall → scope-filter → rerank → cut (design §2).

The one turn-time entry point: `retrieve(question, column_scope, user_id)` →
`RetrievedContext`. It is a deterministic function of (question, scope, corpus
snapshot) so a D45 resume re-derives the same block (design §6); the per-turn
memo that prevents re-embedding on every round-trip lives on the CALLER
(`ContextAssembler`/`AgentLoop`), not here — this object is a stateless,
shareable singleton.

Three independent degrade-not-fail paths (design §2, mirroring D85):
  - no embedder configured / embed failure → empty `RetrievedContext`;
  - no reranker configured / rerank failure → recall order + `reranked=False`;
  - vector index unavailable/empty → empty per-corpus result (other corpora
    unaffected).
`retrieve` NEVER raises and NEVER lets a `str(exc)` reach anything model- or
user-facing (the "never raises" invariant is enforced DEFENSIVELY here — a broad
`except Exception` at every external-call stage, not merely by trusting the D71
clients to wrap their own failures). A degrade is never silent: each empty-return
emits a shape-only `retrieval.degraded` CHAIN span AND a `{"blueprints": 0,
"knowledge": 0}` progress event (H1 — the D85 silent-degrade lesson).

Observability (design §3.5): one `recall_span` (CHAIN) per corpus WRAPPING the
awaited recall (real stage latency), one `rerank_span` (RERANKER) per corpus
WRAPPING the awaited rerank, plus one shape-only progress event. The QUESTION
TEXT is never a span attribute or a progress value (D25).
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from . import scope_filter
from .models import Candidate, KnowledgeHit, RetrievedContext, ThinCard

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from opentelemetry.trace import Tracer

    from data_agent.runtime.model.embedding_client import EmbeddingClient
    from data_agent.runtime.model.reranker_client import RerankerClient

    from .user_memory import UserMemoryProvider
    from .vector_index import VectorIndex

    Observer = Callable[[str, dict[str, Any]], None]

_logger = logging.getLogger(__name__)


class RetrievalPipeline:
    """Wires the two D71 model clients + a `VectorIndex` + a `UserMemoryProvider`
    into the D7/D8 recall+rerank pipeline (design §3.2)."""

    def __init__(
        self,
        *,
        embedding_client: EmbeddingClient | None,
        reranker: RerankerClient | None,
        vector_index: VectorIndex,
        user_memory: UserMemoryProvider,
        recall_k: int,
        top_k_blueprints: int,
        top_k_knowledge: int,
        knowledge_min_score: float | None = None,
        reranker_model: str = "",
        observer: Observer | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._embedding_client = embedding_client
        self._reranker = reranker
        self._vector_index = vector_index
        self._user_memory = user_memory
        self._recall_k = recall_k
        self._top_k_blueprints = top_k_blueprints
        self._top_k_knowledge = top_k_knowledge
        self._knowledge_min_score = knowledge_min_score
        self._reranker_model = reranker_model
        self._observer = observer
        self._tracer = tracer

    async def retrieve(
        self,
        *,
        question: str,
        column_scope: frozenset[str],
        user_id: str | None,
        observer: Observer | None = None,
    ) -> RetrievedContext:
        """Run the full pipeline for one turn. Never raises (degrade-not-fail).

        *observer* (optional) overrides the constructor observer for this call —
        the integration threads the PER-REQUEST observer here (the pipeline is a
        shared singleton on `ContextAssembler`, so the request-scoped progress
        emitter cannot be a constructor argument).
        """
        obs = observer if observer is not None else self._observer

        # L1: the "searching…" progress signal fires at retrieval START (before
        # any work), so the UI shows it while retrieval runs — not after. The
        # COMPLETION counts are a separate `retrieval` event emitted below (and
        # on every degrade path), keeping the design §3.5 count-shape.
        self._emit_event(obs, "retrieval_start", {})

        # --- EMBED (degrade: no embedder / any embed failure → empty) ---
        if self._embedding_client is None:
            return self._degrade("embedding_unconfigured", obs)
        try:
            vectors = await self._embedding_client.embed([question])
        except Exception:  # noqa: BLE001 - any embed failure degrades, never crashes
            _logger.warning("retrieval embed failed; returning empty context", exc_info=True)
            return self._degrade("embedding_error", obs)
        if not vectors:  # an embedder returning nothing degrades too
            return self._degrade("embedding_empty", obs)
        query_vector = vectors[0]

        # --- RECALL (per corpus; index unavailable/empty → [] per corpus) ---
        # The scope pre-filter (blueprints only; knowledge is entity-agnostic)
        # runs INSIDE the recall span so its dropped-count is recorded there.
        kept_blueprints = await self._recall_with_span(
            query_vector, "blueprint", column_scope=column_scope
        )
        knowledge_candidates = await self._recall_with_span(
            query_vector, "knowledge", column_scope=None
        )

        # --- RERANK (per corpus; no reranker / rerank failure → recall order) ---
        bp_ordered, bp_flag = await self._rerank_corpus(question, kept_blueprints)
        kn_ordered, kn_flag = await self._rerank_corpus(question, knowledge_candidates)
        flags = [flag for flag in (bp_flag, kn_flag) if flag is not None]
        reranked = bool(flags) and all(flags)

        # --- CUT ---
        thin_cards = [self._to_thin_card(c) for c in bp_ordered[: self._top_k_blueprints]]
        knowledge_hits = [
            self._to_knowledge_hit(c)
            for c in self._apply_knowledge_floor(kn_ordered)[: self._top_k_knowledge]
        ]

        # --- USER MEMORY (independent of the embedder; Null in Slice 1) ---
        try:
            user_memory = await self._user_memory.fetch(
                user_id=user_id, column_scope=column_scope
            )
        except Exception:  # noqa: BLE001 - a memory-store failure degrades that corpus only
            _logger.warning("user memory fetch failed; omitting", exc_info=True)
            user_memory = []

        self._emit_event(
            obs, "retrieval", {"blueprints": len(thin_cards), "knowledge": len(knowledge_hits)}
        )
        return RetrievedContext(
            thin_cards=thin_cards,
            knowledge_hits=knowledge_hits,
            user_memory=user_memory,
            reranked=reranked,
        )

    # ------------------------------------------------------------------ recall

    async def _recall_with_span(
        self, query_vector: list[float], corpus: str, *, column_scope: frozenset[str] | None
    ) -> list[Candidate]:
        """Recall one corpus inside a CHAIN span WRAPPING the awaited index call
        (L2 — real stage latency). For blueprints (*column_scope* not None) the
        USES ⊄ scope pre-filter runs inside the span so its dropped-count is
        recorded. A per-corpus degrade returns `[]`, never raises."""
        with self._recall_span(corpus) as span:
            raw = await self._recall(query_vector, corpus)
            if column_scope is not None:
                kept = scope_filter.filter_blueprints_by_scope(raw, column_scope)
            else:
                kept = list(raw)
            if span is not None:
                span.set_attribute("retrieval.candidate_count", len(raw))
                span.set_attribute("retrieval.dropped_by_scope_count", len(raw) - len(kept))
        return kept

    async def _recall(self, query_vector: list[float], kind: str) -> list[Candidate]:
        """One corpus recall — a per-corpus degrade returns `[]`, never raises."""
        try:
            return await self._vector_index.recall(
                query_vector=query_vector, kind=kind, k=self._recall_k
            )
        except Exception:  # noqa: BLE001 - index unavailable degrades this corpus only
            _logger.warning("vector recall failed for corpus %s; empty", kind, exc_info=True)
            return []

    # ------------------------------------------------------------------ rerank

    async def _rerank_corpus(
        self, question: str, candidates: Sequence[Candidate]
    ) -> tuple[list[Candidate], bool | None]:
        """Return (ordered candidates, reranked-flag).

        Flag is `True` when the reranker re-sorted, `False` on the degrade path
        (no reranker / any rerank failure / a score-count mismatch → recall order
        preserved), and `None` when there was nothing to rerank (empty corpus —
        not a degrade). Any exception (typed `RerankerError`, a `ValueError` from
        the strict score-count zip, or a non-conforming client's arbitrary error)
        degrades to recall order — no candidate is ever silently dropped (M1)."""
        if not candidates:
            return [], None
        if self._reranker is None:
            self._emit_rerank_marker(len(candidates), reranked=False)
            return list(candidates), False
        try:
            scored = await self._rerank_with_span(question, candidates)
        except Exception:  # noqa: BLE001 - any rerank failure degrades to recall order
            _logger.warning("rerank failed; using recall order", exc_info=True)
            return list(candidates), False
        scored.sort(key=lambda c: (-(c.score or 0.0), c.id))
        return scored, True

    async def _rerank_with_span(
        self, question: str, candidates: Sequence[Candidate]
    ) -> list[Candidate]:
        """Rerank inside a RERANKER span WRAPPING the awaited call (L2). Uses
        `zip(..., strict=True)` so a score-count mismatch raises `ValueError`
        (caught by the caller → degrade), never silently drops candidates (M1)."""
        docs = [c.text for c in candidates]
        with self._rerank_span(len(docs)) as span:
            try:
                scores = await self._reranker.rerank(question, docs)  # type: ignore[union-attr]
                scored = [
                    replace(c, score=s) for c, s in zip(candidates, scores, strict=True)
                ]
            except Exception:
                if span is not None:
                    span.set_attribute("reranker.reranked", False)
                raise
            if span is not None:
                span.set_attribute("reranker.reranked", True)
            return scored

    # -------------------------------------------------------------------- cut

    def _apply_knowledge_floor(self, candidates: list[Candidate]) -> list[Candidate]:
        """Drop knowledge candidates below the (optional) score floor (OQ-R2).

        Off by default (`None`) — ms-marco logits are uncalibrated, so an
        arbitrary floor is more likely to drop good hits than catch junk."""
        if self._knowledge_min_score is None:
            return candidates
        floor = self._knowledge_min_score
        return [c for c in candidates if (c.score or 0.0) >= floor]

    def _to_thin_card(self, candidate: Candidate) -> ThinCard:
        return ThinCard(
            id=candidate.id,
            intent=str(candidate.payload.get("intent", candidate.text)),
            slots_summary=str(candidate.payload.get("slots_summary", "")),
            score=float(candidate.score or 0.0),
        )

    def _to_knowledge_hit(self, candidate: Candidate) -> KnowledgeHit:
        title = candidate.payload.get("title")
        return KnowledgeHit(
            id=candidate.id,
            text=candidate.text,
            score=float(candidate.score or 0.0),
            title=title if isinstance(title, str) else None,
        )

    # ---------------------------------------------------------- observability

    def _recall_span(self, corpus: str) -> Any:
        """Open a recall CHAIN span (candidate/dropped counts filled inside), or
        a `nullcontext` yielding `None` when no tracer is wired."""
        if self._tracer is None:
            return nullcontext()
        from data_agent.runtime.observability import tracing

        return tracing.recall_span(
            self._tracer,
            corpus=corpus,
            recall_k=self._recall_k,
            candidate_count=0,
            dropped_by_scope_count=0,
        )

    def _rerank_span(self, document_count: int) -> Any:
        """Open a rerank RERANKER span (`reranked` filled inside), or a
        `nullcontext` yielding `None` when no tracer is wired."""
        if self._tracer is None:
            return nullcontext()
        from data_agent.runtime.observability import tracing

        return tracing.rerank_span(
            self._tracer,
            model=self._reranker_model,
            document_count=document_count,
            reranked=None,
        )

    def _emit_rerank_marker(self, document_count: int, *, reranked: bool) -> None:
        """A zero-work rerank marker span for the NO-reranker degrade (there is
        no awaited call to wrap in that case)."""
        if self._tracer is None:
            return
        from data_agent.runtime.observability import tracing

        with tracing.rerank_span(
            self._tracer,
            model=self._reranker_model,
            document_count=document_count,
            reranked=reranked,
        ):
            pass

    def _degrade(self, reason: str, obs: Observer | None) -> RetrievedContext:
        """A retrieval-wide degrade early-return: emit a shape-only degrade span
        + a `{0, 0}` progress event (H1 — never a silent degrade), return empty."""
        if self._tracer is not None:
            from data_agent.runtime.observability import tracing

            with tracing.chain_span(
                self._tracer, "retrieval.degraded", attributes={"retrieval.degraded": reason}
            ):
                pass
        self._emit_event(obs, "retrieval", {"blueprints": 0, "knowledge": 0})
        return RetrievedContext.empty()

    def _emit_event(self, obs: Observer | None, event: str, payload: dict[str, Any]) -> None:
        """Emit one shape-only retrieval progress event. Counts/labels only,
        never the question (D25/D61). A misbehaving observer is swallowed +
        logged server-side — a progress-emit failure must not crash the turn
        (M2)."""
        if obs is None:
            return
        try:
            obs(event, payload)
        except Exception:  # noqa: BLE001 - a bad observer never breaks retrieval
            _logger.warning("retrieval progress observer raised; ignored", exc_info=True)


__all__ = ["RetrievalPipeline"]
