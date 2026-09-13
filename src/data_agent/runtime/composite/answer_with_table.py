"""answer_with_table.py — the `answerWithTable` runtime tool.

The model calls `answerWithTable(answer=..., tables=[{sql | blueprint_id, caption}])` when
its answer IS a table. The call is TERMINAL: it carries the final prose AND designates the
queries whose rows the user should see, so the turn ends there. A non-terminal designation
tool cost a whole extra round-trip in which the model learned nothing — it already had the
results in context. `answer` is REQUIRED, so a model calling this out of habit mid-turn
cannot terminate the turn with an empty answer.

The safe default is unchanged: a SCALAR answer still ends the old way, a turn with no tool
calls, so a model that never calls this tool cannot hang.

TWO WAYS TO DESIGNATE INSIDE EACH ENTRY, one wire contract:

  * `sql=` — a raw read-only SELECT, NOT required to be one the agent already ran: the
    executed query usually carries a LIMIT the agent chose for its own reading, and paging
    needs the un-capped shape. Scope is still enforced where the query runs (the MCP, under
    the caller's own JWT), so this can never widen access.
  * `blueprint_id=` — resolved to that blueprint's `terminal_sql`, captured when it RAN
    this turn. It MUST name a blueprint that ran successfully this turn: re-running it to
    find out would decouple the paged table from the D56 verification that gated the answer
    the user was given.

`tables` IS THE ONLY CARRIER. It exists because a single designation could name only ONE of
the several result sets a multi-intent turn produces, and the prompt-level merge rules
written to close that gap were unsatisfiable rather than badly worded. It is the ONLY
carrier because the live model cannot omit declared keys — a second carrier for the same
fact is a second thing to fill in wrong. The top-level `sql`/`blueprint_id` pair survives
only as a read-path fold (`resolve_designations`), for trail entries written before the
change and for a model working from a stale context; that fold has no expiry, because old
session documents are read forever.

An element of `tables` is EXACTLY the mapping `resolve_designation` already reads, so
multi-table adds no second resolution path.

The UI receives `answer_tables` (a list) plus `answer_sql` — a DERIVED projection of
`answer_tables[0]`, kept so every existing single-table consumer works untouched — and
pages EACH table through `POST /query/page`. The blueprint path is resolved server-side
precisely so the UI never re-executes a DAG.

KNOWN LIMIT: a scratch-backed composed blueprint's terminal SQL references a session-scoped
`scratch.*` table with a TTL, so paging works until that lapses and then returns an ordinary
query error.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import MAX_INTENTS
from data_agent.runtime.context.scope_filter import is_provenance_in_scope
from data_agent.runtime.dispatch.tool_dispatcher import ToolResult
from data_agent.runtime.dispatch.tool_envelope import RuntimeToolBase
from data_agent.runtime.sanitize import MAX_FIELD_CHARS, sanitize_text
from data_agent.runtime.session.models import ResultPreview

if TYPE_CHECKING:
    from data_agent.runtime.loop.agent_loop import TurnContext

TOOL_NAME = "answerWithTable"

# 08 §F. EQUAL to `MAX_INTENTS`, and IMPORTED rather than re-declared so the two
# can never drift. A cap BELOW `MAX_INTENTS` would re-create through the payload
# exactly the conflict this feature exists to dissolve: a 5-intent turn under a
# 4-table ceiling is told it cannot show a table for every part, and the only move
# left is to merge two results into one query — premise (1) of the unsatisfiable
# triple, returning by the back door. Tied to `MAX_INTENTS`, the ceiling can never
# be the thing that forces a merge.
MAX_ANSWER_TABLES = MAX_INTENTS

# Lenient safety caps (not a contract — DoS/absurdity guards). Over-long input is
# truncated rather than rejected, matching `record_assumptions.py`'s posture: the
# runtime does not second-guess model text, it only stops a pathological payload
# being stored verbatim.
_MAX_SQL_LEN = 20_000
_MAX_ANSWER_LEN = 20_000


def clean_answer_sql(raw: Any) -> str | None:
    """Normalize a model-supplied `sql` value into a single SQL string or `None`.

    Non-`str` -> `None`; stripped; empty -> `None`; truncated to `_MAX_SQL_LEN`. Does NOT
    parse, validate or rewrite the SQL — validation happens where the query actually runs
    (`runtime/query_page.py` + the MCP), so this helper can never be the thing that
    silently changes what the user sees.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text[:_MAX_SQL_LEN] if text else None


