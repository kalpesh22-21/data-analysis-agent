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
    from collections.abc import Callable, Sequence

    from opentelemetry.trace import Tracer

    from data_agent.runtime.retrieval.models import RetrievedContext
    from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
    from data_agent.runtime.session.models import TrailEntry

    Observer = Callable[[str, dict[str, Any]], None]


# D94 Part 1 — the exact, non-data-bearing sentinel surfaced in place of a
# current-turn `ok`+`None` (undetermined-provenance) tool result that
# `scope_filter.filter_trail` correctly dropped. It carries ZERO data (no
# result_preview/result_full/column values), so it is PII-safe under ANY scope
# including empty/narrow, and it fills the dangling `tool_call`'s required result
# slot so the model stops re-emitting the identical call (design §2).
_WITHHELD_PROVENANCE_SENTINEL = (
    "result withheld: provenance could not be determined for this call, so its "
    "result cannot be shown. Do not retry the identical call — it will be withheld "
    "again. Try a different query or approach, or ask the user."
)

# Repeated-idempotent-read guard (generalizes D94 to "identical repeat of an
# already-served read"). The loop (`loop/agent_loop.py`) detects that the model
# re-issued an identical idempotent read it already served THIS turn and, instead
# of re-dispatching to the MCP, persists a data-free guard TrailEntry marked with
# `IDEMPOTENT_READ_ALREADY_SERVED_CODE`. That entry is `ok`+`None`-provenance, so
# `filter_trail` drops it exactly like a D94 stranded entry and it flows through
# the SAME stranded-detection + withheld-sentinel machinery below — only the
# sentinel TEXT (and the diagnostic event) branch on the marker. The nudge carries
# ZERO data (the real result is already in history under its own tool_call_id), so
# it is PII-safe under any scope, and it fills the dangling repeat call's tool-slot
# so the model stops re-fetching.
IDEMPOTENT_READ_ALREADY_SERVED_CODE = "IDEMPOTENT_READ_ALREADY_SERVED"
_REPEATED_IDEMPOTENT_READ_NUDGE = (
    "Duplicate read: you already requested this exact call this turn, so its result "
    "is already available to you (this response is not a new fetch). Re-requesting it "
    "does nothing — use the result you already have and proceed to runQuery or give "
    "your answer."
)


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
        base_system_prompt: str | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._session_store = session_store
        self._history_token_budget = history_token_budget
        self._preview_row_count = preview_row_count
        self._summarizer = summarizer
        self._cache = cache if cache is not None else SummaryCache()
        # Always-present base instruction (`prompts.AGENT_SYSTEM_PROMPT`, wired
        # from settings by `app.py`). When set, `assemble` prepends it as the
        # FIRST `role:"system"` message AFTER retrieval pre-injection, so the
        # model always sees: base prompt -> retrieval cards -> history. It is a
        # static constant inserted post-compaction, so it is BOTH exempt from
        # the history-token-budget trimming AND byte-stable across a D45 rebuild/
        # resume. `None` (Layer-1 tests, or the disabled toggle) reproduces the
        # exact prompt-less message list.
        self._base_system_prompt = base_system_prompt
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
        withheld_call_ids: set[str] | None = None,
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

        *withheld_call_ids* (D94 Part 2, turn-local, sibling to *retrieval_memo*):
        a caller-owned `set[str]` of `tool_call_id`s for which the
        `loop_result_withheld_provenance` diagnostic has already been emitted this
        turn, so the D45 per-round-trip rebuild fires that event AT MOST ONCE per
        stranded call rather than once per remaining budget window. Owned by
        `AgentLoop`, reset per budget window (same lifecycle as *retrieval_memo*);
        `None` (Layer-1 tests) simply disables the de-dup. The sentinel *injection*
        itself (Part 1) is unconditional every round-trip — only the observer event
        is de-duped.
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

            # 4b. D94 Part 1/2 — sentinel injection for current-turn `ok`+`None`
            # entries that `filter_trail` just dropped (undetermined provenance).
            # A non-data-bearing tool result is injected in the dropped entry's
            # slot to break the retry-until-budget-cap loop, and a diagnostic
            # observer event is emitted (de-duped). Runs BEFORE retrieval
            # pre-injection so the retrieval block still leads `messages`.
            self._inject_withheld_provenance_sentinels(
                messages,
                raw_trail=raw_trail,
                in_scope=in_scope,
                current_turn_index=current_turn_index,
                withheld_call_ids=withheld_call_ids,
                observer=observer,
            )

            # 0. retrieval pre-injection (design §3.3): a SEPARATE, additive
            # pre-loop stage — runs alongside D50 history assembly, prepended
            # as one `user`-role prior-context message before history (NON-system
            # so the base prompt stays the sole system message). Only when both the
            # pipeline and a user_message are present (else byte-identical to today).
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

            # 0b. base system prompt (always-present leading instruction): the
            # LAST prepend so it precedes the retrieval block and history, and the
            # SOLE `role: "system"` message (retrieval + summary are both demoted to
            # `user` so nothing competes with these base instructions). It is
            # inserted here — after `render_messages`/compaction has
            # already run and the budget walk is complete — so it can never be
            # trimmed by the history-token budget. As a static constant it keeps
            # `assemble` byte-identical across the D45 per-round-trip rebuild/resume.
            if self._base_system_prompt:
                messages.insert(0, {"role": "system", "content": self._base_system_prompt})

            dropped_by_scope_count = len(raw_trail) - len(in_scope)
            if current_span is not None:
                current_span.set_attribute("dropped_by_scope_count", dropped_by_scope_count)
                current_span.set_attribute(
                    "compaction_applied", compaction.summary_text is not None
                )
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

    def _inject_withheld_provenance_sentinels(
        self,
        messages: list[dict[str, Any]],
        *,
        raw_trail: Sequence[TrailEntry],
        in_scope: Sequence[TrailEntry],
        current_turn_index: int | None,
        withheld_call_ids: set[str] | None,
        observer: Observer | None,
    ) -> None:
        """D94 Part 1/2 — surface a non-data-bearing sentinel for each current-turn
        `ok`+`None` entry that `filter_trail` dropped, and emit the diagnostic event.

        Stranded predicate (design §2, covers BOTH the raw `runQuery` and the
        `runBlueprint`/`_union_provenance`→`None` paths with one condition):
        `entry.turn_index == current_turn_index` AND `entry.status == "ok"` AND
        `entry.provenance is None` AND the entry was dropped (not in *in_scope*).
        Cross-turn `ok`+`None` stays dropped as history (no sentinel, no event).

        The sentinel is keyed to the stranded entry's `tool_call_id` and reinserted
        into its chronological slot (by trail order) so the assistant `tool_call` it
        answers keeps a valid matching tool result for the OpenAI API. It carries
        ONLY `_WITHHELD_PROVENANCE_SENTINEL` — never any field of the dropped entry's
        data — so the fail-closed invariant holds under any scope. Injection is
        unconditional every round-trip; the observer event is de-duped via
        *withheld_call_ids* (once per `tool_call_id` per budget window).
        """
        if current_turn_index is None:
            return
        # `id(entry)` membership relies on `filter_trail` returning the SAME
        # `TrailEntry` objects it was handed (it does — a filtered sub-list, no
        # copies). The `provenance is None` conjunct is belt-and-braces: an
        # `ok`+`None` current-turn entry can NEVER survive `filter_trail` (it is
        # not status-exempt and `is_provenance_in_scope(None, ...)` is always
        # False), so `id(entry) not in in_scope_ids` already holds — do not
        # "simplify" the predicate by dropping either conjunct.
        in_scope_ids = {id(entry) for entry in in_scope}
        stranded = [
            entry
            for entry in raw_trail
            if entry.turn_index == current_turn_index
            and entry.status == "ok"
            and entry.provenance is None
            and id(entry) not in in_scope_ids
        ]
        if not stranded:
            return

        trail_order = {entry.tool_call_id: index for index, entry in enumerate(raw_trail)}
        leading: list[dict[str, Any]] = []
        tool_messages: list[dict[str, Any]] = []
        for message in messages:
            (tool_messages if message["role"] == "tool" else leading).append(message)

        for entry in stranded:
            tool_messages.append(_build_withheld_sentinel_message(entry))
            self._emit_withheld_provenance_event(
                entry, current_turn_index, withheld_call_ids, observer
            )

        # Re-sort so each sentinel occupies the ordinal slot its dropped entry
        # would have held; survivors are already chronological, so this is stable.
        tool_messages.sort(key=lambda m: trail_order.get(m["tool_call_id"], len(raw_trail)))
        messages[:] = leading + tool_messages

    def _emit_withheld_provenance_event(
        self,
        entry: TrailEntry,
        current_turn_index: int,
        withheld_call_ids: set[str] | None,
        observer: Observer | None,
    ) -> None:
        """Emit `loop_result_withheld_provenance` (D94 Part 2) at most once per
        `tool_call_id` per budget window. Payload is non-sensitive — NO SQL / column / cell
        values / scope token (D25/D61 parity); `blueprint_id` is the model-supplied
        `id` arg on the `runBlueprint` path, `None` for a raw `runQuery`."""
        if observer is None:
            return
        # The repeated-idempotent-read guard reuses this stranded-detection path
        # (its entry is also `ok`+`None`), but its telemetry is owned SOLELY by the
        # loop's guard-decision site (`loop/agent_loop.py`), which emits
        # `loop_repeated_idempotent_read_guarded` exactly once per guarded call.
        # This render-time path must therefore stay SILENT for a guard entry —
        # otherwise it would double-emit and, because `withheld_call_ids` resets
        # per budget window, re-fire once per window for the same guarded call.
        if entry.error_code == IDEMPOTENT_READ_ALREADY_SERVED_CODE:
            return
        # `recordAssumptions` is intentionally `ok`+`None` provenance (it carries no
        # warehouse data and stays out of replay — see
        # `_compute_turn_provenance_union`'s carve-out + ui-assumptions-contract.md).
        # It rides this same stranded-sentinel path to keep its tool_call paired,
        # but firing `loop_result_withheld_provenance` on EVERY normal use would be
        # routine noise — stay silent for it, exactly like the idempotent-read guard.
        if entry.tool_name == "recordAssumptions":
            return
        if withheld_call_ids is not None:
            if entry.tool_call_id in withheld_call_ids:
                return
            withheld_call_ids.add(entry.tool_call_id)
        blueprint_id = entry.args.get("id") if entry.tool_name == "runBlueprint" else None
        observer(
            "loop_result_withheld_provenance",
            {
                "tool_name": entry.tool_name,
                "turn_index": current_turn_index,
                "tool_call_id": entry.tool_call_id,
                "blueprint_id": blueprint_id,
                "reason": "provenance_undetermined",
            },
        )


