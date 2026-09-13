"""Pure projection of a persisted `SessionDoc` into the `GET /session/history` transcript.

Load-bearing order: the two D44 filters run over this request's `column_scope` BEFORE
any serialization — `filter_messages` drops answers whose provenance is out of scope or
undetermined, and `filter_trail` (with `current_turn_index=None`, so every entry gets the
strict check) drops out-of-scope tool calls. Survivors are joined INDEPENDENTLY by
`turn_index`: an answer may be withheld while one of its tool calls survives. User
messages always survive, so a turn is never wholly dropped. Inline-only — a
`runBlueprint` node's SQL lives behind a KV pointer, so its `sql` projects as `null` and
no KV de-reference happens on the read path.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from data_agent.runtime.composite.answer_with_table import (
    TOOL_NAME as ANSWER_TABLE_TOOL_NAME,
)
from data_agent.runtime.composite.answer_with_table import (
    AnswerTable,
    BlueprintRun,
    enrich_table,
    finalize_designations,
    is_answer_table_in_scope,
    resolve_designations,
    terminal_sql_by_id,
)
from data_agent.runtime.composite.record_assumptions import fold_assumptions
from data_agent.runtime.context.scope_filter import filter_messages, filter_trail
from data_agent.runtime.session.models import PauseCheckpoint, TrailEntry, TurnMessage


def _noop_observer(event: str, payload: dict[str, Any]) -> None:  # pragma: no cover - default
    return None


def _project_provenance(
    provenance: frozenset[tuple[str, str]] | None,
) -> list[str] | None:
    """Project a provenance set to a sorted list of `"db.table.column"` strings, identical
    to `_outcome_to_dict` (`runtime/app.py`). `None` (undetermined) -> `null`;
    `frozenset()` (determined-empty) -> `[]`.
    """
    if provenance is None:
        return None
    return sorted(f"{db_table}.{column}" for db_table, column in provenance)


def _project_tool_call(entry: TrailEntry) -> dict[str, Any]:
    """Project one surviving `TrailEntry` to a `tool_calls[]` element.

    `sql` is `args["sql"]` for `runQuery`; `null` for `runBlueprint` (its node SQL is
    behind a KV pointer, not inline) and for any tool that carries no `sql` argument.
    """
    sql = None if entry.tool_name == "runBlueprint" else entry.args.get("sql")
    return {
        "tool_name": entry.tool_name,
        "sql": sql,
        "result_table": entry.result_preview.to_doc() if entry.result_preview else None,
        "provenance": _project_provenance(entry.provenance),
    }


def project_history(
    messages: Sequence[TurnMessage],
    trail: Sequence[TrailEntry],
    column_scope: frozenset[str],
    pause_checkpoint: PauseCheckpoint | None,
    blueprint_terminal_sql: Mapping[str, str] | None = None,
    blueprint_runs: Mapping[str, BlueprintRun] | None = None,
    observer: Callable[[str, dict[str, Any]], None] = _noop_observer,
) -> dict[str, Any]:
    """Project the persisted `messages`/`tool_trail` into the transcript shape under
    *column_scope*, applying the two D44 filters first.

    Returns `{"turns": [...], "pending_question": ...}` ordered by `turn_index`
    ascending; the caller wraps it with `session_id`. Pure: no I/O, no store read, no
    KV de-reference.
    """
    surviving_messages = filter_messages(messages, column_scope)
    # A read has NO in-progress turn — pass `current_turn_index=None` so every
    # trail entry is subject to the strict `is_entry_in_scope` check (§2). The
    # two filters run INDEPENDENTLY; the join below never couples them.
    surviving_trail = filter_trail(trail, column_scope, current_turn_index=None)

    # Anchor turns on the user question — always kept (§2.1), so a turn always
    # renders at least its question. First user message per turn is the question
    # (a resumed turn appends its answer as a second user message at the SAME
    # turn_index — the original question wins).
    questions: dict[int, str] = {}
    turn_order: list[int] = []
    answers: dict[int, TurnMessage] = {}
    for message in surviving_messages:
        if message.role == "user":
            if message.turn_index not in questions:
                questions[message.turn_index] = message.content
                turn_order.append(message.turn_index)
        elif message.role == "assistant":
            answers[message.turn_index] = message

    tools_by_turn: dict[int, list[TrailEntry]] = {}
    for entry in surviving_trail:
        tools_by_turn.setdefault(entry.turn_index, []).append(entry)

    # recordAssumptions (docs/decisions/ui-assumptions-contract.md): assumptions
    # are MODEL-authored PLAIN ENGLISH — by contract they carry no warehouse data
    # (no cell values, no column names). So they are NOT scope-gated per trail
    # entry like `tool_calls` are; instead they inherit the ANSWER's scope
    # treatment exactly (like `answer` / `pending_question` text). We therefore
    # gather them from the RAW (unfiltered) trail — a `recordAssumptions` entry
    # has `None` provenance and would be dropped by `filter_trail`, which is
    # irrelevant here — and only SURFACE a turn's assumptions when that turn's
    # assistant answer survives the message scope filter (`assistant is not None`
    # below). If the answer is withheld, its assumptions are withheld with it.
    raw_assumptions_by_turn: dict[int, list[str]] = {}
    for entry in trail:
        if entry.status == "ok" and entry.tool_name == "recordAssumptions":
            fold_assumptions(
                raw_assumptions_by_turn.setdefault(entry.turn_index, []),
                entry.args.get("assumptions"),
            )

    # `answer_sql` per turn — the query the model designated as the answer, so a
    # RELOADED transcript can page the same table the live turn showed (via
    # `POST /query/page`) instead of degrading to a static preview. Reconstructed
    # from the RAW trail for the same reason assumptions are: an `answerWithTable`
    # entry has determined-EMPTY provenance and survives `filter_trail`, but reading
    # the raw trail keeps this independent of that gate, and the value is surfaced
    # only when the turn's answer itself survives the scope filter (below).
    #
    # LAST designation wins, matching the loop. `blueprint_terminal_sql` is supplied
    # by the caller because resolving a `blueprint_id` needs a D46 KV de-reference,
    # which this pure projection cannot do — see `GET /session/history` in `app.py`.
    #
    # 08: the whole SET, not just the primary. `answer_sql` stays and is the SAME
    # projection the loop's envelope applies (`answer_tables[0].sql`), computed here
    # from the reconstructed list rather than resolved a second way — so a reloaded
    # transcript and the live turn cannot disagree about which table is the lead.
    runs: Mapping[str, BlueprintRun] = blueprint_runs or {
        # Back-compat for the `blueprint_terminal_sql` callers (tests and any
        # caller that has only the SQL): a run with no verification and no slots,
        # which renders as a table with no badge and no chip rather than a wrong one.
        bp_id: BlueprintRun(terminal_sql=sql)
        for bp_id, sql in (blueprint_terminal_sql or {}).items()
    }
    terminal_by_id = terminal_sql_by_id(runs)
    answer_tables_by_turn: dict[int, list[AnswerTable]] = {}
    scope_dropped_by_turn: dict[int, int] = {}
    for entry in trail:
        if entry.status != "ok" or entry.tool_name != ANSWER_TABLE_TOOL_NAME:
            continue
        finalized = finalize_designations(resolve_designations(entry.args, terminal_by_id).items)
        if not finalized.tables:
            continue
        persisted = entry.answer_table_provenance
        kept: list[AnswerTable] = []
        dropped = 0
        for index, table in enumerate(finalized.tables):
            # POSITIONALLY PARALLEL to what was persisted. A length mismatch means
            # the reconstruction no longer lines up with what was captured (an
            # expired blueprint `result_full` is enough), so the positional read is
            # abandoned rather than mis-attributed — `None` then means undetermined,
            # and `is_answer_table_in_scope` says what that costs.
            provenance = (
                persisted[index]
                if persisted is not None and len(persisted) == len(finalized.tables)
                else None
            )
            enriched = enrich_table(table, runs, provenance=provenance)
            if is_answer_table_in_scope(enriched.provenance, column_scope):
                kept.append(enriched)
            else:
                dropped += 1
        # LAST designation wins, matching the loop, and over the whole set.
        answer_tables_by_turn[entry.turn_index] = kept
        scope_dropped_by_turn[entry.turn_index] = dropped
    for dropped in scope_dropped_by_turn.values():
        if dropped:
            observer("history_answer_table_scope_dropped", {"table_count": dropped})

    turns: list[dict[str, Any]] = []
    for turn_index in sorted(turn_order):
        assistant = answers.get(turn_index)
        # Tie assumptions to answer survival (see the scope-posture comment above):
        # a surfaced answer carries its assumptions; a withheld answer withholds
        # them too. `[]` (turn recorded none) collapses to `None` — the `sql` fork.
        assumptions = (
            (raw_assumptions_by_turn.get(turn_index) or None) if assistant is not None else None
        )
        if assistant is not None and assistant.ship_disposition is not None:
            assumptions = (assumptions or [])[: assistant.retained_assumption_count or 0] or None
            if assistant.ship_disposition != "ship_tables_with_hedge":
                answer_tables_by_turn.pop(turn_index, None)
        turns.append(
            {
                "turn_index": turn_index,
                "question": questions[turn_index],
                "answer": assistant.content if assistant is not None else None,
                "provenance_union": (
                    _project_provenance(assistant.provenance) if assistant is not None else None
                ),
                "assumptions": assumptions,
                # Withheld with the answer, exactly like `assumptions`: if the answer
                # did not survive the scope filter, neither does the query behind it.
                # §D.3, stated so a later reader does not mistake it for a bug: the
                # answer-survival gate is TURN-WIDE and deliberately so, because the
                # turn's provenance union already contains every table's columns. A
                # narrowing that excludes one table's columns drops the assistant
                # message, and all N tables go with it. Per-table provenance closes
                # the OTHER half — a designated `sql=` that was never executed, whose
                # columns appear in no trail entry's provenance at all.
                "answer_sql": (
                    (answer_tables_by_turn.get(turn_index) or [None])[0].sql
                    if assistant is not None and answer_tables_by_turn.get(turn_index)
                    else None
                ),
                "answer_tables": (
                    [table.to_doc() for table in answer_tables_by_turn[turn_index]]
                    if assistant is not None and answer_tables_by_turn.get(turn_index)
                    else None
                ),
                "tool_calls": [
                    _project_tool_call(entry) for entry in tools_by_turn.get(turn_index, [])
                ],
            }
        )

    # Top-level nice-to-have (§1.4): mirror an UNCONSUMED pause checkpoint's
    # pending question so a reloaded page can re-open the ask-user prompt and
    # resume. Model-authored text (not warehouse-derived) → not scope-gated.
    pending_question = (
        pause_checkpoint.pending_question
        if pause_checkpoint is not None and not pause_checkpoint.consumed
        else None
    )

    return {"turns": turns, "pending_question": pending_question}


__all__ = ["project_history"]