def clean_answer_text(raw: Any) -> str | None:
    """Normalize the model's final prose. Same rules as `clean_answer_sql`; kept a
    separate function so the two caps can diverge without a shared-helper edit."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text[:_MAX_ANSWER_LEN] if text else None


def clean_blueprint_id(raw: Any) -> str | None:
    """Normalize a model-supplied `blueprint_id`. Resolution against THIS turn's
    successful runs happens in the loop (`_resolve_answer_tables`), not here."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text[:200] if text else None


def resolve_designation(args: Any, terminal_by_id: Mapping[str, str]) -> str | None:
    """Resolve ONE designation mapping to a concrete query, or `None`.

    The single expression of the two-forms rule, shared by every caller so they cannot
    drift. `sql=` wins when both are given: it is the more specific instruction. Otherwise
    `blueprint_id` is looked up in *terminal_by_id*, the `blueprint_id -> terminal_sql` map
    of blueprints that ran successfully in that turn.

    Callers differ only in how they BUILD that map — in-window the loop reads `terminal_sql`
    straight off the dispatch result, while the resume and history paths de-reference each
    blueprint's `result_full` from the D46 KV store. The resolution itself is identical,
    which is the point: reading only `args["sql"]` looked complete and silently dropped
    every blueprint designation, the form the live model actually emits (`sql=""` beside
    `blueprint_id`).
    """
    if not isinstance(args, dict):
        return None
    raw_sql = clean_answer_sql(args.get("sql"))
    if raw_sql is not None:
        return raw_sql
    blueprint_id = clean_blueprint_id(args.get("blueprint_id"))
    return terminal_by_id.get(blueprint_id) if blueprint_id is not None else None


# ---------------------------------------------------------------------------
# Multi-table designation (08)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlueprintRun:
    """What one SUCCESSFUL `runBlueprint` of this turn leaves behind for the answer-table
    machinery: the query whose rows ARE its result, whether the D56 gate verified it, and
    the slots it was run with.

    ONE RECORD, CAPTURED AT ONE SITE (`loop/turn_accumulators.py::capture_terminal_sql`),
    because a `blueprint_id -> terminal_sql` map beside a separate `blueprint_id ->
    verification` map allows a table's SQL and its badge to be paired from DIFFERENT runs of
    the same blueprint later. Here they cannot be: both are read out of one `result_full`.
    """

    terminal_sql: str
    # `{"passed": True, "method": "blueprint_gate", "grain_checked": bool}` when the
    # D56 gate verified this run; the explicit `empty_result`/`status` block when it
    # came back with ZERO rows (J6 — nothing to verify); `None` when no blueprint
    # verified this table at all. See `blueprint_verification`.
    verification: dict[str, Any] | None = None
    slots: dict[str, Any] = field(default_factory=dict)


# J6: the human-readable state of an empty blueprint result's badge. A display
# string on the wire (the shape's only one) because the badge is read by more than
# the browser — the persisted trail, `GET /session/history` and the D56 review
# input all carry this dict, and "unverifiable" is not derivable from `passed:
# false` alone (a raw-loop table has no dict at all, which is a different thing).
VERIFICATION_EMPTY_STATUS = "empty — unverifiable"


def is_zero_row_count(row_count: Any) -> bool:
    """Is *row_count* a genuine integer zero?

    THE ONE PLACE the bool exclusion is written. `isinstance(True, int)` is True in Python,
    so a bare `row_count == 0` reads a poisoned or legacy `row_count: false` as an empty
    result and retracts a verification claim over a value that says nothing about the row
    count. Both call sites read a row count off an untrusted JSON payload, so the rule lives
    in one function rather than being spelled out twice and drifting once.
    """
    return isinstance(row_count, int) and not isinstance(row_count, bool) and row_count == 0