def _build_withheld_sentinel_message(entry: TrailEntry) -> dict[str, Any]:
    """Build the render-shape tool message carrying the D94 sentinel (Part 1).

    The explicit `withheld_sentinel` flag (never set by the ordinary
    `budget.render_messages` shape) signals
    `loop/agent_loop.py::_tool_trail_entry_to_canonical` to use this verbatim,
    data-free text as the tool result instead of a rendered payload — an explicit
    marker so a future `_render_entry` field can never silently reroute a normal
    tool entry to verbatim rendering.

    `args` carries the model's OWN current-turn arguments (the SQL it just sent) so
    the synthesized ASSISTANT-side `tool_call` correlates the withheld marker with
    the exact call that stranded — without it the model sees `runQuery({})` and
    cannot map "do not retry the identical call" to its SQL, re-emitting and
    re-stranding (the exact hang D94 fixes). PII-consistent: args are the model's
    own output, and the current-turn denied-entry exemption already replays full
    args via `budget.py::_render_entry`. The sentinel *content* stays the fixed,
    data-free string — no result_preview/result_full/column values.

    A `IDEMPOTENT_READ_ALREADY_SERVED_CODE` guard entry (a repeat idempotent read
    the loop declined to re-dispatch) reuses this exact shape but with the "you
    already fetched this, proceed" nudge as its verbatim, data-free content.
    """
    content = (
        _REPEATED_IDEMPOTENT_READ_NUDGE
        if entry.error_code == IDEMPOTENT_READ_ALREADY_SERVED_CODE
        else _WITHHELD_PROVENANCE_SENTINEL
    )
    return {
        "role": "tool",
        "tool_call_id": entry.tool_call_id,
        "tool_name": entry.tool_name,
        "args": dict(entry.args),
        "withheld_sentinel": True,
        "content": content,
    }
