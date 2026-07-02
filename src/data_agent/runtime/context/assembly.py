"""ContextAssembler — the D50 fixed-order context assembly pipeline (design §5).

    1. load   — `SessionStore.load_trail(session_id)`
    2. filter — D44 scope re-filter (`scope_filter.filter_trail`)
    3. budget — D46 token-budgeted compaction (`budget.compact_trail`)
    4. inject — render to model-facing messages (`budget.render_messages`)

Ordering is load-bearing (D50): filtering strictly before compaction means the
summarizer (Pass B: an LLM call) never sees an out-of-scope entry, so the
resulting prose is safe by construction and needs no residual per-column
provenance tag.

Pass-B seam: `summarizer`/`cache` are injected dependencies (see
`context/budget.py`); Pass B's `AgentLoop` also passes the current
`RuntimeCredentials` to `ToolDispatcher` separately — `ContextAssembler` only
ever needs `column_scope`, never the JWT (design §2: "scope only, no jwt
needed here").
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from data_agent.runtime.observability import tracing
from data_agent.runtime.retrieval.render import render_retrieved_context
from data_agent.runtime.session.store import SessionStore

from . import scope_filter
from .budget import (
    CompactionResult,
    Summarizer,
    SummaryCache,
    compact_trail_async,
    default_summarizer,
    render_messages,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from opentelemetry.trace import Tracer

    from data_agent.runtime.retrieval.models import RetrievedContext
    from data_agent.runtime.retrieval.pipeline import RetrievalPipeline

    Observer = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class AssembledContext:
    """The result of one `ContextAssembler.assemble()` call."""

    messages: list[dict[str, Any]]
    dropped_by_scope_count: int
    compaction_applied: bool
    # Slice-1 retrieval: shape-only counts of the pre-injected block (design
    # §12 "AssembledContext may gain retrieved_counts for the span"); `(0, 0)`
    # whenever retrieval did not run (unconfigured / no user_message) — the
    # unconfigured path is byte-identical either way.
    retrieved_counts: tuple[int, int] = (0, 0)


class ContextAssembler:
    """Wires `SessionStore` + the budget/summarizer dependencies into the D50 pipeline."""

    def __init__(
        self,
        session_store: SessionStore,
        *,
        history_token_budget: int,
        preview_row_count: int = 20,
        summarizer: Summarizer = default_summarizer,
        cache: SummaryCache | None = None,
        retrieval: RetrievalPipeline | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._session_store = session_store
        self._history_token_budget = history_token_budget
        self._preview_row_count = preview_row_count
        self._summarizer = summarizer
        self._cache = cache if cache is not None else SummaryCache()
        # Slice-1 retrieval (design §3.3): an OPTIONAL pre-loop stage. When
        # `None` (Layer-1 history-only tests, unconfigured deploy) `assemble`
        # is byte-identical to before this dependency existed — the whole
        # retrieval branch below is gated on both `retrieval is not None` AND a
        # non-`None` `user_message`, so no existing caller observes any change.
        self._retrieval = retrieval
        # B5: optional — when wired (app.py's composition root), assemble()
        # emits one CHAIN span per call recording only non-sensitive shape
        # counters (trail entries loaded, dropped-by-scope count, compaction
        # hit/miss — design §7's CHAIN row); `None` (Layer-1 tests) means no
        # span is ever created.
        self._tracer = tracer

    async def assemble(
        self,
        session_id: str,
        column_scope: frozenset[str],
        current_turn_index: int | None = None,
        *,
        user_message: str | None = None,
        user_id: str | None = None,
        retrieval_memo: dict[tuple[str, str], RetrievedContext] | None = None,
        observer: Observer | None = None,
    ) -> AssembledContext:
        """*current_turn_index* (turn-scoped continuity, 2026-07-01, optional):
        threaded straight through to `scope_filter.filter_trail` — see that
        function's docstring. Default `None` preserves the exact pre-existing
        all-strict D44 replay behavior; this is what the QA-locked
        `tests/runtime/provenance/test_fail_closed_replay_adversarial.py`
        still exercises, unchanged.

        *user_message*/*user_id* (Slice-1 retrieval, design §3.3, optional):
        when a `retrieval` pipeline was injected AND a *user_message* is given,
        the retrieved thin-cards/knowledge/user-memory block is pre-injected as
        ONE system message at the front of `messages` (before history). Absent
        either, no retrieval runs and the returned context is byte-identical to
        the pre-retrieval behavior.

        *retrieval_memo* (design §3.3, turn-local): a caller-owned dict that
        memoizes the `RetrievedContext` by `(user_message, scope_hash)` so the
        D45 per-round-trip rebuild embeds/recalls at most ONCE per turn window.
        The memo is per-turn state owned by `AgentLoop`, never by this shared
        assembler. *observer* is the per-request progress observer, forwarded to
        the pipeline for its shape-only retrieval progress event (D61).
        """
        scope_hash = scope_filter.compute_scope_hash(column_scope)
        span_cm = (
            tracing.chain_span(
                self._tracer, "context.assembly", attributes={"scope_hash": scope_hash}
            )
            if self._tracer is not None
            else nullcontext()
        )
        with span_cm as current_span:
            raw_trail = await self._session_store.load_trail(session_id)  # 1. load

            in_scope = scope_filter.filter_trail(  # 2. D44 filter
                raw_trail, column_scope, current_turn_index
            )

            # S2: compact_trail_async keeps a cache HIT synchronous/cheap and
            # only off-loads a cache-MISS summarizer call (e.g. a blocking LLM
            # round trip) to a worker thread, so it never stalls this loop.
            compaction: CompactionResult = await compact_trail_async(  # 3. D46 budget/compact
                in_scope,
                token_budget=self._history_token_budget,
                scope_hash=scope_hash,
                preview_row_count=self._preview_row_count,
                summarizer=self._summarizer,
                cache=self._cache,
            )

            messages = render_messages(  # 4. inject
                compaction, preview_row_count=self._preview_row_count
            )

            # 0. retrieval pre-injection (design §3.3): a SEPARATE, additive
            # pre-loop stage — runs alongside D50 history assembly, prepended
            # as one system message before history. Only when both the pipeline
            # and a user_message are present (else byte-identical to today).
            retrieved_counts = (0, 0)
            if self._retrieval is not None and user_message is not None:
                retrieved_counts = await self._prepend_retrieval(
                    messages,
                    user_message=user_message,
                    user_id=user_id,
                    column_scope=column_scope,
                    scope_hash=scope_hash,
                    retrieval_memo=retrieval_memo,
                    observer=observer,
                )

            dropped_by_scope_count = len(raw_trail) - len(in_scope)
            if current_span is not None:
                current_span.set_attribute("dropped_by_scope_count", dropped_by_scope_count)
                current_span.set_attribute("compaction_applied", compaction.summary_text is not None)
                current_span.set_attribute("compaction_cache_hit", compaction.cache_hit)
                current_span.set_attribute("retrieved_blueprints", retrieved_counts[0])
                current_span.set_attribute("retrieved_knowledge", retrieved_counts[1])

        return AssembledContext(
            messages=messages,
            dropped_by_scope_count=dropped_by_scope_count,
            compaction_applied=compaction.summary_text is not None,
            retrieved_counts=retrieved_counts,
        )

    async def _prepend_retrieval(
        self,
        messages: list[dict[str, Any]],
        *,
        user_message: str,
        user_id: str | None,
        column_scope: frozenset[str],
        scope_hash: str,
        retrieval_memo: dict[tuple[str, str], RetrievedContext] | None,
        observer: Observer | None,
    ) -> tuple[int, int]:
        """Run retrieval (memoized), render the block, and prepend it in place.

        Returns the shape-only `(blueprints, knowledge)` counts. Retrieval never
        raises (degrade-not-fail, design §2), so this never breaks assembly.
        """
        assert self._retrieval is not None  # guarded by the caller
        key = (user_message, scope_hash)
        retrieved: RetrievedContext | None = None
        if retrieval_memo is not None:
            retrieved = retrieval_memo.get(key)
        if retrieved is None:
            retrieved = await self._retrieval.retrieve(
                question=user_message,
                column_scope=column_scope,
                user_id=user_id,
                observer=observer,
            )
            if retrieval_memo is not None:
                retrieval_memo[key] = retrieved

        rendered = render_retrieved_context(retrieved)
        if rendered is not None:
            messages.insert(0, rendered)
        return (len(retrieved.thin_cards), len(retrieved.knowledge_hits))