def _is_empty_blueprint_result(result_full: Mapping[str, Any]) -> bool:
    """Did this blueprint return ZERO rows?

    Read TWO ways on purpose: `verify.empty_result` is what the executor writes today, and
    `row_count == 0` is the underlying fact — which is what catches a `result_full`
    PERSISTED BEFORE that marker existed and rehydrated from the D46 KV on
    `GET /session/history`, where re-deriving is the difference between a reloaded
    transcript telling the truth and reproducing the exact over-claim this fixes.
    """
    if (result_full.get("verify") or {}).get("empty_result") is True:
        return True
    return is_zero_row_count(result_full.get("row_count"))


def blueprint_verification(result_full: Any) -> dict[str, Any] | None:
    """The D56 verification block for one `runBlueprint` `result_full`, or `None`.

    The SINGLE constructor of that dict — shared by the turn-level enrichment accumulator
    and by per-table designation, so the two can never describe the same run differently.
    `None` (not `{"passed": False}`) is the negative form for a table NOTHING verified:
    absence reads as NO CLAIM, which is what an unverified result is.

    THE ONE PLACE `passed: False` IS EMITTED is a blueprint whose result is EMPTY. The grain
    teeth are `row_count == distinct_grain_count`, so at zero rows they read `0 == 0` and
    pass for every blueprint alive, and a structurally-empty blueprint shipped a 0-row grid
    wearing a full verified badge. That claim is withdrawn and replaced with an EXPLICIT
    state (`empty_result: True` + `status: "empty — unverifiable"`) rather than with `None`,
    because `None` means "a hand-written query, no gate involved" and would lose the fact
    that a blueprint ran and came back with nothing. Only the VERIFICATION claim is
    retracted; the rows are still the authoritative answer for the intent.
    """
    if not isinstance(result_full, dict) or result_full.get("status") != "verified":
        return None
    if _is_empty_blueprint_result(result_full):
        return {
            "passed": False,
            "method": "blueprint_gate",
            # The teeth did not meaningfully run — see `executor._verify_block`.
            "grain_checked": False,
            "empty_result": True,
            "status": VERIFICATION_EMPTY_STATUS,
        }
    return {
        "passed": True,
        "method": "blueprint_gate",
        # None-safe: a `verify: None` must not AttributeError.
        "grain_checked": bool((result_full.get("verify") or {}).get("grain_checked")),
    }


def blueprint_run_from_result(
    result_full: Any, *, slots: Mapping[str, Any] | None = None
) -> tuple[str, BlueprintRun] | None:
    """`(blueprint_id, BlueprintRun)` for a SUCCESSFUL blueprint `result_full`, or `None` when
    the payload carries no usable terminal SQL.

    The terminal SQL is exposed explicitly rather than inferred as "the last element of the
    result's sql list": rehydrated nodes are appended to that list FIRST on a D45 resume, so
    the positional assumption is not safe.
    """
    if not isinstance(result_full, dict):
        return None
    blueprint_id = result_full.get("blueprint_id")
    terminal_sql = result_full.get("terminal_sql")
    if not isinstance(blueprint_id, str) or not isinstance(terminal_sql, str):
        return None
    if not terminal_sql.strip():
        return None
    return blueprint_id, BlueprintRun(
        terminal_sql=terminal_sql,
        verification=blueprint_verification(result_full),
        slots=dict(slots or {}),
    )


def terminal_sql_by_id(runs: Mapping[str, BlueprintRun]) -> dict[str, str]:
    """The `blueprint_id -> terminal_sql` projection `resolve_designation` takes.

    A pure projection of the one captured map, NOT a second source — which is what keeps the
    pairing hazard `BlueprintRun` exists to close, closed.
    """
    return {blueprint_id: run.terminal_sql for blueprint_id, run in runs.items()}


@dataclass(frozen=True)
class DesignationItem:
    """One element of the model's designation, resolved as far as pure code can.

    `sql is None` with `named_blueprint` set is the REFUSAL case: the model named a blueprint
    that did not run successfully this turn, and there is nothing to resolve it to. It is
    deliberately carried here rather than dropped, because dropping it would silently lose a
    deliverable's table.
    """

    sql: str | None
    caption: str | None = None
    # Set ONLY when `sql` came FROM that blueprint's terminal SQL. A raw `sql=`
    # table has no blueprint behind it and therefore no chip and no badge.
    blueprint_id: str | None = None
    # What the model named, resolved or not. Drives the retryable nudge.
    named_blueprint: str | None = None


