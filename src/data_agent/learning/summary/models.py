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

# Tools whose calls are the model's OWN BOOKKEEPING: they execute nothing, carry no SQL,
# and their result is the model's prose or ledger echoed back at it. `updateAnalysisState`
# is the intent ledger — `sql: null`, and Release 1 models re-send the WHOLE intent list
# every round with the rejected attempts persisted alongside the accepted ones, so one
# turn contributes many near-identical entries. `recordAssumptions` echoes the model's
# own prose.
#
# They are NEVER dropped from `SessionSummary` itself — that stays a faithful projection
# of the trail, and `loader.py` is the module that must not lie about what happened. The
# set exists for the two stages that build a MODEL PROMPT out of a summary and are paying
# per token for it: the extractor's payload (`extractor.py::_build_messages`) and the
# coverage judge's pre-extraction brief (`judge/prompt.py::session_brief`). Both want the
# same thing — the SQL evidence and the outcome narrative — and this churn is neither.
#
# It lives HERE rather than in either consumer for the reason `loader.py::DATA_TOOLS`
# does: two stages classifying the same tool names from two private lists is a mirror
# that goes out of step silently. It is in `models.py`, not `loader.py`, because both
# consumers already import this module and neither should have to pull the loader's
# runtime dependencies in to learn a pair of tool names.
BOOKKEEPING_TOOLS = frozenset({"updateAnalysisState", "recordAssumptions"})


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
class AnswerSql:
    """One query the FINAL answer stood on — an `answerWithTable` designation (§2.6).

    NOT a `ToolCallSummary`: `answerWithTable` executes nothing, and the query it
    designates need never have been dispatched as a `runQuery` at all, so this is
    the only record of it. `tool_call_ref` is the `answerWithTable` call, which keeps
    it resolvable as an S3 `EvidenceRef` like every other ref in this module.
    """

    tool_call_ref: str  # the answerWithTable call that designated this query
    sql: str  # non-blank by construction (`loader._designations`)
    blueprint_id: str | None  # the blueprint the MODEL associated with it, if any


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
    # The final answer's SQL (§2.6), in trail order, deduped. The one DEFAULTED field
    # here, because it is purely ADDITIVE: `()` is the honest value for a session
    # whose answer designated no table, which is also every session recorded before
    # `answerWithTable` carried one — so no other construction site of this dataclass
    # has to claim anything about it.
    answer_sqls: tuple[AnswerSql, ...] = ()
