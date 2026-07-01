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
    from opentelemetry.trace import Tracer


@dataclass(frozen=True)
class AssembledContext:
    """The result of one `ContextAssembler.assemble()` call."""

    messages: list[dict[str, Any]]
    dropped_by_scope_count: int
    compaction_applied: bool


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
        tracer: Tracer | None = None,
    ) -> None:
        self._session_store = session_store
        self._history_token_budget = history_token_budget
        self._preview_row_count = preview_row_count
        self._summarizer = summarizer
        self._cache = cache if cache is not None else SummaryCache()
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
    ) -> AssembledContext:
        """*current_turn_index* (turn-scoped continuity, 2026-07-01, optional):
        threaded straight through to `scope_filter.filter_trail` — see that
        function's docstring. Default `None` preserves the exact pre-existing
        all-strict D44 replay behavior; this is what the QA-locked
        `tests/runtime/provenance/test_fail_closed_replay_adversarial.py`
        still exercises, unchanged.
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

            dropped_by_scope_count = len(raw_trail) - len(in_scope)
            if current_span is not None:
                current_span.set_attribute("dropped_by_scope_count", dropped_by_scope_count)
                current_span.set_attribute("compaction_applied", compaction.summary_text is not None)
                current_span.set_attribute("compaction_cache_hit", compaction.cache_hit)

        return AssembledContext(
            messages=messages,
            dropped_by_scope_count=dropped_by_scope_count,
            compaction_applied=compaction.summary_text is not None,
        )