@dataclass(frozen=True)
class DesignatedTable:
    """One resolved, deduped, in-cap answer table."""

    sql: str
    caption: str | None = None
    blueprint_id: str | None = None


@dataclass(frozen=True)
class Designation:
    """The pure read of one `answerWithTable` call's arguments."""

    items: tuple[DesignationItem, ...]
    # `True` when the `tables` array was the source — the ONLY shape the model-facing
    # schema declares since 08 §O. `False` with a non-empty `items` means the LEGACY
    # top-level `sql`/`blueprint_id` pair was folded in: a persisted trail entry from
    # before the slim-down, or a model still working from a stale context. See
    # `resolve_designations`.
    from_tables_array: bool = False
    # Items in `tables` that carried no designation at all — the wholly-placeholder
    # item 03 §C.3.1 measured. Counted, never fatal.
    dropped_unresolvable: int = 0


def _resolve_item(item: Any, terminal_by_id: Mapping[str, str]) -> DesignationItem:
    """Resolve ONE designation mapping through the EXISTING `resolve_designation`.

    The flat-schema mis-fill is handled by construction: the live model emits every declared
    property and fills the unused ones with placeholders, so `{"sql": "", "blueprint_id":
    "bp-x", "caption": ""}` is the shape that actually arrives, and
    `clean_answer_sql`/`clean_blueprint_id` already map empty and whitespace to `None`.
    """
    raw_sql = clean_answer_sql(item.get("sql")) if isinstance(item, dict) else None
    named = clean_blueprint_id(item.get("blueprint_id")) if isinstance(item, dict) else None
    resolved = resolve_designation(item, terminal_by_id)
    raw_caption = item.get("caption") if isinstance(item, dict) else None
    caption = (
        sanitize_text(raw_caption, MAX_FIELD_CHARS) or None
        if isinstance(raw_caption, str)
        else None
    )
    return DesignationItem(
        sql=resolved,
        caption=caption,
        # A `sql=` that won over a `blueprint_id` is a raw table: it is NOT that
        # blueprint's result, so it inherits neither the chip nor the badge.
        blueprint_id=named if (resolved is not None and raw_sql is None) else None,
        named_blueprint=named,
    )


