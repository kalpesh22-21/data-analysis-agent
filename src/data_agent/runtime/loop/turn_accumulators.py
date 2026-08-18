"""The turn window's ANSWER ACCUMULATORS — every fact `_run_loop_body` gathers
from its tool calls and reads back out at a `TurnOutcome` return, and the folds
that gather them, in one window-scoped object.

WHAT THIS REPLACES. Seven parallel locals declared together at the top of
`_run_loop_body` (`turn_sql`, `answer_tables`, `blueprint_runs`, `blueprint_use`,
`verification`, `result_sql_by_call_id`, `turn_assumptions`) plus SIX `seed_*`
parameters that had to be declared on `_run_loop`, re-declared on `_run_loop_body`
and forwarded between them — thirteen names describing one thing: what this turn
window knows about its answer so far.

THE COUNT WAS THE DEFECT, not the style. `seed_blueprint_terminal_sql` (the
predecessor of `blueprint_runs`) was accepted by `_run_loop` and then simply not
forwarded to `_run_loop_body`, so the blueprint-approval resume's carefully-built
run map was discarded on every resume and a blueprint that completed before the
pause was never designatable after it. Nothing failed: the other five seeds
arrived, every other accumulator behaved, and the turn merely lost its table. With
ONE object there is no per-accumulator forwarding to get wrong — a hole can only
drop the whole window's state, which no test survives. The `_run_loop` wrapper
itself was inlined away in T5.6; `_run_loop_body`'s docstring points back here
for this incident.

WHAT STAYED IN THE LOOP, and why, following `read_guard.py` /
`blueprint_gate.py` / `finalization.py`: this owns the STATE and the FOLDS; the
loop keeps the EFFECTS and the ORDERING. So the observer events, the trail writes,
the `tool_result` rewrites and the POSITION of every fold relative to
`append_trail_entry` (a refused call is never folded, because the rewrite that
refused it already made it non-`ok` by the time these methods see it) stay where
they were and remain the loop's business.

Three neighbours deliberately did NOT move, because each needs something this
module has no business holding:

  - `AgentLoop._resolve_answer_tables` — the answer-table hooks, the dispatcher's
    provenance capture and the observer. It is fed `blueprint_runs` from here and
    its output is handed back to `note_answer_tables`.
  - `AgentLoop._observe_uncovered_intents` — the observer. It is fed
    `result_sql_by_call_id` from here.
  - `AgentLoop._compute_turn_answer_tables` / `_compute_turn_assumptions` — the
    session store. They REBUILD these accumulators from the persisted trail, which
    is how a resume seeds one of these objects in the first place.

FOUR IMPORTS, each unavoidable and each one-directional (nothing here imports
`agent_loop`, and `composite.answer_with_table`'s own edge back to it is
`TYPE_CHECKING`-only, so no cycle exists):

  - the `composite.answer_with_table` value types and constructors
    (`AnswerTable`, `BlueprintRun`, `blueprint_run_from_result`,
    `blueprint_verification`, `rollup_verification`) — these accumulators ARE
    those records, and re-deriving any of them here is the divergence those
    shared constructors exist to prevent;
  - `TOOL_NAME as ANSWER_TABLE_TOOL_NAME` — two folds key on it; re-spelling the
    literal is the same divergence;
  - `fold_assumptions` — the SAME fold `session_history` applies to the same
    arguments, so the live turn and the history read-surface cannot disagree;
  - `ToolResult` — every fold reads one, and reads `status`/`result_full` off it.

So this is NOT the stdlib leaf `read_guard.py` is, and it does not need to be —
nothing outside the loop package imports it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from data_agent.runtime.composite.answer_with_table import TOOL_NAME as ANSWER_TABLE_TOOL_NAME
from data_agent.runtime.composite.answer_with_table import (
    AnswerTable,
    BlueprintRun,
    blueprint_run_from_result,
    blueprint_verification,
    rollup_verification,
)
from data_agent.runtime.composite.record_assumptions import fold_assumptions
from data_agent.runtime.dispatch.tool_dispatcher import ToolResult

__all__ = [
    "AnswerEnvelope",
    "TurnAccumulators",
    "accumulate_enrichment",
    "answer_envelope",
    "capture_terminal_sql",
]


@dataclass(frozen=True)
class AnswerEnvelope:
    """The four answer-table fields of a `TurnOutcome`, computed together."""

    answer_sql: str | None
    blueprint_use: dict[str, Any] | None
    verification: dict[str, Any] | None
    answer_tables: list[dict[str, Any]] | None


def answer_envelope(
    tables: Sequence[AnswerTable],
    *,
    blueprint_use: dict[str, Any] | None,
    verification: dict[str, Any] | None,
) -> AnswerEnvelope:
    """THE ONE PLACE the answer envelope is computed (08 §E).

    Going additive rather than breaking has one real cost — two fields that can
    disagree — and deriving one from the other is what pays it. `answer_sql` and
    `blueprint_use` are projections of `answer_tables[0]`; they are never
    accumulated independently.

    THE PRIMARY IS THE FIRST ITEM, NOT THE LAST. Within one call the first item is
    the model's lead table. (Between calls, last-wins still applies to the whole
    SET — a second `answerWithTable` replaces the list rather than appending to it.)

    `verification` is the conservative AND roll-up over the designated tables
    (`rollup_verification`), which is what stops a verified blueprint for part 1
    badging a hand-written grid for part 2.

    *blueprint_use* / *verification* are the turn-level accumulators and are used
    ONLY when the turn designated NO table at all. That case has no grid to
    over-claim on: the fields then mean what they have always meant — this turn's
    prose answer came from a verified blueprint — which is the enrichment the
    approval-resume seed exists to carry across a pause. As soon as there IS a
    designated table the derived values win outright, including when they are
    `None`, which is the badge-loss this change deliberately lands.
    """
    if not tables:
        return AnswerEnvelope(
            answer_sql=None,
            blueprint_use=blueprint_use,
            verification=verification,
            answer_tables=None,
        )
    primary = tables[0]
    return AnswerEnvelope(
        answer_sql=primary.sql,
        blueprint_use=dict(primary.blueprint_use) if primary.blueprint_use else None,
        verification=rollup_verification(tables),
        answer_tables=[table.to_doc() for table in tables],
    )


def capture_terminal_sql(
    tool_name: str,
    tool_result: ToolResult,
    *,
    into: dict[str, BlueprintRun],
    arguments: Mapping[str, Any] | None = None,
) -> None:
    """Record a SUCCESSFUL blueprint's `terminal_sql`, its D56 verification and its
    slot bindings under its id, in place.

    The terminal SQL is the ONE query whose rows are that blueprint's answer
    (`blueprint/executor.py`), exposed explicitly rather than inferred as "the last
    element of `result_full["sql"]`" — rehydrated nodes are appended to that list
    FIRST on a D45 resume, so the positional assumption is not safe.

    Captured HERE, at dispatch, because `result_full` is in hand: a blueprint's
    result is persisted behind a D46 KV pointer (`result_full_ref`), so reading it
    back off the trail later would cost a store round-trip. A no-op for any
    non-`ok` / non-runBlueprint call, so it is safe to call unconditionally.

    ALL THREE VALUES COME FROM THIS ONE SITE (08 §C.3), which is the whole reason
    the map holds a `BlueprintRun` rather than a bare SQL string. A separate
    `blueprint_id -> verification` map filled somewhere else would let a table's
    query and its green badge be paired from two DIFFERENT runs of the same
    blueprint; read out of one `result_full` at one moment, they cannot be.

    MODULE-LEVEL, NOT A METHOD OF `TurnAccumulators`, because it has a THIRD caller
    with no turn window to belong to: `_compute_turn_answer_tables` replays the
    persisted trail into a bare map, and `_resume_blueprint` fills the seed map it
    then hands to a window that does not exist yet. `capture_blueprint_run` below
    is the windowed caller and delegates here, so all three fill the map through
    one function.
    """
    if tool_result.status != "ok" or tool_name != "runBlueprint":
        return
    captured = blueprint_run_from_result(
        tool_result.result_full, slots=(arguments or {}).get("slot_bindings") or {}
    )
    if captured is not None:
        blueprint_id, run = captured
        into[blueprint_id] = run


def accumulate_enrichment(
    tool_name: str,
    arguments: dict[str, Any],
    tool_result: ToolResult,
    *,
    turn_sql: list[str],
    blueprint_use: dict[str, Any] | None,
    verification: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fold one SUCCESSFUL runQuery/runBlueprint result into the turn-window
    enrichment accumulators (UI Slice 1, contract §3.2). `turn_sql` is mutated
    in place (deduped, first-occurrence order); the other two are RETURNED
    for the caller to reassign. A no-op for any non-`ok` / non-query tool call,
    so it is safe to call unconditionally. Shared by the in-loop dispatch path
    AND the blueprint approval-resume seed path so both produce identical
    enrichment (Fix 1: a resumed verified answer keeps its badge + chip).

    MODULE-LEVEL for the reason `capture_terminal_sql` is: `_resume_blueprint` runs
    it to BUILD the seeds of a window that does not exist yet, so it cannot be a
    method of the object those seeds construct. `TurnAccumulators.note_enrichment`
    is the windowed caller and delegates here, which is what keeps the two paths
    from drifting.

    No longer tracks a `primary_preview`: the turn result used to carry the LAST
    successful query's `ResultPreview` as `result_table`, a fixed ~20-row window
    the user could not page past AND a choice the RUNTIME made. The answer table
    is now the model-designated `answer_sql` (`presentTable`), which the UI runs
    itself with real paging."""
    if tool_result.status != "ok":
        return blueprint_use, verification
    if tool_name == "runQuery":
        query_sql = arguments.get("sql")
        if query_sql and query_sql not in turn_sql:
            turn_sql.append(query_sql)
        return blueprint_use, verification
    if tool_name == "runBlueprint":
        rf = tool_result.result_full or {}
        for bp_sql in rf.get("sql", []):
            if bp_sql and bp_sql not in turn_sql:
                turn_sql.append(bp_sql)
        new_blueprint_use = {
            "blueprint_id": rf.get("blueprint_id"),
            "slots": dict(arguments.get("slot_bindings") or {}),
        }
        # The SAME constructor per-table designation uses, so the turn-level
        # badge and a table's badge can never describe one run differently.
        gate = blueprint_verification(rf)
        new_verification = gate if gate is not None else verification
        return new_blueprint_use, new_verification
    return blueprint_use, verification


