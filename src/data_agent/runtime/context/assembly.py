"""ContextAssembler — the interleaved context-assembly pipeline.

Load both streams (`tool_trail` + `messages`) -> D44 scope-filter each independently ->
interleave by a STABLE sort on `(turn_index, ts, stream_rank)`, where `stream_rank`
(user=0, trail=1, assistant=2) is only a tie-break for an identical `ts` -> insert the
date anchor, the retrieval block and the `analysisState` block at the SAME index, just
around the current question. The established path reads `anchor -> retrieval -> state ->
question`; behind the feature flag it reads `anchor -> state -> question -> prefetch
tool pair`, matching a valid user-then-assistant/tool exchange. Finally insert the base
prompt at index 0 as the SOLE `role:"system"` message.

Compaction is deliberately bypassed: every in-scope turn interleaves verbatim and
`fit_request_to_budget` is the only size bound. Each current-turn `ok` entry with `None`
provenance that `filter_trail` dropped is re-materialised as a non-data-bearing withheld
sentinel AT THE DROPPED ENTRY'S `ts` (D44 fail-closed fold). This assembler only ever
needs `column_scope`, never the JWT.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from data_agent.runtime.capabilities.prefetch import (
    render_capability_prefetch,
    route_uses_data_prefetch,
)
from data_agent.runtime.dispatch.denial_mapping import (
    ANSWER_JUDGE_REJECTED_CODE,
    ANSWER_TABLE_NO_TABLE_DESIGNATED_CODE,
    FINALIZATION_BLOCKED_PENDING_INTENTS_CODE,
)
from data_agent.runtime.observability import tracing
from data_agent.runtime.retrieval.render import (
    render_retrieved_context,
    render_retrieved_context_tool_entry,
)
from data_agent.runtime.sanitize import MAX_FIELD_CHARS, sanitize_text
from data_agent.runtime.session.models import live_analysis_state
from data_agent.runtime.session.store import SessionStore

from . import scope_filter
from .budget import _render_entry

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from opentelemetry.trace import Tracer

    from data_agent.runtime.capabilities.client import CapabilityPrefetch
    from data_agent.runtime.retrieval.models import RetrievedContext
    from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
    from data_agent.runtime.session.models import AnalysisState, TrailEntry, TurnMessage

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
# REWORDED with the trim-aware re-fetch exemption (`loop/agent_loop.py`), which
# changed what this text is allowed to claim.
#
# It used to say the result "is already available to you" whatever the state of the
# window — and that was sometimes FALSE: the guard fired on the persisted trail, so a
# result the budget had since trimmed out (or that D44 had replaced with a withheld
# sentinel) got this nudge anyway, telling the model to use something it could not
# read and offering no way to recover it. The loop now declines to dedup exactly
# those cases, so "it is in the messages above" is warranted, and the nudge says the
# stronger, now-true thing plus WHERE to look.
#
# The closing instruction is also tool-agnostic now. It used to say "proceed to
# runQuery or give your answer", which was written when the guarded set was
# schema-shaped; `getBlueprint` joined it, and for that read the correct next step is
# `runBlueprint`, not `runQuery`.
_REPEATED_IDEMPOTENT_READ_NUDGE = (
    "Duplicate read: you already made this exact call this turn, and its result is in "
    "the messages above (this response is not a new fetch). Re-requesting it does "
    "nothing — find that earlier result, use it, and take the next step of your "
    "analysis."
)


@dataclass(frozen=True)
class AssembledContext:
    """The result of one `ContextAssembler.assemble()` call."""

    messages: list[dict[str, Any]]
    dropped_by_scope_count: int
    # Slice-1 retrieval: shape-only counts of the pre-injected block (design
    # §12 "AssembledContext may gain retrieved_counts for the span"); `(0, 0)`
    # whenever retrieval did not run (unconfigured / no user_message) — the
    # unconfigured path is byte-identical either way.
    retrieved_counts: tuple[int, int] = (0, 0)


class ContextAssembler:
    """Wires `SessionStore` + the render/retrieval dependencies into the D50 pipeline."""

    def __init__(
        self,
        session_store: SessionStore,
        *,
        preview_row_count: int = 20,
        retrieval: RetrievalPipeline | None = None,
        retrieval_prefetch_tool_enabled: bool = False,
        capability_prefetch_provider: Callable[[str], Any] | None = None,
        base_system_prompt: str | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._session_store = session_store
        # Phase 1 bypasses D46 compaction: there is no history token budget, no
        # summarizer and no summary cache here — `assemble` interleaves every
        # in-scope turn verbatim and the downstream `fit_request_to_budget`
        # (loop/agent_loop.py) is the sole size bound.
        self._preview_row_count = preview_row_count
        # Always-present base instruction (`prompts.AGENT_SYSTEM_PROMPT`, wired
        # from settings by `app.py`). When set, `assemble` prepends it as the
        # FIRST `role:"system"` message AFTER retrieval pre-injection, so the
        # model always sees: base prompt -> retrieval cards -> history. It is a
        # static constant, so it is byte-stable across a D45 rebuild/resume, and
        # the downstream `fit_request_to_budget` pins it as the undroppable head.
        # `None` (Layer-1 tests, or the disabled toggle) reproduces the exact
        # prompt-less message list.
        self._base_system_prompt = base_system_prompt
        # Slice-1 retrieval (design §3.3): an OPTIONAL pre-loop stage. When
        # `None` (Layer-1 history-only tests, unconfigured deploy) `assemble`
        # is byte-identical to before this dependency existed — the whole
        # retrieval branch below is gated on both `retrieval is not None` AND a
        # non-`None` `user_message`, so no existing caller observes any change.
        self._retrieval = retrieval
        # Feature-flagged representation only; retrieval behavior and lifetime are
        # unchanged. False preserves the established byte-for-byte user-message path.
        self._retrieval_prefetch_tool_enabled = retrieval_prefetch_tool_enabled
        self._capability_prefetch_provider = capability_prefetch_provider
        # B5: optional — when wired (app.py's composition root), assemble()
        # emits one CHAIN span per call recording only non-sensitive shape
        # counters (trail entries loaded, dropped-by-scope count — design §7's
        # CHAIN row); `None` (Layer-1 tests) means no span is ever created.
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
        capability_memo: dict[str, CapabilityPrefetch] | None = None,
        withheld_call_ids: set[str] | None = None,
        observer: Observer | None = None,
    ) -> AssembledContext:
        """Assemble the canonical message list for one turn under *column_scope*.

                *current_turn_index* is threaded straight through to
                `scope_filter.filter_trail`; `None` preserves the all-strict D44 replay
                behaviour.

                *user_message*/*user_id*: with a `retrieval` pipeline injected AND a
                *user_message* given, the retrieved thin-cards/knowledge/user-memory block is
                associated with the LAST `user` message. The default representation is inserted
                before it as one `user` message; the feature-flagged assistant/tool prefetch pair
                is inserted after it. Neither adds a system message. Absent either input, no
                retrieval runs.

                *retrieval_memo* is a caller-owned dict memoizing the `RetrievedContext` by
                `(user_message, scope_hash)`, so the D45 per-round-trip rebuild recalls at most
                ONCE per turn window. It is per-turn state owned by `AgentLoop`, never by this
                shared assembler.

                *withheld_call_ids* is the same-lifecycle set of `tool_call_id`s whose
                `loop_result_withheld_provenance` diagnostic has already fired this turn; `None`
                disables the de-dup. Only the EVENT is de-duped — the sentinel injection itself
                is unconditional on every round-trip.
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

            # 3/4/5. Phase 1: NO compaction (no summary). Interleave the two
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

            # 6. THE DATE ANCHOR (issues-stack B2), FIRST of the three
            # before-the-question inserts. See `_turn_date_anchor` for where the date
            # comes from and why it is not `date.today()`.
            #
            # ORDER IS PRODUCED BY INSERTION ORDER, NOT BY ARITHMETIC. All three
            # blocks insert at `_last_user_index(messages)`, so each one lands
            # immediately before the question and pushes the previous ones EARLIER —
            # meaning the final frame is exactly the reverse of the insert sequence:
            #
            #     anchor -> retrieval cards -> analysis state -> the question
            #
            # The anchor therefore goes first to end up OUTERMOST. This block sat
            # between retrieval and the question for one review round while the
            # comments claimed otherwise; the fix is the ordering, not the comment,
            # because the retrieval cards and the state ledger are both things the
            # anchor frames rather than the other way round.
            if current_turn_index is not None:
                anchor = _turn_date_anchor(raw_messages, current_turn_index)
                if anchor is not None:
                    messages.insert(_last_user_index(messages), anchor)

            # 6a. retrieval (design §3.3): render this turn's retrieved thin-cards/
            # knowledge as ONE `user`-role block and insert it IMMEDIATELY BEFORE the
            # current-turn question (the last `user` message), so the current question
            # stays the last `user` message (fit's tail-pin) and the block reads as
            # this question's context. NON-system so the base prompt stays the sole
            # system message. Only when both the pipeline and a user_message are
            # present (else byte-identical to the retrieval-off path).
            capability_prefetch = None
            if self._capability_prefetch_provider is not None and user_message is not None:
                capability_prefetch = (
                    capability_memo.get(user_message) if capability_memo is not None else None
                )
                if capability_prefetch is None:
                    try:
                        capability_prefetch = await self._capability_prefetch_provider(user_message)
                    except Exception:
                        capability_prefetch = None
                    if capability_prefetch is not None and capability_memo is not None:
                        capability_memo[user_message] = capability_prefetch

            retrieved_counts = (0, 0)
            should_retrieve = capability_prefetch is None or route_uses_data_prefetch(
                capability_prefetch.route
            )
            if self._retrieval is not None and user_message is not None and should_retrieve:
                retrieved_counts = await self._insert_retrieval(
                    messages,
                    current_turn_index=current_turn_index,
                    user_message=user_message,
                    user_id=user_id,
                    column_scope=column_scope,
                    scope_hash=scope_hash,
                    retrieval_memo=retrieval_memo,
                    observer=observer,
                )

            if capability_prefetch is not None:
                rendered_capabilities = render_capability_prefetch(capability_prefetch)
                if rendered_capabilities is not None:
                    messages.insert(_last_user_index(messages), rendered_capabilities)

            # 6b. analysisState (Release 1, 03 §D): the live intent ledger,
            # rendered as ONE `user`-role block IMMEDIATELY BEFORE the current
            # question — inserted AFTER the retrieval block so it is the last
            # thing the model reads before the question itself.
            #
            # READ FRESH EVERY ROUND-TRIP, from the `doc` this method already
            # loaded: zero extra store reads, and correct by construction. It is
            # deliberately NOT threaded like `discovery_canonical`, which is
            # computed ONCE PER BUDGET WINDOW and reused unchanged; the state
            # changes WITHIN the window (every `updateAnalysisState` mutates it),
            # so copying that lifetime would mean the model never sees the ids it
            # was just assigned — the entire point of the initialize result.
            #
            # EPHEMERAL: never appended to `doc.messages`, so it cannot surface in
            # the `/session/history` transcript as something the user said.
            #
            # `live_analysis_state` is what makes a PRIOR turn's state invisible
            # here. That is necessary but NOT sufficient — the `updateAnalysisState`
            # TRAIL ENTRY carries the same descriptions in its `args` with
            # `frozenset()` provenance, so it is dropped separately by
            # `_is_stale_model_text_entry` below.
            #
            # SPLICE SEAM FOR 05: when a finalization nudge is also present, 05
            # owns the final ordering — locate the question, insert THIS block
            # immediately before it, then append the nudge at the TAIL. The nudge
            # must be appended after this insert, or it becomes the last `user`
            # message and this block lands after the question instead of before it.
            if current_turn_index is not None:
                state = live_analysis_state(doc, current_turn_index)
                if state is not None and state.intents:
                    messages.insert(
                        _last_user_index(messages), render_analysis_state_block(state)
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
                # Phase 1 bypasses compaction — no summary is ever produced, so this
                # is a constant `False`. KEPT deliberately: it is an OUTWARD telemetry
                # surface (design §7's CHAIN row, read in Phoenix), and dropping the
                # attribute would silently change what a dashboard/query sees. The
                # in-process `AssembledContext.compaction_applied` field it used to
                # mirror had no readers at all and was removed.
                current_span.set_attribute("compaction_applied", False)
                current_span.set_attribute("retrieved_blueprints", retrieved_counts[0])
                current_span.set_attribute("retrieved_knowledge", retrieved_counts[1])

        return AssembledContext(
            messages=messages,
            dropped_by_scope_count=dropped_by_scope_count,
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

                The merge key is `(turn_index, ts, stream_rank)` and the sort is STABLE, so the
                rank is only a tie-break for an identical `(turn_index, ts)` and insertion order
                survives an exact tie. Within a turn this yields question -> tool pairs ->
                answer, and an askUser mid-turn answer lands between the tool pairs its `ts`
                falls between.

                D94 fold: each current-turn `ok`+`None` entry that `filter_trail` dropped is
                re-materialised as a non-data-bearing withheld sentinel at the DROPPED ENTRY'S
                `ts`, and its de-duped diagnostic event fires here.
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
            if _is_stale_model_text_entry(entry, current_turn_index):
                continue
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
        """The current-turn `ok`+`None`-provenance entries `filter_trail` dropped (covers
                BOTH the raw `runQuery` and the `runBlueprint`/`_union_provenance` paths).

                Cross-turn `ok`+`None` stays dropped as history, with no sentinel. The
                `provenance is None` conjunct is belt-and-braces — such a current-turn entry can
                never survive `filter_trail`, so the id check already holds; do not "simplify"
                it away.
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
        current_turn_index: int | None,
        user_message: str,
        user_id: str | None,
        column_scope: frozenset[str],
        scope_hash: str,
        retrieval_memo: dict[tuple[str, str], RetrievedContext] | None,
        observer: Observer | None,
    ) -> tuple[int, int]:
        """Run retrieval (memoized), render the block, and insert it IMMEDIATELY BEFORE the
                current-turn question (the last `user` message; appended at the end when there
                is no question yet).

                Returns the shape-only `(blueprints, knowledge)` counts. Retrieval never raises
                (degrade-not-fail), so this never breaks assembly.
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

        question_index = _last_user_index(messages)
        if self._retrieval_prefetch_tool_enabled:
            entry = render_retrieved_context_tool_entry(
                retrieved, turn_index=current_turn_index if current_turn_index is not None else 0
            )
            if entry is not None:
                # A synthetic tool result must follow the user request that caused
                # the application prefetch. Canonicalization later expands this one
                # render item to assistant(tool_call) -> tool(result).
                messages.insert(min(question_index + 1, len(messages)), entry)
        else:
            rendered = render_retrieved_context(retrieved)
            if rendered is not None:
                messages.insert(question_index, rendered)
        return (len(retrieved.thin_cards), len(retrieved.knowledge_hits))

    def _emit_withheld_provenance_event(
        self,
        entry: TrailEntry,
        current_turn_index: int,
        withheld_call_ids: set[str] | None,
        observer: Observer | None,
    ) -> None:
        """Emit `loop_result_withheld_provenance` at most once per `tool_call_id` per budget
                window. The payload is non-sensitive — no SQL, column, cell values or scope token
                (D25/D61); `blueprint_id` is the model-supplied `id` on the `runBlueprint` path,
                `None` for a raw `runQuery`.
        """
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
        # `recordAssumptions` now carries DETERMINED-EMPTY (`frozenset()`)
        # provenance, so it is in scope under any `column_scope` and never reaches
        # the stranded path at all — this guard is unreachable in normal operation
        # and kept only as belt-and-braces against a regression to `None`.
        #
        # It USED to return `None` (undetermined) and therefore hit this path on
        # EVERY successful call, which is why the diagnostic was silenced here
        # rather than the sentinel being fixed: the model was shown "result
        # withheld … Do not retry" in place of its own confirmation every time it
        # recorded assumptions. The cause is fixed at the source in
        # `composite/record_assumptions.py`; this stays silent regardless.
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
        `len(messages)` when there is none yet. The retrieval block is inserted at this index
        so the question stays the last `user` message — the tail `fit_request_to_budget` pins.
    """
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return index
    return len(messages)


DATE_ANCHOR_PREFIX = "Today's date is "

# THE ANCHOR IS A FRAME, NOT A VALUE TO PASTE. Live turns copied the rendered day
# straight into SQL (`toDateTime64('2026-08-28 00:00:00', 6)` as a dateDiff bound),
# which the learning plane then froze into blueprint templates that go stale the next
# morning. The note rides the anchor rather than `AGENT_SYSTEM_PROMPT` because "this
# literal" needs the literal beside it — and because it is then unbudgeted against the
# prompt ceiling. `today()`/`now()` were probed live: both pass the SQL validator,
# scope enforcement and `explainQuery`.
DATE_ANCHOR_SQL_NOTE = (
    " Use it to interpret the question; in SQL express the current date or time as "
    "`today()`/`now()`, never as this literal."
)


def turn_date_anchor_day(
    raw_messages: Sequence[TurnMessage], current_turn_index: int
) -> str | None:
    """The ISO day this turn is anchored to (`"2026-08-26"`), or `None` when there is no
        usable one.

        THE SINGLE DERIVATION, shared by the model-facing anchor message below and by the
        answer judge's brief (09 §D.2). A second derivation is not a duplication risk in the
        abstract — it is a specific, silent failure: the judge would grade "this year"
        against a different today than the model was given, and would then fault a correct
        answer for a disagreement it created itself.

        See `_turn_date_anchor` for why the date comes from the turn's own first `user`
        message `ts` rather than `date.today()`, and why a malformed stamp yields `None`
        instead of a confidently-rendered non-date.
    """
    for message in raw_messages:
        if message.turn_index == current_turn_index and message.role == "user":
            day = message.ts[:10]
            try:
                date.fromisoformat(day)
            except (TypeError, ValueError):
                return None
            return day
    return None


def _turn_date_anchor(
    raw_messages: Sequence[TurnMessage], current_turn_index: int
) -> dict[str, Any] | None:
    """`Today's date is YYYY-MM-DD.` plus `DATE_ANCHOR_SQL_NOTE`, as ONE `user`-role
        message, or `None`.

        The model has no grounded present: without this, "last 6 months" or "this quarter"
        resolves against training-frozen time and nothing downstream can detect the wrong
        window — the SQL parses, the query runs, the grain verifies.

        THE DATE COMES FROM THE TURN'S OWN FIRST `user` MESSAGE `ts`, NOT `date.today()`.
        That stamp is written once when the turn opens and then persisted, so round-trip 1,
        round-trip 9, a budget-window rebuild and a next-morning resume all re-derive
        byte-identical bytes; `date.today()` would re-evaluate on every rebuild, so a turn
        spanning midnight would silently change what "today" means. For the same reason the
        anchor cannot live in the module-level `AGENT_SYSTEM_PROMPT`.

        `role: "user"`, not `system`, so the base prompt stays the SOLE `role:"system"`
        message. Reads the RAW messages, not the filtered ones: `user` messages always carry
        `frozenset()` provenance and are never dropped, and the raw stream cannot become
        empty for a reason unrelated to dates.

        Returns `None` when the turn has no `user` message yet, and when the stored `ts` is
        not a readable date. The slice is VALIDATED, not trusted — no anchor is better than
        confidently rendering `Today's date is not-a-dat.` from a malformed stamp.
    """
    # `ts` is produced by `_now_iso()` (`datetime.now(UTC).isoformat()`), so the first
    # 10 characters ARE the ISO date — the slice is the cheap path, and
    # `date.fromisoformat` is the guard that keeps a hand-built or corrupted stamp from
    # being rendered as if it were one. Both live in `turn_date_anchor_day`, which the
    # judge reads too, so the two can never disagree about what "today" is.
    day = turn_date_anchor_day(raw_messages, current_turn_index)
    if day is None:
        return None
    return {"role": "user", "content": f"{DATE_ANCHOR_PREFIX}{day}.{DATE_ANCHOR_SQL_NOTE}"}


def render_analysis_state_block(state: AnalysisState) -> dict[str, Any]:
    """Render the live `AnalysisState` as ONE `user`-role message.

        `role: "user"`, not `system`, so the base prompt stays the SOLE `role:"system"`
        message — the head-pin the total-request fit and the send-seam base-prompt invariant
        both depend on.

        Every `description` is structurally sanitised through the shared
        `runtime/sanitize.py` before interpolation: this is model-authored text re-entering
        model context, and a newline could otherwise fabricate a bullet, a header, or an
        instruction line inside this block. Deterministic — the same state renders
        byte-identically, so a D45 rebuild or resume produces the same request.
    """
    lines = [
        "[Analysis state — the deliverables you are tracking for the current question]",
        "",
        "Update these with updateAnalysisState as you resolve them. Every intent must "
        "end completed or blocked before you give your final answer.",
    ]
    for intent in state.intents:
        detail = intent.status
        if intent.reason_code:
            detail = f"{detail}: {intent.reason_code}"
        if intent.evidence_tool_call_id:
            detail = f"{detail}, evidence {intent.evidence_tool_call_id}"
        lines.append(
            f"- {intent.intent_id} [{detail}] "
            f"{sanitize_text(intent.description, MAX_FIELD_CHARS)}"
        )
    return {"role": "user", "content": "\n".join(lines)}


# Tools whose persisted `args` carry MODEL-AUTHORED TEXT derived from the user's
# question. Every such entry is dropped from a LATER turn's replayed context by
# `_is_stale_model_text_entry` below.
#
# `updateAnalysisState` joined `recordAssumptions` here in Release 1, and it is
# the SECOND of two required mechanisms, not a duplicate of the first:
# `live_analysis_state` suppresses the rendered STATE BLOCK cross-turn, but every
# `updateAnalysisState` call ALSO leaves a `TrailEntry` whose `args` carry the
# intent descriptions with `frozenset()` provenance — so `is_entry_in_scope` keeps
# it under ANY `column_scope`, in EVERY later turn, and `_render_entry` replays
# those args verbatim plus a `result_preview` that the tool's own contract
# requires to contain the full state, descriptions included.
_STALE_CROSS_TURN_TOOLS = frozenset({"recordAssumptions", "updateAnalysisState"})

# The SAME rule, keyed on the ERROR CODE instead of the tool name — because the
# third carrier of this text is not a tool of its own. The finalization refusal
# (05 §B.1) is persisted as an `answerWithTable` entry, and `answerWithTable`
# cannot join the set above: its SUCCESSFUL entries are the turn's answer and must
# replay. What must not replay is the REFUSAL specifically, whose `denial_detail`
# names every pending intent by id and description (`loop/finalization.py::_describe_pending`)
# and whose `args` carry the refused draft answer prose.
#
# This is the THIRD instance of one defect class in this release (README findings
# 9 and 11). If a fourth model-authored-text channel appears, extend one of these
# two sets — do not add a third predicate.
_STALE_CROSS_TURN_ERROR_CODES = frozenset(
    {
        FINALIZATION_BLOCKED_PENDING_INTENTS_CODE,
        # The FOURTH instance, and the set was extended rather than a third
        # predicate added, exactly as the paragraph above instructs. The
        # empty-designation refusal (08 §O) is persisted under `answerWithTable`
        # too, and its `args` likewise carry the model's refused draft answer —
        # prose written from warehouse rows, under a scope that may since have
        # narrowed. It only ever needs to survive its OWN turn: the whole point is
        # that the model reads it on the next round-trip and sends the table.
        ANSWER_TABLE_NO_TABLE_DESIGNATED_CODE,
        # The FIFTH and SIXTH, added with the answer judge (09 §G.2). Both are
        # persisted under `answerWithTable` and both carry the refused draft answer
        # in `args`, so both belong here for the reason the two above do.
        #
        # `ANSWER_JUDGE_REJECTED` carries a SECOND channel the others do not: its
        # `denial_detail` is the JUDGE'S OWN sentence about this answer, model-
        # authored prose written after reading warehouse rows. It is the most
        # scope-sensitive text this set holds, and it is useful for exactly one
        # round-trip.
        ANSWER_JUDGE_REJECTED_CODE,
    }
)


def _is_stale_model_text_entry(entry: TrailEntry, current_turn_index: int | None) -> bool:
    """True for a `_STALE_CROSS_TURN_TOOLS` / `_STALE_CROSS_TURN_ERROR_CODES` entry from
        any turn OTHER than the current one.

        Such an entry is dropped from the replayed context NOT because its provenance is
        unknown (it is `frozenset()` — these tools read no warehouse data) but because its
        `args` carry model-authored plain-English text that must not re-enter model context
        on a later turn, whose `column_scope` may since have narrowed: an assumption may
        name VALUES, and an intent description is derived from the user's own question. The
        finalization refusal (matched by ERROR CODE, since it is persisted under
        `answerWithTable`, whose successful entries must keep replaying) carries both.

        This is a REPLAY rule, not a provenance one. Encoding it as `None` provenance
        fail-closed drops the entry cross-turn but also replaces the model's own in-turn
        confirmation with the D94 withheld sentinel on every successful call. It is also why
        the refusal keeps `frozenset()` provenance: one `None` collapses
        `_compute_turn_provenance_union`, tagging that turn's final assistant message
        undetermined and dropping the user's answer from every later replay.

        Dropping the entry cannot orphan a tool message — both halves are synthesized from
        this one entry by `_tool_trail_entry_to_canonical`, so they leave together.
        `current_turn_index is None` drops every such entry, the same fail-safe direction.

        The RAW trail is untouched, so resume seeding, the UI's per-turn `assumptions` and
        the `analysisState` ledger all still read the persisted document.
    """
    if (
        entry.tool_name not in _STALE_CROSS_TURN_TOOLS
        and entry.error_code not in _STALE_CROSS_TURN_ERROR_CODES
    ):
        return False
    return current_turn_index is None or entry.turn_index != current_turn_index


def _build_withheld_sentinel_message(entry: TrailEntry) -> dict[str, Any]:
    """Build the render-shape tool message carrying the D94 sentinel (Part 1).

        The explicit `withheld_sentinel` flag (never set by the ordinary
        `budget._render_entry` shape) tells `_tool_trail_entry_to_canonical` to use this
        verbatim, data-free text as the tool result instead of a rendered payload — an
        explicit marker, so no future `_render_entry` field can silently reroute a normal
        tool entry to verbatim rendering.

        `args` carries the model's OWN current-turn arguments (the SQL it just sent) so the
        synthesized ASSISTANT-side `tool_call` correlates the withheld marker with the exact
        call that stranded; without it the model sees `runQuery({})`, cannot map "do not
        retry the identical call" to its SQL, and re-strands. The sentinel CONTENT stays the
        fixed, data-free string — no result preview, no column values.

        An `IDEMPOTENT_READ_ALREADY_SERVED_CODE` guard entry reuses this exact shape with
        the "you already fetched this, proceed" nudge as its verbatim content.
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
