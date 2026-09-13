"""The turn window's ANSWER ACCUMULATORS — every fact `_run_loop_body` gathers from its
tool calls and reads back out at a `TurnOutcome` return, and the folds that gather them,
in one window-scoped object.

THE COUNT WAS THE DEFECT that motivated the object: seven parallel locals plus six
`seed_*` parameters forwarded by hand, one of which was silently not forwarded — so a
resumed blueprint's run map was discarded and the turn merely lost its table, with nothing
failing. With ONE object there is no per-accumulator forwarding to get wrong.

This owns the STATE and the FOLDS; the loop keeps the EFFECTS and the ORDERING — the
observer events, the trail writes, the `tool_result` rewrites, and the POSITION of every
fold relative to `append_trail_entry` (a refused call is never folded, because the rewrite
that refused it already made it non-`ok` before these methods see it).

It imports the `composite.answer_with_table` value types and constructors, its
`TOOL_NAME`, `fold_assumptions` (the SAME fold `session_history` applies, so the live turn
and the history read-surface cannot disagree) and `ToolResult`. Re-deriving any of them
here is the divergence those shared definitions exist to prevent. So this is NOT the
stdlib leaf `read_guard.py` is — nothing outside the loop package imports it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from data_agent.runtime.capabilities.digest import (
    filter_labels_digest,
    metadata_data_digest,
    widget_label,
)
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
from data_agent.runtime.sanitize import sanitize_text

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
    """THE ONE PLACE the answer envelope is computed.

    `answer_sql` and `blueprint_use` are PROJECTIONS of `answer_tables[0]`, never
    accumulated independently — that is what keeps two additive fields from disagreeing.

    THE PRIMARY IS THE FIRST ITEM, NOT THE LAST: within one call the first item is the
    model's lead table. (Between calls, last-wins still applies to the whole SET — a
    second `answerWithTable` replaces the list rather than appending to it.)

    `verification` is the conservative AND roll-up over the designated tables
    (`rollup_verification`), which is what stops a verified blueprint for part 1 badging a
    hand-written grid for part 2.

    *blueprint_use*/*verification* are the turn-level accumulators, used ONLY when the
    turn designated NO table at all — that case has no grid to over-claim on. As soon as
    there IS a designated table the derived values win outright, including when they are
    `None`.
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
    """Record a SUCCESSFUL blueprint's `terminal_sql`, its D56 verification and its slot
    bindings under its id, in place.

    The terminal SQL is the ONE query whose rows are that blueprint's answer, exposed
    explicitly rather than inferred as "the last element of the result's sql list" —
    rehydrated nodes are appended to that list FIRST on a D45 resume, so the positional
    assumption is not safe.

    Captured HERE, at dispatch, because `result_full` is in hand: a blueprint's result is
    persisted behind a D46 KV pointer, so reading it back off the trail would cost a store
    round-trip. A no-op for any non-`ok` / non-runBlueprint call.

    ALL THREE VALUES COME FROM THIS ONE SITE, which is the whole reason the map holds a
    `BlueprintRun` rather than a bare SQL string: a separate `blueprint_id ->
    verification` map filled somewhere else would let a table's query and its green badge
    be paired from two DIFFERENT runs of the same blueprint.

    MODULE-LEVEL, not a method, because it has a THIRD caller with no turn window to
    belong to: `_compute_turn_answer_tables` replays the persisted trail into a bare map,
    and `_resume_blueprint` fills the seed map for a window that does not exist yet.
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
    """Fold one SUCCESSFUL runQuery/runBlueprint result into the turn-window enrichment
    accumulators. `turn_sql` is mutated in place (deduped, first-occurrence order); the
    other two are RETURNED for the caller to reassign. A no-op for any non-`ok` /
    non-query call, so it is safe to call unconditionally. Shared by the in-loop dispatch
    path AND the blueprint approval-resume seed path, so a resumed verified answer keeps
    its badge and chip.

    MODULE-LEVEL for the reason `capture_terminal_sql` is: `_resume_blueprint` runs it to
    BUILD the seeds of a window that does not exist yet, so it cannot be a method of the
    object those seeds construct. `TurnAccumulators.note_enrichment` is the windowed
    caller and delegates here, which is what keeps the two paths from drifting.
    """
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
    """One turn WINDOW's answer accumulators: fresh per `_run_loop_body`, never persisted,
    seeded at construction on the paths that resume a turn already in progress.

    SEEDED AT CONSTRUCTION AND ONLY THERE. `run()` seeds nothing (a brand-new turn knows
    nothing); `resume()` seeds the three facts it can rebuild from the trail; its
    budget-cap STOP branch seeds the two the stop outcome reads, so a user who answered
    "stop" keeps the table and assumptions a user who answered "continue" keeps; and
    `_resume_blueprint` seeds all six, including the enrichment of the blueprint that
    completed DURING the resume, which no trail replay could produce.

    EVERY SEED IS COPIED, including the two that are single objects rather than
    containers. They are only ever REBOUND today, so aliasing the caller's dict happens to
    be safe — and "happens to be safe" via a property of a function two modules away is
    exactly the invariant that quietly stops holding. The copy is TOP-LEVEL only: nested
    values stay shared with the caller's seed, which is fine because both seed producers
    discard their dicts and the fold rebinds rather than mutates.
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
        capability_cards: Sequence[Mapping[str, Any]] | None = None,
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
        self._capability_cards: list[dict[str, Any]] = []
        self._loaded_capability_names: set[str] = set()
        self._capability_evidence: dict[str, dict[str, Any]] = {}
        for card in capability_cards or ():
            self._remember_capability(card, "")

    # --- folds (one per dispatched tool call, all no-ops off their own tool) ---

    def note_enrichment(
        self, tool_name: str, arguments: dict[str, Any], tool_result: ToolResult
    ) -> None:
        """Fold one SUCCESSFUL runQuery/runBlueprint result into the SQL list and the
        turn-level `blueprint_use`/`verification`.

        All three are this object's fields, so the caller has nothing to reassign and
        cannot reassign one and forget another — the whole reason this is a method rather
        than the free function it delegates to.
        """
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
        """Record a SUCCESSFUL `runBlueprint`'s terminal SQL + verification + slots under its
        id, so `answerWithTable(blueprint_id=…)` can resolve it this turn. Delegates to
        the module-level `capture_terminal_sql` — see it for why that function is not a
        method.
        """
        capture_terminal_sql(tool_name, tool_result, into=self._blueprint_runs, arguments=arguments)

    def note_result_sql(
        self,
        tool_name: str,
        tool_call_id: str,
        arguments: Any,
        tool_result: ToolResult,
    ) -> None:
        """Record which query's rows this call produced, keyed by the id an intent cites as
        its evidence — the intent-coverage CHECK's raw material.

        NOT a source for the tables themselves: deriving those from the evidence call
        would page the agent's own LIMIT-ed reading query and silently truncate every grid.

        The two branches read DIFFERENT places for the same fact and neither can serve the
        other. A `runQuery`'s query is the argument the model sent; a `runBlueprint`'s is
        the terminal node's, which only `result_full` knows.
        """
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
        """Fold one SUCCESSFUL `recordAssumptions` call into the assumptions list (deduped,
        first-occurrence order, mirroring `note_enrichment`'s SQL discipline). Read from
        the call ARGUMENTS via the SAME `fold_assumptions` helper `session_history` uses,
        so the loop and the history read-surface agree exactly. A no-op for any other call.
        """
        if tool_result.status != "ok" or tool_name != "recordAssumptions":
            return
        fold_assumptions(self._assumptions, arguments.get("assumptions"))

    def note_answer_tables(
        self, tool_name: str, tool_result: ToolResult, resolved: Sequence[AnswerTable]
    ) -> None:
        """Fold one SUCCESSFUL `answerWithTable` call into the turn's answer tables — read
        from the call ARGUMENTS, never from the result. A no-op for any other call.

        *resolved* is the output of `AgentLoop._resolve_answer_tables`, computed ONCE by
        the caller and passed in: resolving here as well would fire the
        `hooks/answer_table.py` seams TWICE per designation, which a registered hook would
        see as two events for one model decision.

        LAST designation wins, and it wins over the WHOLE SET. A second `answerWithTable`
        means the model changed its mind about which query is the answer, and appending
        instead would make "changed its mind" unexpressible. (`recordAssumptions`
        accumulates because assumptions are additive; an answer is one answer.) A
        designation that resolves to nothing leaves the previous set intact, so a
        malformed retry cannot silently drop a good table.
        """
        if tool_result.status != "ok" or tool_name != ANSWER_TABLE_TOOL_NAME:
            return
        if resolved:
            self._answer_tables = list(resolved)

    def note_capability_card(self, tool_result: ToolResult) -> str | None:
        if not (tool_result.status == "ok" and tool_result.terminal):
            return None
        payload = tool_result.result_full
        if not isinstance(payload, dict):
            return None
        self._remember_capability(payload, tool_result.tool_name)
        answer = payload.get("answer")
        return answer.strip() if isinstance(answer, str) and answer.strip() else None

    def _remember_capability(self, payload: Mapping[str, Any], tool_name: str) -> None:
        card = {
            key: value for key, value in payload.items() if key not in {"answer", "_agent_evidence"}
        }
        if card not in self._capability_cards:
            self._capability_cards.append(card)
        evidence = payload.get("_agent_evidence")
        name = card.get("name", tool_name)
        if isinstance(name, str) and isinstance(evidence, dict):
            self._capability_evidence[name] = evidence

    def note_loaded_capability(self, tool_name: str, tool_result: ToolResult) -> None:
        if tool_name != "getCapabilityTool" or tool_result.status != "ok":
            return
        payload = tool_result.result_full
        if not isinstance(payload, Mapping) or payload.get("ready") is not True:
            return
        name = payload.get("tool_name")
        if isinstance(name, str) and name:
            self._loaded_capability_names.add(name)

    @property
    def unpresented_capability_names(self) -> tuple[str, ...]:
        presented = {
            card.get("name") for card in self._capability_cards if isinstance(card.get("name"), str)
        }
        return tuple(sorted(self._loaded_capability_names - presented))

    # --- reads: the turn's exits ---------------------------------------------

    @property
    def sql_executed(self) -> list[str] | None:
        """`TurnOutcome.sql_executed`. `[]` (no successful query this turn) -> `None`, so the
        UI treats "no SQL panel" and "empty SQL" identically.
        """
        return self._sql or None

    @property
    def assumptions(self) -> list[str] | None:
        """`TurnOutcome.assumptions`. The same `[] -> None` fork as `sql_executed`."""
        return self._assumptions or None

    @property
    def capability_cards(self) -> list[dict[str, Any]] | None:
        return self._capability_cards or None

    @property
    def capability_judge_context(self) -> tuple[dict[str, Any], ...]:
        contexts = []
        for card in self._capability_cards:
            name = card.get("name", card.get("tool_name", ""))
            evidence = self._capability_evidence.get(name, {}) if isinstance(name, str) else {}
            kind = evidence.get("kind", card.get("kind"))
            kind = kind if kind in ("navigation", "data_widget") else "unknown"
            metadata = card.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            parameters = evidence.get("parameters")
            parameters = parameters if isinstance(parameters, list) else []
            contexts.append(
                {
                    "name": sanitize_text(name, 120) if isinstance(name, str) else "",
                    "kind": kind,
                    "data": list(metadata_data_digest(kind, metadata)),
                    "presentation": widget_label(
                        kind,
                        preamble_url=metadata.get("preamble_url"),
                        widget_name=metadata.get("widgetName"),
                    ),
                    "filters": list(filter_labels_digest(metadata)),
                    "parameter_names": [
                        sanitize_text(p["name"], 120)
                        for p in parameters[:8]
                        if isinstance(p, dict) and isinstance(p.get("name"), str)
                    ],
                }
            )
        return tuple(contexts)

    def clear_capabilities(self) -> None:
        self._capability_cards.clear()
        self._capability_evidence.clear()
        self._loaded_capability_names.clear()

    def apply_ship_disposition(self, disposition: str, assumptions: tuple[str, ...]) -> None:
        self._assumptions = [item for item in self._assumptions if item in assumptions]
        if disposition != "ship_cards_with_hedge":
            self.clear_capabilities()
        if disposition != "ship_tables_with_hedge":
            self._sql.clear()
            self._answer_tables.clear()
            self._blueprint_use = None
            self._verification = None

    def envelope(self) -> AnswerEnvelope:
        """The four answer-table fields of a `TurnOutcome` — `answer_envelope` over this
        window's designated tables, with the turn-level enrichment as the no-table
        fallback. See that function for which of the two sources wins and when.
        """
        return answer_envelope(
            self._answer_tables,
            blueprint_use=self._blueprint_use,
            verification=self._verification,
        )

    # --- reads: what other objects need --------------------------------------

    @property
    def blueprint_runs(self) -> Mapping[str, BlueprintRun]:
        """Read-only view for `AgentLoop._resolve_answer_tables`, which turns a designated
        `blueprint_id` into that run's concrete pageable SQL. Every successful
        `runBlueprint` of the batch is captured before the resolver runs, because the fold
        and the resolve happen in the same drain of the same batch, in that order.
        """
        return self._blueprint_runs

    @property
    def result_sql_by_call_id(self) -> Mapping[str, str]:
        """Read-only view for `AgentLoop._observe_uncovered_intents`."""
        return self._result_sql_by_call_id

    @property
    def has_answer_tables(self) -> bool:
        """Whether the turn is holding any designated table at all — the `AnswerShapeCounter`
        seed, and live, half of the test that disarms the shape gate.

        Deliberately the ACCUMULATOR, not one call's resolution: a later call that
        designates nothing leaves an earlier good set intact, and reading only that call
        would re-arm the gate on a malformed retry and refuse a turn that has its table.
        """
        return bool(self._answer_tables)