def resolve_designations(args: Any, terminal_by_id: Mapping[str, str]) -> Designation:
    """Read one `answerWithTable` call's arguments into an ORDERED item list.

    `tables` IS THE SHAPE: the model-facing schema declares exactly one carrier and requires
    it, so every live call arrives as a list — a single-table answer as a one-entry list.
    Everything below about the top-level `sql`/`blueprint_id` pair is a READ-PATH FOLD for
    arguments today's schema did not write.

    THE PRECEDENCE, mirroring the rule already in `resolve_designation`:

        `tables` wins when at least one of its items CARRIES A DESIGNATION. Otherwise the
        legacy top-level `sql`/`blueprint_id` pair is folded in as ONE item.

    "Carries a designation" — rather than "resolves" — is deliberate: `tables:
    [{blueprint_id: X}]` where X never ran carries a designation that RESOLVES to nothing,
    and under a resolves-only test it would fall back to an empty top-level pair and the
    model would silently lose its table. Under this test it stays the source, the item
    survives as a refusal, and the model gets the retryable nudge it can act on.

    WHAT THE FOLD IS FOR, now that nothing is supposed to send it. Two callers, neither
    optional:

      1. REPLAY. Every `answerWithTable` trail entry persisted before the schema change
         carries the pair at the top level and no `tables` at all. Those entries are
         SUCCESSFUL, so they replay cross-turn, seed a resumed window and rebuild a reloaded
         transcript — a read path that understood only the new shape would silently drop
         every one of them. There is no migration and no expiry date: old documents are read
         forever.
      2. A STALE-CONTEXT MODEL. A conversation already in flight, or a provider-side cached
         tool list, can still produce the old serialisation, and refusing it would cost the
         user a finished answer over a payload detail the runtime can read perfectly well.

    A LEGACY KEY CARRYING NO INFORMATION IS ABSENT, not an error: `sql: ""` and
    `blueprint_id: ""` clean to `None`, so `{"answer": …, "sql": "", "blueprint_id": "bp-…",
    "tables": []}` resolves through the blueprint id. The same rule is why placeholder soup
    BESIDE a real array is not a conflict — there is nothing there to conflict.

    NEVER A UNION. A model that fills `tables: [{blueprint_id: X}]` AND `sql: <X's SQL>` gets
    ONE table, not two. Fallback rather than union is also what lets an empty `tables: []`
    keep its legacy designation instead of silently losing it.

    Pure: no hooks, no store, no dedupe, no cap — see `finalize_designations`.
    """
    if not isinstance(args, dict):
        return Designation(items=())

    raw_tables = args.get("tables") if isinstance(args.get("tables"), list) else []
    candidates = [item for item in raw_tables if isinstance(item, dict)]
    # A NON-DICT ENTRY IS A DROPPED ITEM, not a non-event. `["SELECT 1"]` — a bare
    # string where an object belongs — is a real thing a model sends, and it was
    # being filtered out one line above the counter, so it vanished with no
    # `loop_answer_table_item_dropped` and no way to tell it from a call that sent
    # nothing at all. Counted here so the telemetry says how many entries the model
    # sent that produced no table, whatever shape they arrived in.
    dropped_non_dict = len(raw_tables) - len(candidates)
    resolved = [_resolve_item(item, terminal_by_id) for item in candidates]
    designating = [
        item for item in resolved if item.sql is not None or item.named_blueprint is not None
    ]
    # Every entry the model sent that produced no designation, counted ONCE and
    # carried into whichever branch is taken below — the fallback paths dropped this
    # on the floor before, so an array of pure placeholders beside a legacy pair
    # reported nothing at all.
    dropped_unresolvable = len(resolved) - len(designating) + dropped_non_dict
    if designating:
        return Designation(
            items=tuple(designating),
            from_tables_array=True,
            dropped_unresolvable=dropped_unresolvable,
        )

    # THE LEGACY FOLD. `args` itself is exactly the mapping `_resolve_item` reads —
    # a top-level `{sql, blueprint_id, caption}` IS one item's shape — so folding is
    # reading the call as its own single entry, not a second resolution path.
    single = _resolve_item(args, terminal_by_id)
    if single.sql is None and single.named_blueprint is None:
        # NO DESIGNATION ANYWHERE: no `tables` entry carried one and no legacy pair
        # was present either. Reported as an empty `items`, which the loop reads
        # back as `carried_designation=False` and turns into the retryable
        # `ANSWER_TABLE_NO_TABLE_DESIGNATED` nudge when the turn is holding
        # multi-row results it never tabled (`answer_table_no_table_designated`).
        #
        # The absence itself is NOT counted in `dropped_unresolvable` — that counts
        # ITEMS the model sent, and this is the absence of any usable one. (Entries
        # it DID send that designated nothing are still counted, and carried out
        # through here.) The two are different facts and the loop acts on them
        # differently: a dropped item is telemetry, an empty designation is a
        # refusal.
        return Designation(items=(), dropped_unresolvable=dropped_unresolvable)
    return Designation(items=(single,), dropped_unresolvable=dropped_unresolvable)


@dataclass(frozen=True)
class FinalizedDesignation:
    tables: tuple[DesignatedTable, ...]
    dropped_duplicate: int = 0
    dropped_over_cap: int = 0


def finalize_designations(items: Sequence[DesignationItem]) -> FinalizedDesignation:
    """Dedupe on resolved SQL (first-occurrence order, the discipline the turn's SQL
    accumulator applies) and cap at `MAX_ANSWER_TABLES`.

    OVERFLOW TRUNCATES, IT DOES NOT REFUSE — the one place the "REJECT, never truncate"
    precedent deliberately does not transfer. There, truncating rewrote a FROZEN
    `description` that enforcement depended on, so a clipped value was a corrupted one. Here
    the cap equals `MAX_INTENTS`, so exceeding it means the model designated more tables than
    it can possibly have intents, and refusing would cost the user a finished answer over the
    model's own bookkeeping.

    Items that resolved to nothing are skipped: the caller decides whether they are a refusal
    (the loop, which can nudge) or simply absent (a replay, which cannot).
    """
    tables: list[DesignatedTable] = []
    seen: set[str] = set()
    dropped_duplicate = 0
    dropped_over_cap = 0
    for item in items:
        if item.sql is None:
            continue
        if item.sql in seen:
            dropped_duplicate += 1
            continue
        seen.add(item.sql)
        if len(tables) >= MAX_ANSWER_TABLES:
            dropped_over_cap += 1
            continue
        tables.append(
            DesignatedTable(sql=item.sql, caption=item.caption, blueprint_id=item.blueprint_id)
        )
    return FinalizedDesignation(
        tables=tuple(tables),
        dropped_duplicate=dropped_duplicate,
        dropped_over_cap=dropped_over_cap,
    )


