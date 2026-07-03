"""SessionSummary — the deterministic, normalized view the triage gate and the
S3 extractor consume (learning-loop-slice2-design §1, D27).

Entity-bearing but strictly in-boundary: `user_nl`, `assistant_text`, and `args`
(SQL literals) may carry PII. This is fine — the summary is produced INSIDE the
learning process, pre-leakage-gate (S5), and NEVER leaves it except as (a) an
access-controlled `learning_audit` snapshot (S3) or (b) an entity-FREE derivative
after the S5 gate. S2 itself emits neither: it hands the in-memory summary to the
stub extractor and drops it.

`tool_call_ref` (= `TrailEntry.tool_call_id`) and `turn_index` are preserved so
S3's `EvidenceRef{turn_ref, tool_call_ref}` resolves against the live session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# S2 and the S3 extractor share ONE acceptance type. The S2 loader's output range
# is {no_correction, explicit_confirm, None} — `thumbs_up` is declared (S3's
# header uses the same enum) but NEVER emitted in Phase 1 (no rating capture
# surface exists yet, D99).
AcceptedSignal = Literal["no_correction", "thumbs_up", "explicit_confirm"]


@dataclass(frozen=True)
class ToolCallSummary:
    turn_index: int  # → EvidenceRef.turn_ref
    tool_call_ref: str  # = TrailEntry.tool_call_id → EvidenceRef.tool_call_ref
    tool_name: str
    args: dict[str, Any]  # verbatim (SQL lives at args["sql"] for runQuery, etc.)
    sql: str | None  # convenience: extracted SQL for runQuery/explainQuery, else None
    status: str  # "ok" | "denied" | "error"
    error_code: str | None
    provenance: frozenset[tuple[str, str]] | None  # carried VERBATIM (may be None=undetermined)
    result_columns: tuple[str, ...]  # SHAPE only — from full result if loaded, else preview
    result_row_count: int | None
    result_full_ref: str | None  # the D46 pointer recorded on the TrailEntry
    full_result_loaded: bool  # True iff the D46 full result was fetched (§1.4)


@dataclass(frozen=True)
class TurnSummary:
    turn_index: int
    user_nl: str | None  # role=="user" content for this turn (None if none)
    assistant_text: str | None  # role=="assistant" answer for this turn (None if none)
    tool_call_refs: tuple[str, ...]  # tool_call_ids produced in this turn, in order


@dataclass(frozen=True)
class BlueprintUsage:
    tool_call_ref: str  # the runBlueprint call
    blueprint_id: str | None  # from args
    status: str  # "ok" | "denied" | "error"
    outcome: Literal["accepted", "corrected"]  # inferred (§2.5)


@dataclass(frozen=True)
class AskUserExchange:
    question_tool_call_ref: str  # the askUser call
    question: str  # the question text (from args)
    answer: str | None  # the NEXT user message content (None if unanswered)
    answer_turn_index: int | None


@dataclass(frozen=True)
class FailedFixedSql:
    failed_tool_call_ref: str  # a runQuery/runBlueprint with status in {"error","denied"}
    failed_sql: str | None
    fixed_tool_call_ref: str  # the SUBSEQUENT status=="ok" runQuery
    fixed_sql: str | None


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    user_id: str  # carried from the LearningJob reference
    scope_ref: str  # scope id/hash (D25/D30) — NEVER raw scope
    trace_id: str
    content_hash: str  # carried for provenance / idempotency continuity
    turns: tuple[TurnSummary, ...]
    tool_calls: tuple[ToolCallSummary, ...]
    blueprint_usages: tuple[BlueprintUsage, ...]
    askuser_exchanges: tuple[AskUserExchange, ...]
    failed_fixed_sql: tuple[FailedFixedSql, ...]
    accepted_signal: AcceptedSignal | None  # None ⇒ no acceptance detected (§2)
