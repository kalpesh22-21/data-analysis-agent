"""Pure projection of a persisted `SessionDoc` into the `GET /session/history`
transcript (UI Slice 3, docs/decisions/ui-slice3-history-lineage-contract.md).

`project_history` is the pure, HTTP-free, I/O-free D44 *read-surface* target —
the sibling of `filter_trail`/`filter_messages` (`context/scope_filter.py`) that
the history endpoint composes with them. It:

  1. Runs the two D44 filters over this request's `column_scope` BEFORE any
     serialization (§2, load-bearing): `filter_messages` drops assistant answers
     whose provenance ⊄ scope (or is `None`); `filter_trail` (with
     `current_turn_index=None` — a read has no in-progress turn, every entry gets
     the strict check) drops out-of-scope / undetermined tool calls. User
     messages always survive (they carry no warehouse-derived data), so a turn is
     never wholly dropped as long as it recorded a question (§2.1).
  2. Joins the survivors INDEPENDENTLY by `turn_index` (§2.1, YELLOW-2): a turn's
     answer can be withheld while one of its tool calls survives, or vice versa —
     the two filters are never coupled.
  3. Projects each turn/tool-call using EXACTLY the Slice-1 encodings — provenance
     as `sorted("db.table.column")` (identical to `_outcome_to_dict`,
     `runtime/app.py`) and result tables via `ResultPreview.to_doc()`.

Inline-only (§0, YELLOW-1): a `runBlueprint` node's SQL lives behind a KV pointer
(`TrailEntry.result_full_ref`), not inline, so its `sql` is projected as `null`
here (its provenance + result table + `blueprint_id` in `args` still surface). No
KV de-reference happens on the read path.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from data_agent.runtime.composite.record_assumptions import fold_assumptions
from data_agent.runtime.context.scope_filter import filter_messages, filter_trail
from data_agent.runtime.session.models import PauseCheckpoint, TrailEntry, TurnMessage


def _project_provenance(
    provenance: frozenset[tuple[str, str]] | None,
) -> list[str] | None:
    """Project a `frozenset[(db.table, column)]` provenance set to a sorted list
    of `"db.table.column"` strings — IDENTICAL to `_outcome_to_dict`
    (`runtime/app.py`). `None` (undetermined) → `null`; `frozenset()`
    (determined-empty) → `[]`."""
    if provenance is None:
        return None
    return sorted(f"{db_table}.{column}" for db_table, column in provenance)


def _project_tool_call(entry: TrailEntry) -> dict[str, Any]:
    """Project one surviving `TrailEntry` to a `tool_calls[]` element (§1.1).

    `sql` is `args["sql"]` for `runQuery`; `null` for `runBlueprint` (§0
    YELLOW-1 — its node SQL is behind a KV pointer, not inline) and for any tool
    that carries no `sql` argument.
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
) -> dict[str, Any]:
    """Project the persisted `messages`/`tool_trail` into the §1.1 transcript
    shape under *column_scope*, applying the two D44 filters first.

    Returns `{"turns": [...], "pending_question": ...}` (ordered by `turn_index`
    ascending). The caller (`GET /session/history`) wraps this with `session_id`.
    Pure: no I/O, no store read, no KV de-reference.
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

    turns: list[dict[str, Any]] = []
    for turn_index in sorted(turn_order):
        assistant = answers.get(turn_index)
        # Tie assumptions to answer survival (see the scope-posture comment above):
        # a surfaced answer carries its assumptions; a withheld answer withholds
        # them too. `[]` (turn recorded none) collapses to `None` — the `sql` fork.
        assumptions = (
            (raw_assumptions_by_turn.get(turn_index) or None)
            if assistant is not None
            else None
        )
        turns.append(
            {
                "turn_index": turn_index,
                "question": questions[turn_index],
                "answer": assistant.content if assistant is not None else None,
                "provenance_union": (
                    _project_provenance(assistant.provenance) if assistant is not None else None
                ),
                "assumptions": assumptions,
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