@dataclass(frozen=True)
class AnswerTable:
    """One designated answer table, fully enriched — what the wire carries.

    `provenance` is NOT on the wire and is NOT this entry's provenance: it is the D44 USES
    set of the designated query, persisted separately on the `answerWithTable` `TrailEntry`
    (`answer_table_provenance`) so a scope narrowing can drop out-of-scope tables
    individually. It must NEVER enter `_compute_turn_provenance_union`, which is fail-closed
    — one unparseable designated query would collapse the whole turn's union and drop the
    user's own answer from every later replay.
    """

    sql: str
    caption: str | None = None
    blueprint_use: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    provenance: frozenset[tuple[str, str]] | None = None

    def to_doc(self) -> dict[str, Any]:
        return {
            "sql": self.sql,
            "caption": self.caption,
            "blueprint_use": dict(self.blueprint_use) if self.blueprint_use else None,
            "verification": dict(self.verification) if self.verification else None,
        }


def enrich_table(
    table: DesignatedTable,
    runs: Mapping[str, BlueprintRun],
    *,
    provenance: frozenset[tuple[str, str]] | None = None,
) -> AnswerTable:
    """Attach the per-table chip + badge, both read from the SAME `BlueprintRun` that supplied
    the table's SQL. A raw `sql=` table gets neither: nothing verified it, and no blueprint
    produced it.
    """
    run = runs.get(table.blueprint_id) if table.blueprint_id is not None else None
    if run is None:
        return AnswerTable(sql=table.sql, caption=table.caption, provenance=provenance)
    return AnswerTable(
        sql=table.sql,
        caption=table.caption,
        blueprint_use={"blueprint_id": table.blueprint_id, "slots": dict(run.slots)},
        verification=dict(run.verification) if run.verification else None,
        provenance=provenance,
    )


def rollup_verification(tables: Sequence[AnswerTable]) -> dict[str, Any] | None:
    """The envelope's `verification`: a CONSERVATIVE AND over the DESIGNATED tables only.

    Green only if EVERY designated table is verified and there is at least one. An OR
    roll-up would be the over-claim restated; computing it over "any blueprint that ran
    anywhere in the turn" — what the turn-level accumulator does — badges an unverified grid
    green whenever some other part of the turn used a verified blueprint.

    ABSENCE, NOT `passed: False`, IS THE SIGNAL FOR "one of these is a hand-written query":
    that is not a failure at all, and a `False` would be read as one.

    EVERY designated table backed by an EMPTY blueprint result rolls up to the same explicit
    *empty — unverifiable* block the per-table badge carries. It is propagated rather than
    flattened to `None` because the UI renders the ENVELOPE's badge at N<=1, which is exactly
    the confirmed case — flattening would make the fix invisible in the situation it exists
    for.

    A MIXED set rolls up to `None`: "empty" would over-state it, since part of the answer has
    rows, and "verified" would be the original over-claim restated.
    """
    if not tables:
        return None
    if any(table.verification is None for table in tables):
        return None
    empty = [bool((table.verification or {}).get("empty_result")) for table in tables]
    if all(empty):
        return {
            "passed": False,
            "method": "blueprint_gate",
            "grain_checked": False,
            "empty_result": True,
            "status": VERIFICATION_EMPTY_STATUS,
        }
    if any(empty):
        return None
    return {
        "passed": True,
        "method": "blueprint_gate",
        "grain_checked": all(
            bool((table.verification or {}).get("grain_checked")) for table in tables
        ),
    }


