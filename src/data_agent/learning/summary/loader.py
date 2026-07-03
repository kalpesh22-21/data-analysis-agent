"""load_session_summary — the deterministic, READ-ONLY normalizer (D27, §1).

A PURE function of `(SessionDoc, full-results-loaded-via-read_full_result, job)`.
No LLM, no network beyond the read-only full-result fetch, NO mutation of the
session (D72). Given the same doc + the same full-result bytes it returns an
identical `SessionSummary` (§1.3) — every §2/§3 rule is therefore Layer-1
unit-testable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from data_agent.runtime.session.models import ResultPreview, SessionDoc, TrailEntry

from ..models import LearningJob
from .lexicon import CORRECTION_PHRASES, matches_any, matches_confirmation
from .models import (
    AcceptedSignal,
    AskUserExchange,
    BlueprintUsage,
    FailedFixedSql,
    SessionSummary,
    ToolCallSummary,
    TurnSummary,
)

# MEDIUM-1: a transient store blip on the D46 full-result read must NOT dead-letter
# a healthy session. `read_full_result` already maps not-found → None (no raise),
# so any EXCEPTION here is a genuine error; retry a handful of times with linear
# backoff before letting a persistent failure propagate (→ reclaim → dead-letter).
_DEFAULT_READ_RETRIES = 3
_DEFAULT_READ_BACKOFF_SECONDS = 0.05

# Tool names whose successful call makes a turn an "answer turn" (§2.1) and which
# are lift/fix candidates.
_DATA_TOOLS = ("runQuery", "runBlueprint")
_SQL_TOOLS = ("runQuery", "explainQuery")
_FAILED_STATUSES = ("error", "denied")


class _FullResultReader(Protocol):
    async def read_full_result(
        self, session_id: str, result_full_ref: str
    ) -> dict[str, Any] | None: ...


def _extract_sql(tool_name: str, args: dict[str, Any]) -> str | None:
    if tool_name in _SQL_TOOLS:
        sql = args.get("sql")
        return sql if isinstance(sql, str) else None
    return None


def _shape_from_full(full: dict[str, Any]) -> tuple[tuple[str, ...], int | None]:
    """Columns + row_count SHAPE from a loaded full result (never the rows)."""
    columns = full.get("columns")
    rows = full.get("rows")
    col_tuple = tuple(columns) if isinstance(columns, list) else ()
    if "row_count" in full:
        return col_tuple, int(full["row_count"])
    if isinstance(rows, list):
        return col_tuple, len(rows)
    return col_tuple, None


def _shape_from_preview(preview: ResultPreview | None) -> tuple[tuple[str, ...], int | None]:
    if preview is None:
        return (), None
    return tuple(preview.columns), preview.row_count


def _askuser_question(args: dict[str, Any]) -> str:
    for key in ("question", "prompt", "text"):
        value = args.get(key)
        if isinstance(value, str):
            return value
    return ""


async def _read_full_result_with_retry(
    store: _FullResultReader,
    session_id: str,
    result_full_ref: str,
    *,
    retries: int,
    backoff: float,
    sleep: Callable[[float], Awaitable[None]],
) -> dict[str, Any] | None:
    """`store.read_full_result` with a bounded transient-error retry (MEDIUM-1).
    A not-found result already returns `None` inside the store (no raise), so a
    raised exception is retried; a persistent failure re-raises after *retries*
    attempts so the message is (correctly) not-acked → reclaimed → dead-lettered."""
    attempt = 0
    while True:
        try:
            return await store.read_full_result(session_id, result_full_ref)
        except Exception:  # noqa: BLE001 - transient store blip; retry then propagate
            attempt += 1
            if attempt > retries:
                raise
            await sleep(backoff * attempt)


async def _build_tool_call(
    entry: TrailEntry,
    session_id: str,
    store: _FullResultReader,
    *,
    retries: int,
    backoff: float,
    sleep: Callable[[float], Awaitable[None]],
) -> ToolCallSummary:
    columns: tuple[str, ...] = ()
    row_count: int | None = None
    full_loaded = False
    if entry.result_full_ref is not None:
        full = await _read_full_result_with_retry(
            store, session_id, entry.result_full_ref,
            retries=retries, backoff=backoff, sleep=sleep,
        )
        if full is not None:
            columns, row_count = _shape_from_full(full)
            full_loaded = True
    if not full_loaded:
        # Graceful degradation (§1.4): a missing/expired full result falls back
        # to the preview shape; never a crash.
        columns, row_count = _shape_from_preview(entry.result_preview)
    return ToolCallSummary(
        turn_index=entry.turn_index,
        tool_call_ref=entry.tool_call_id,
        tool_name=entry.tool_name,
        args=dict(entry.args),
        sql=_extract_sql(entry.tool_name, entry.args),
        status=entry.status,
        error_code=entry.error_code,
        provenance=entry.provenance,  # carried VERBATIM (frozenset is immutable)
        result_columns=columns,
        result_row_count=row_count,
        result_full_ref=entry.result_full_ref,
        full_result_loaded=full_loaded,
    )


def _build_turns(doc: SessionDoc) -> tuple[TurnSummary, ...]:
    # Group in first-seen turn order (deterministic). Per turn: at most one user +
    # one assistant message; tool_call_ids from trail entries of that turn, in
    # trail order.
    order: list[int] = []
    user_nl: dict[int, str] = {}
    assistant_text: dict[int, str] = {}
    tool_refs: dict[int, list[str]] = {}

    def _touch(turn_index: int) -> None:
        if turn_index not in tool_refs:
            tool_refs[turn_index] = []
            order.append(turn_index)

    for message in doc.messages:
        _touch(message.turn_index)
        if message.role == "user" and message.turn_index not in user_nl:
            user_nl[message.turn_index] = message.content
        elif message.role == "assistant" and message.turn_index not in assistant_text:
            assistant_text[message.turn_index] = message.content
    for entry in doc.tool_trail:
        _touch(entry.turn_index)
        tool_refs[entry.turn_index].append(entry.tool_call_id)

    return tuple(
        TurnSummary(
            turn_index=turn_index,
            user_nl=user_nl.get(turn_index),
            assistant_text=assistant_text.get(turn_index),
            tool_call_refs=tuple(tool_refs[turn_index]),
        )
        for turn_index in order
    )


def _failed_fixed_pairs(tool_calls: tuple[ToolCallSummary, ...]) -> tuple[FailedFixedSql, ...]:
    # §2.4: for each failed runQuery/runBlueprint, pair with the NEXT ok runQuery
    # in trail order. A failed query with no later ok runQuery is NOT a fix.
    pairs: list[FailedFixedSql] = []
    for i, tc in enumerate(tool_calls):
        if tc.tool_name not in _DATA_TOOLS or tc.status not in _FAILED_STATUSES:
            continue
        fix = next(
            (t for t in tool_calls[i + 1 :] if t.tool_name == "runQuery" and t.status == "ok"),
            None,
        )
        if fix is not None:
            pairs.append(
                FailedFixedSql(
                    failed_tool_call_ref=tc.tool_call_ref,
                    failed_sql=tc.sql,
                    fixed_tool_call_ref=fix.tool_call_ref,
                    fixed_sql=fix.sql,
                )
            )
    return tuple(pairs)


def _askuser_exchanges(
    doc: SessionDoc, tool_calls: tuple[ToolCallSummary, ...]
) -> tuple[AskUserExchange, ...]:
    # HIGH-1: pair each askUser with the user message the RESUME flow appends for
    # it. The resume appends the answer at the SAME turn_index as the askUser
    # (agent_loop.py: "an askUser answer is a later user message of the SAME
    # turn_index"; couchbase_store `next_turn_index = messages[-1].turn_index`,
    # NOT +1). So within a turn the user messages are, in LIST/append order:
    #   [originating question, answer-to-askUser#1, answer-to-askUser#2, ...]
    # The originating question (index 0) precedes the askUser and must NOT be
    # paired; successive askUsers in the turn take successive later user messages.
    # A strict `> turn_index` (the old code) never sees the same-turn answer, and
    # a turn-index SORT would wrongly grab the originating question — so we index
    # per-turn in list order and skip index 0.
    users_by_turn: dict[int, list[str]] = {}
    for message in doc.messages:
        if message.role == "user":
            users_by_turn.setdefault(message.turn_index, []).append(message.content)

    # Per turn, index 0 is the originating question; answers start at index 1.
    answer_cursor: dict[int, int] = {}
    exchanges: list[AskUserExchange] = []
    for tc in tool_calls:
        if tc.tool_name != "askUser":
            continue
        turn = tc.turn_index
        candidates = users_by_turn.get(turn, [])
        cursor = answer_cursor.get(turn, 1)
        if cursor < len(candidates):
            answer: str | None = candidates[cursor]
            answer_turn: int | None = turn
            answer_cursor[turn] = cursor + 1
        else:
            answer = None
            answer_turn = None
        exchanges.append(
            AskUserExchange(
                question_tool_call_ref=tc.tool_call_ref,
                question=_askuser_question(tc.args),
                answer=answer,
                answer_turn_index=answer_turn,
            )
        )
    return tuple(exchanges)


def _trailing_correction_after(doc: SessionDoc, turn_index: int) -> bool:
    return any(
        m.role == "user"
        and m.turn_index > turn_index
        and matches_any(m.content, CORRECTION_PHRASES)
        for m in doc.messages
    )


def _blueprint_usages(
    doc: SessionDoc, tool_calls: tuple[ToolCallSummary, ...]
) -> tuple[BlueprintUsage, ...]:
    # §2.5: a runBlueprint is `corrected` if status != ok OR a correction event
    # references a later turn; else `accepted`.
    usages: list[BlueprintUsage] = []
    for tc in tool_calls:
        if tc.tool_name != "runBlueprint":
            continue
        corrected = tc.status != "ok" or _trailing_correction_after(doc, tc.turn_index)
        bp_id = tc.args.get("blueprint_id")
        usages.append(
            BlueprintUsage(
                tool_call_ref=tc.tool_call_ref,
                blueprint_id=bp_id if isinstance(bp_id, str) else None,
                status=tc.status,
                outcome="corrected" if corrected else "accepted",
            )
        )
    return tuple(usages)


def _infer_accepted_signal(
    doc: SessionDoc, turns: tuple[TurnSummary, ...], tool_calls: tuple[ToolCallSummary, ...]
) -> AcceptedSignal | None:
    # §2.2 decision table, evaluated top-to-bottom (first match wins). `thumbs_up`
    # is NEVER emitted (no capture surface, D99) — range is
    # {no_correction, explicit_confirm, None}.
    ok_data_turns = {
        tc.turn_index for tc in tool_calls if tc.tool_name in _DATA_TOOLS and tc.status == "ok"
    }
    answer_turns = [
        t.turn_index for t in turns if t.assistant_text is not None and t.turn_index in ok_data_turns
    ]
    if not answer_turns:
        return None  # row 1: nothing successfully answered

    final = max(answer_turns)
    trailing_users = [
        m.content for m in doc.messages if m.role == "user" and m.turn_index > final
    ]
    has_correction = any(matches_any(text, CORRECTION_PHRASES) for text in trailing_users)
    has_confirmation = any(matches_confirmation(text) for text in trailing_users)

    # Correction-first ordering also resolves row 5 (ambiguous both) to None:
    # a message that is both a confirmation and a correction trips this branch.
    if has_correction:
        return None  # row 2 (+ row 5 ambiguity)
    if has_confirmation:
        return "explicit_confirm"  # row 3
    return "no_correction"  # row 4 (incl. idle-after-answer: no trailing user msg)


async def load_session_summary(
    doc: SessionDoc,
    store: _FullResultReader,
    *,
    job: LearningJob,
    read_retries: int = _DEFAULT_READ_RETRIES,
    read_backoff_seconds: float = _DEFAULT_READ_BACKOFF_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> SessionSummary:
    """Normalize *doc* (+ its D46 full results, via `store.read_full_result`) into
    a `SessionSummary`. Pure + read-only (D72): reads only; the caller's
    lifecycle CAS is the sole write. The D46 full-result reads retry transient
    store errors (MEDIUM-1) before propagating; *sleep* is injectable so Layer-1
    tests exercise the retry without real delay."""
    tool_calls = tuple(
        [
            await _build_tool_call(
                entry, doc.session_id, store,
                retries=read_retries, backoff=read_backoff_seconds, sleep=sleep,
            )
            for entry in doc.tool_trail
        ]
    )
    turns = _build_turns(doc)
    failed_fixed = _failed_fixed_pairs(tool_calls)
    askuser = _askuser_exchanges(doc, tool_calls)
    blueprint_usages = _blueprint_usages(doc, tool_calls)
    accepted_signal = _infer_accepted_signal(doc, turns, tool_calls)

    return SessionSummary(
        session_id=doc.session_id,
        user_id=job.user_id or "",
        scope_ref=job.scope_ref or "",
        trace_id=job.trace_id or "",
        content_hash=job.content_hash,
        turns=turns,
        tool_calls=tool_calls,
        blueprint_usages=blueprint_usages,
        askuser_exchanges=askuser,
        failed_fixed_sql=failed_fixed,
        accepted_signal=accepted_signal,
    )
