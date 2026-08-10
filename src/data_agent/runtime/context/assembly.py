"""ContextAssembler — the interleaved context-assembly pipeline (Phase 1).

    1. load      — `SessionStore.get_or_create_session(session_id)`, reading BOTH
                   `doc.tool_trail` (tool pairs) AND `doc.messages` (dialogue).
    2. filter    — D44 scope re-filter of EACH stream independently, order-preserving
                   (`scope_filter.filter_trail` + `scope_filter.filter_messages`).
    3. interleave — merge the two streams by a STABLE SORT on the key
                   `(turn_index, ts, stream_rank)` so tool results land chronologically
                   next to the question that triggered them (design "true chronological
                   interleave by turn"). `stream_rank` (user=0, trail=1, assistant=2) is
                   only a tie-break for identical `ts`; `ts` (`_now_iso()`,
                   lexicographically sortable) drives real order.
    4. retrieval — the retrieved thin-cards/knowledge block is inserted as ONE
                   `user`-role message IMMEDIATELY BEFORE the LAST `user` message
                   (the current question — or, on an askUser resume, the
                   clarification answer). It reads as this question's context and
                   stays inside the current turn, which `fit_request_to_budget` pins
                   as a whole (from the current turn's FIRST `user` message through
                   the end), so neither the question nor this block is dropped.
    5. base      — the base system prompt is inserted at index 0, the SOLE
                   `role:"system"` message.

Phase 1 deliberately BYPASSES compaction (no summary): every in-scope turn
interleaves verbatim and `fit_request_to_budget` (downstream, in
`loop/agent_loop.py`) is the sole size bound. The compaction machinery in
`context/budget.py` is retained but no longer invoked here.

D44 fail-closed folding (design §5): each current-turn `ok`+`None`-provenance
entry that `filter_trail` dropped is re-materialised as a non-data-bearing
withheld sentinel AT THE DROPPED ENTRY'S `ts`, so it lands in its chronological
slot within the current turn (replacing the former post-hoc tool-message sort).

`ContextAssembler` only ever needs `column_scope`, never the JWT (design §2:
"scope only, no jwt needed here").
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
    Summarizer,
    SummaryCache,
    _render_entry,
    default_summarizer,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from opentelemetry.trace import Tracer

    from data_agent.runtime.retrieval.models import RetrievedContext
    from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
    from data_agent.runtime.session.models import TrailEntry, TurnMessage

    Observer = Callable[[str, dict[str, Any]], None]

# Interleave tie-break ranks (design "merge by (turn_index, ts, stream_rank)"):
# used ONLY to order items that share an identical `(turn_index, ts)`. `ts` drives
# real order; within a single instant this yields question -> tool pairs -> answer.
# `ts` is wall-clock (`_now_iso`), so it is the orderer but not guaranteed
# monotonic — `turn_index` dominates the sort, so any clock skew/backward-step can
# only misorder items WITHIN one turn, never across turns (see `_now_iso`).
_STREAM_RANK_USER = 0
_STREAM_RANK_TRAIL = 1
_STREAM_RANK_ASSISTANT = 2


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
    # Always `False` in Phase 1 — the interleave path bypasses compaction (no
    # summary). Retained for the CHAIN span / caller-shape stability; a later phase
    # that reintroduces a compaction summary will set it True again.
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
        # PHASE-1 INERT (retained, UNREAD by `assemble`): `history_token_budget`,
        # `summarizer` and `cache` drove the D46 compaction that Phase 1 bypasses —
        # `assemble` no longer calls `compact_trail_async`, so none of these three
        # affect the assembled request today (the downstream `fit_request_to_budget`
        # is the sole size bound). They are kept on the constructor so the phase that
        # reintroduces a compaction summary can re-wire them without a signature
        # change; do NOT assume setting `history_token_budget` shrinks history now.
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
        the retrieved thin-cards/knowledge/user-memory block is inserted as ONE
        `user`-role message IMMEDIATELY BEFORE the LAST `user` message (the current
        question — or an askUser clarification answer on a resume) — NOT a system
        message, so the base prompt stays the SOLE leading `role:"system"` message
        (the head-pin the total-request fit and the send-seam base-prompt invariant
        both depend on). The block sits inside the current turn, which
        `fit_request_to_budget` pins as a whole (from the current turn's FIRST
        `user` message through the end), so neither the originating question nor
        this block is ever dropped. Absent either, no retrieval runs and the
        returned context is byte-identical to the pre-retrieval behavior.

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
            # 1. load BOTH streams from the SAME doc (single get_or_create_session):
            # the tool pairs AND the user/assistant dialogue that interleave by turn.
            doc = await self._session_store.get_or_create_session(session_id)
            raw_trail = doc.tool_trail
            raw_messages = doc.messages

            # 2. D44 filter EACH stream independently, order-preserving.
            in_scope_trail = scope_filter.filter_trail(
                raw_trail, column_scope, current_turn_index
            )
            in_scope_messages = scope_filter.filter_messages(raw_messages, column_scope)

            # 3/4/5. Phase 1: NO compaction (summary_text = None). Interleave the two
            # in-scope streams verbatim by (turn_index, ts, stream_rank), folding the
            # D94 withheld/idempotent-read sentinels into their chronological slot.
            messages = self._interleave(
                in_scope_trail=in_scope_trail,
                in_scope_messages=in_scope_messages,
                raw_trail=raw_trail,
                current_turn_index=current_turn_index,
                withheld_call_ids=withheld_call_ids,
                observer=observer,
            )

            # 6. retrieval (design §3.3): render this turn's retrieved thin-cards/
            # knowledge as ONE `user`-role block and insert it IMMEDIATELY BEFORE the
            # current-turn question (the last `user` message), so the current question
            # stays the last `user` message (fit's tail-pin) and the block reads as
            # this question's context. NON-system so the base prompt stays the sole
            # system message. Only when both the pipeline and a user_message are
            # present (else byte-identical to the retrieval-off path).
            retrieved_counts = (0, 0)
            if self._retrieval is not None and user_message is not None:
                retrieved_counts = await self._insert_retrieval(
                    messages,
                    user_message=user_message,
                    user_id=user_id,
                    column_scope=column_scope,
                    scope_hash=scope_hash,
                    retrieval_memo=retrieval_memo,
                    observer=observer,
                )

            # 7. base system prompt at index 0 — the SOLE `role:"system"` message.
            # Inserted last so it precedes the interleaved history + retrieval block.
            # As a static constant it keeps `assemble` byte-stable across the D45
            # per-round-trip rebuild/resume, and it can never be trimmed here (the
            # downstream `fit_request_to_budget` pins it as the head).
            if self._base_system_prompt:
                messages.insert(0, {"role": "system", "content": self._base_system_prompt})

            dropped_by_scope_count = len(raw_trail) - len(in_scope_trail)
            if current_span is not None:
                current_span.set_attribute("dropped_by_scope_count", dropped_by_scope_count)
                # Phase 1 bypasses compaction — no summary is ever produced.
                current_span.set_attribute("compaction_applied", False)
                current_span.set_attribute("retrieved_blueprints", retrieved_counts[0])
                current_span.set_attribute("retrieved_knowledge", retrieved_counts[1])

        return AssembledContext(
            messages=messages,
            dropped_by_scope_count=dropped_by_scope_count,
            compaction_applied=False,
            retrieved_counts=retrieved_counts,
        )

    def _interleave(
        self,
        *,
        in_scope_trail: Sequence[TrailEntry],
        in_scope_messages: Sequence[TurnMessage],
        raw_trail: Sequence[TrailEntry],
        current_turn_index: int | None,
        withheld_call_ids: set[str] | None,
        observer: Observer | None,
    ) -> list[dict[str, Any]]:
        """Merge the two in-scope streams into ONE chronological render list.

        The merge key is `(turn_index, ts, stream_rank)` and the sort is STABLE, so
        for items sharing an identical `(turn_index, ts)` the `stream_rank`
        (user=0, trail=1, assistant=2) is a deterministic tie-break — and for items
        sharing the SAME key (e.g. two trail entries written in the same instant in a
        test) insertion order is preserved. `ts` (an ISO-8601 `_now_iso()` stamp on
        both `TurnMessage` and `TrailEntry`) drives real order, so within a turn this
        yields question(earliest ts) -> tool pairs -> answer(latest ts), and an
        askUser mid-turn answer lands between the tool pairs its ts falls between.

        D94 fold: each current-turn `ok`+`None` entry that `filter_trail` dropped is
        re-materialised as a non-data-bearing withheld sentinel at the DROPPED
        ENTRY'S `ts` (so it occupies its chronological slot in the current turn),
        and its de-duped diagnostic event fires here.
        """
        # (turn_index, ts, stream_rank, render_dict) — sorted by the first three.
        items: list[tuple[int, str, int, dict[str, Any]]] = []

        # Dialogue stream (inserted in doc order so identical-key ties are stable).
        for message in in_scope_messages:
            rank = _STREAM_RANK_USER if message.role == "user" else _STREAM_RANK_ASSISTANT
            items.append(
                (message.turn_index, message.ts, rank, {"role": message.role, "content": message.content})
            )

        # Trail stream + folded D94 sentinels, walked in raw_trail order so
        # identical-`ts` entries keep their original ordinal order.
        in_scope_ids = {id(entry) for entry in in_scope_trail}
        stranded_ids = {
            id(entry)
            for entry in self._stranded_current_turn_entries(
                raw_trail, in_scope_ids, current_turn_index
            )
        }
        for entry in raw_trail:
            if id(entry) in in_scope_ids:
                items.append(
                    (entry.turn_index, entry.ts, _STREAM_RANK_TRAIL,
                     _render_entry(entry, self._preview_row_count))
                )
            elif id(entry) in stranded_ids:
                items.append(
                    (entry.turn_index, entry.ts, _STREAM_RANK_TRAIL,
                     _build_withheld_sentinel_message(entry))
                )
                self._emit_withheld_provenance_event(
                    entry, current_turn_index, withheld_call_ids, observer
                )
            # else: dropped by scope and not stranded -> absent (no orphan).

        items.sort(key=lambda item: (item[0], item[1], item[2]))
        return [render for (_turn, _ts, _rank, render) in items]

    @staticmethod
    def _stranded_current_turn_entries(
        raw_trail: Sequence[TrailEntry],
        in_scope_ids: set[int],
        current_turn_index: int | None,
    ) -> list[TrailEntry]:
        """The current-turn `ok`+`None`-provenance entries `filter_trail` dropped
        (design §2 stranded predicate — covers BOTH the raw `runQuery` and the
        `runBlueprint`/`_union_provenance`→`None` paths). Cross-turn `ok`+`None`
        stays dropped as history (no sentinel). The `provenance is None` conjunct is
        belt-and-braces: such a current-turn entry can never survive `filter_trail`,
        so `id(entry) not in in_scope_ids` already holds — do not "simplify" it away.
        """
        if current_turn_index is None:
            return []
        return [
            entry
            for entry in raw_trail
            if entry.turn_index == current_turn_index
            and entry.status == "ok"
            and entry.provenance is None
            and id(entry) not in in_scope_ids
        ]

    async def _insert_retrieval(
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
        """Run retrieval (memoized), render the block, and insert it IMMEDIATELY
        BEFORE the current-turn question (the last `user` message; appended at the
        end when there is no question yet — e.g. a Layer-1 assemble with no dialogue).

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
            messages.insert(_last_user_index(messages), rendered)
        return (len(retrieved.thin_cards), len(retrieved.knowledge_hits))

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


def _last_user_index(messages: list[dict[str, Any]]) -> int:
    """Index of the last `role:"user"` render item (the current-turn question), or
    `len(messages)` when there is none yet. The retrieval block is inserted at this
    index so it lands IMMEDIATELY BEFORE the current question, keeping that question
    the last `user` message (the tail `fit_request_to_budget` pins)."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return index
    return len(messages)


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