def is_answer_table_in_scope(
    provenance: frozenset[tuple[str, str]] | None, column_scope: frozenset[str]
) -> bool:
    """Is this designated table still offerable under *column_scope*?

    ONE predicate, read by the live path and by `session_history.project_history`, for the
    reason `resolve_designation` itself exists: reading a designation one way live and
    another way on reload silently dropped every blueprint designation once, and RELOAD is
    the only place that regression shows.

    UNDETERMINED PROVENANCE (`None`) IS KEPT, deliberately. The gap being closed here is a
    CONSISTENCY defect, not an entitlement hole: `POST /query/page` re-enforces column scope
    at execution under the caller's own credentials, so nothing out of scope was ever
    readable. Dropping on `None` would buy no access control while silently deleting every
    grid whose query the runtime's own extractor cannot parse — an uncatalogued table is
    enough — for queries `/query/page` executes perfectly well today. A table PROVEN out of
    scope is still dropped.
    """
    if provenance is None:
        return True
    return is_provenance_in_scope(provenance, column_scope)


class AnswerWithTableTool(RuntimeToolBase):
    _INTERNAL_ERROR_CODE = "RUNTIME_TOOL_INTERNAL_ERROR"
    _INTERNAL_ERROR_MESSAGE = "The tool could not complete. Please try again."

    def _span_args(self, model_args: dict[str, Any]) -> dict[str, Any]:
        return {}

    """The `answerWithTable(answer, tables=[{sql | blueprint_id, caption}])` tool.

        Stateless: `run` never raises on malformed args (the loop's `_run_runtime_tool` also
        guards, defense in depth) and holds nothing itself. The loop reads the ARGUMENTS — the
        same discipline as `recordAssumptions` — and treats a SUCCESSFUL call as the turn's
        terminal event.
    """

    tool_name = TOOL_NAME

    async def _execute(
        self,
        arguments: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: TurnContext | None = None,
    ) -> ToolResult:
        # *turn* (03 §C.1): the loop threads its own `TurnContext` to every
        # runtime tool. This one does not need it — accepted and ignored so the
        # `RuntimeTool` protocol has ONE signature rather than two shapes the
        # dispatch site has to tell apart.
        args = arguments if isinstance(arguments, dict) else {}
        answered = clean_answer_text(args.get("answer")) is not None
        # Reported off the SAME precedence the loop resolves with (`tables` first,
        # then the top-level pair), so the confirmation can never claim a
        # designation the loop did not make, or deny one it did.
        designated = bool(resolve_designations(args, {}).items)
        # A tiny confirmation. In the normal (terminal) case the model never sees
        # it — the turn ends on this call — but it is still persisted to the trail,
        # so it must be a real, honest result rather than a placeholder. It
        # deliberately carries NO rows: echoing the table back would re-create the
        # transcribe-the-rows behaviour this tool exists to stop.
        confirmation = ResultPreview(
            columns=["answered", "table_designated"],
            row_count=1,
            truncated=False,
            preview_rows=[[answered, designated]],
        )
        return ToolResult(
            status="ok",
            tool_name=TOOL_NAME,
            error_code=None,
            retryable=None,
            user_message=None,
            # DETERMINED-EMPTY, like `recordAssumptions` and every other data-free
            # tool in `provenance/capture.py::_NO_PROVENANCE_TOOLS`. This call reads
            # NO warehouse data — it echoes back the model's own text and a query
            # reference. `None` would mean UNDETERMINED and, because
            # `filter_trail`'s current-turn exemption is status-gated to
            # `status != "ok"`, would hand the model the D94 "result withheld"
            # sentinel (the bug fixed for recordAssumptions).
            provenance=frozenset(),
            result_preview=confirmation,
            result_full=None,
        )


__all__ = [
    "MAX_ANSWER_TABLES",
    "TOOL_NAME",
    "VERIFICATION_EMPTY_STATUS",
    "AnswerTable",
    "AnswerWithTableTool",
    "BlueprintRun",
    "Designation",
    "DesignationItem",
    "DesignatedTable",
    "FinalizedDesignation",
    "blueprint_run_from_result",
    "blueprint_verification",
    "clean_answer_sql",
    "clean_answer_text",
    "clean_blueprint_id",
    "enrich_table",
    "finalize_designations",
    "is_answer_table_in_scope",
    "is_zero_row_count",
    "resolve_designation",
    "resolve_designations",
    "rollup_verification",
    "terminal_sql_by_id",
]