class TurnAccumulators:
    """One turn WINDOW's answer accumulators: fresh per `_run_loop_body`, never
    persisted, seeded at construction on the paths that resume a turn already in
    progress (UI Slice 1, `docs/decisions/ui-slice1-enriched-result-contract.md`
    §3; 08 for the multi-table half).

    SEEDED AT CONSTRUCTION AND ONLY THERE. Four call sites build one: `run()`
    builds nothing (a brand-new turn knows nothing), `resume()` seeds the three
    facts it can rebuild from the trail, `resume()`'s budget-cap STOP branch seeds
    the two of those the stop OUTCOME reads (assumptions + answer tables, rebuilt
    from the same trail by the same two producers — M2, so a user who answered
    "stop" keeps the table and assumptions a user who answered "continue" keeps),
    and `_resume_blueprint` seeds all six —
    including the enrichment of the blueprint that completed *during* the resume,
    which no trail replay could produce because its entry is written moments later.

    EVERY SEED IS COPIED, including the two that are single objects rather than
    containers. `blueprint_use`/`verification` are only ever REBOUND today (the
    enrichment fold builds a new dict rather than mutating), so aliasing the
    caller's dict happens to be safe — "happens to be safe" via a property of a
    function two modules away is exactly the kind of invariant that quietly stops
    holding, and a copy costs one dict per resume. (The copy is TOP-LEVEL only:
    nested values — e.g. `blueprint_use["slots"]` — stay shared with the caller's
    seed, which is fine because both seed producers discard their dicts and the
    fold rebinds rather than mutates.)
    """

    def __init__(
        self,
        *,
        sql: Sequence[str] | None = None,
        answer_tables: Sequence[AnswerTable] | None = None,
        blueprint_runs: Mapping[str, BlueprintRun] | None = None,
        blueprint_use: Mapping[str, Any] | None = None,
        verification: Mapping[str, Any] | None = None,
        assumptions: Sequence[str] | None = None,
    ) -> None:
        # Every successful query this turn ran, deduped, first-occurrence order.
        self._sql: list[str] = list(sql) if sql else []
        # EVERY table the model has designated so far this turn (08). Last
        # `answerWithTable` wins over the WHOLE set, never appends.
        self._answer_tables: list[AnswerTable] = list(answer_tables or ())
        # `blueprint_id -> BlueprintRun` (terminal SQL + D56 verification + slots)
        # for every blueprint that ran SUCCESSFULLY this turn, captured at dispatch
        # (the result is in hand there, so this needs no D46 KV de-reference). It is
        # what lets `answerWithTable(blueprint_id=…)` resolve to a concrete pageable
        # query — with its own badge — without re-running the DAG. Seeded on the
        # approval-resume path so a blueprint that completed BEFORE the pause is
        # still designatable after it.
        self._blueprint_runs: dict[str, BlueprintRun] = dict(blueprint_runs or {})
        self._blueprint_use: dict[str, Any] | None = (
            dict(blueprint_use) if blueprint_use is not None else None
        )
        self._verification: dict[str, Any] | None = (
            dict(verification) if verification is not None else None
        )
        # `tool_call_id -> the query whose rows that call produced`, for the
        # intent-coverage CHECK (08 §B.1). Window-local and best-effort: it is
        # a telemetry signal that an intent's result went untabled, never a refusal,
        # so an evidence call from an earlier window simply does not contribute —
        # which is why it is the ONE accumulator with no seed parameter.
        self._result_sql_by_call_id: dict[str, str] = {}
        # recordAssumptions accumulator (mirrors `_sql`): the deduped,
        # first-occurrence list of plain-English assumptions the model recorded
        # this turn. Folded from each SUCCESSFUL recordAssumptions call's ARGUMENTS,
        # read at every `TurnOutcome(...)` return site.
        self._assumptions: list[str] = list(assumptions) if assumptions else []

    # --- folds (one per dispatched tool call, all no-ops off their own tool) ---

    def note_enrichment(
        self, tool_name: str, arguments: dict[str, Any], tool_result: ToolResult
    ) -> None:
        """Fold one SUCCESSFUL runQuery/runBlueprint result into the SQL list and
        the turn-level `blueprint_use`/`verification` (UI Slice 1 §3.2).

        The tuple-rebind that made this awkward as a free function — two values
        returned for the caller to reassign beside a third mutated in place — is
        the whole reason it is a method now: all three are this object's fields, so
        the caller has nothing to reassign and cannot reassign one and forget
        another."""
        self._blueprint_use, self._verification = accumulate_enrichment(
            tool_name,
            arguments,
            tool_result,
            turn_sql=self._sql,
            blueprint_use=self._blueprint_use,
            verification=self._verification,
        )

    def capture_blueprint_run(
        self,
        tool_name: str,
        tool_result: ToolResult,
        arguments: Mapping[str, Any] | None = None,
    ) -> None:
        """Record a SUCCESSFUL `runBlueprint`'s terminal SQL + verification + slots
        under its id (08 §C.3), so `answerWithTable(blueprint_id=…)` can resolve it
        this turn. Delegates to `capture_terminal_sql` — see it for why that
        function is module-level."""
        capture_terminal_sql(tool_name, tool_result, into=self._blueprint_runs, arguments=arguments)

    def note_result_sql(
        self,
        tool_name: str,
        tool_call_id: str,
        arguments: Any,
        tool_result: ToolResult,
    ) -> None:
        """Record which query's rows this call produced, keyed by the id an intent
        cites as its evidence — the intent-coverage CHECK's raw material (08 §B.1).

        NOT a source for the tables themselves: deriving those from the evidence
        call would page the agent's own LIMIT-ed reading query and silently
        truncate every grid.

        The two branches read DIFFERENT places for the same fact and neither can
        serve the other. A `runQuery`'s query is the argument the model sent; a
        `runBlueprint`'s is the terminal node's, which only `result_full` knows."""
        if tool_result.status != "ok":
            return
        if tool_name == "runQuery" and isinstance(arguments, dict):
            produced = arguments.get("sql")
            if isinstance(produced, str) and produced:
                self._result_sql_by_call_id[tool_call_id] = produced
        elif tool_name == "runBlueprint":
            captured = blueprint_run_from_result(tool_result.result_full)
            if captured is not None:
                self._result_sql_by_call_id[tool_call_id] = captured[1].terminal_sql

    def note_assumptions(
        self, tool_name: str, arguments: dict[str, Any], tool_result: ToolResult
    ) -> None:
        """Fold one SUCCESSFUL `recordAssumptions` call into the assumptions list
        (mirrors `note_enrichment`'s SQL discipline: deduped, first-occurrence
        order). Read from the call ARGUMENTS via the SAME `fold_assumptions` helper
        `session_history` uses, so the loop and the history read-surface agree
        exactly. A no-op for any non-`ok` / non-`recordAssumptions` call, so it is
        safe to call unconditionally."""
        if tool_result.status != "ok" or tool_name != "recordAssumptions":
            return
        fold_assumptions(self._assumptions, arguments.get("assumptions"))

    def note_answer_tables(
        self, tool_name: str, tool_result: ToolResult, resolved: Sequence[AnswerTable]
    ) -> None:
        """Fold one SUCCESSFUL `answerWithTable` call into the turn's answer tables
        (mirrors `note_assumptions`: read from the call ARGUMENTS, never from the
        result). A no-op for any non-`ok` / non-`answerWithTable` call, so it is
        safe to call unconditionally.

        *resolved* is the output of `AgentLoop._resolve_answer_tables`, computed
        ONCE by the caller and passed in — resolving here as well would fire the
        `hooks/answer_table.py` seams TWICE per designation, which a registered hook
        would see as two events for one model decision.

        LAST designation wins, and it wins over the WHOLE SET. A second
        `answerWithTable` means the model changed its mind about which query is the
        answer — the later choice is the current one; that recorded rationale is
        exactly as true of a set as of a string, and appending instead would make
        "changed its mind" unexpressible. (`recordAssumptions` accumulates because
        assumptions are additive; an answer is one answer.) A designation that
        resolves to nothing (blank args, or a refused call) leaves the previous set
        intact rather than clearing it, so a malformed retry cannot silently drop a
        good table."""
        if tool_result.status != "ok" or tool_name != ANSWER_TABLE_TOOL_NAME:
            return
        if resolved:
            self._answer_tables = list(resolved)

    # --- reads: the turn's exits ---------------------------------------------

    @property
    def sql_executed(self) -> list[str] | None:
        """`TurnOutcome.sql_executed`. `[]` (no successful query this turn) ->
        `None`, so the UI treats "no SQL panel" and "empty SQL" identically
        (contract §1 fork 1)."""
        return self._sql or None

    @property
    def assumptions(self) -> list[str] | None:
        """`TurnOutcome.assumptions`. The same `[] -> None` fork as
        `sql_executed`: the UI treats "no assumptions" and "empty" identically."""
        return self._assumptions or None

    def envelope(self) -> AnswerEnvelope:
        """The four answer-table fields of a `TurnOutcome`, computed together —
        `answer_envelope` over this window's designated tables, with the
        turn-level enrichment as the no-table fallback. See that function for
        which of the two sources wins and when."""
        return answer_envelope(
            self._answer_tables,
            blueprint_use=self._blueprint_use,
            verification=self._verification,
        )

    # --- reads: what other objects need --------------------------------------

    @property
    def blueprint_runs(self) -> Mapping[str, BlueprintRun]:
        """Read-only view for `AgentLoop._resolve_answer_tables`, which turns a
        designated `blueprint_id` into that run's concrete pageable SQL. Every
        successful `runBlueprint` of the batch is captured before the resolver runs,
        because the fold and the resolve happen in the same drain of the same
        batch, in that order."""
        return self._blueprint_runs

    @property
    def result_sql_by_call_id(self) -> Mapping[str, str]:
        """Read-only view for `AgentLoop._observe_uncovered_intents`."""
        return self._result_sql_by_call_id

    @property
    def has_answer_tables(self) -> bool:
        """Whether the turn is holding any designated table at all — the
        `AnswerShapeCounter` seed (a resumed window inherits "the user already has
        a grid") and, live, half of the test that disarms the shape gate.

        Deliberately the ACCUMULATOR, not one call's resolution: a later call that
        designates nothing leaves an earlier good set intact (see
        `note_answer_tables`), and reading only that call would re-arm the gate on
        a malformed retry and refuse a turn that has its table."""
        return bool(self._answer_tables)
