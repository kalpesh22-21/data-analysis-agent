"""Internal table-resolution and saved-history regressions.

Live answer selection now requires execution references and is covered by
`tests/runtime/test_answer_document.py` and `tests/runtime/test_app.py`.
Retired raw-SQL finalization and parallel UI-envelope assertions are intentionally
not part of the new document contract. Keep these resolver/history checks for
existing saved sessions and the private verification machinery.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import (
    MAX_INTENTS,
    UpdateAnalysisStateTool,
)
from data_agent.runtime.composite.answer_with_table import (
    MAX_ANSWER_TABLES,
    AnswerTable,
    AnswerWithTableTool,
    BlueprintRun,
    finalize_designations,
    resolve_designations,
    rollup_verification,
)
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolResult
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import (
    ResultPreview,
    SessionDoc,
    TrailEntry,
    TurnMessage,
)
from data_agent.runtime.session_history import project_history
from tests._blueprint_gate import expand_blueprint
from tests.runtime.final_answer import final_answer

pytestmark = pytest.mark.usefixtures("answer_tools", "blueprint_consulted")

SESSION_ID = "sess-multi-table"
_E = "dbpcm_warehouse.employee"
_P = "dbpcm_warehouse.payroll"
# Two REAL, parseable queries over a real catalog, so per-table provenance is
# genuinely extracted rather than degrading to `None` and proving nothing.
CATALOG = CatalogHandle(
    {
        _E: {"employee_code": "String", "department_name": "String"},
        _P: {"employee_code": "String", "annual_salary": "Float64"},
    }
)
HEADCOUNT_SQL = f"SELECT department_name, count() AS n FROM {_E} GROUP BY department_name"
SALARY_SQL = f"SELECT employee_code, annual_salary FROM {_P}"

STATE = "updateAnalysisState"
ANSWER = "answerWithTable"


def _creds(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="SECRET", column_scope=scope)


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return [
        {"type": "function", "name": name, "parameters": {}}
        for name in ("runQuery", "getTableSchema", "listDatabases", "listTables")
    ]


class _StubBlueprintTool:
    """A `runBlueprint` stand-in serving SEVERAL blueprints from one registry.

    `verified` is per blueprint on purpose: the mixed-verification case is the one
    that would have accepted an OR roll-up, and it cannot be expressed with a
    single-blueprint stub.
    """

    tool_name = "runBlueprint"

    def __init__(
        self,
        registry: dict[str, tuple[str, bool]],
        *,
        empty: frozenset[str] = frozenset(),
    ) -> None:
        self._registry = registry
        # J6: which blueprints come back with ZERO rows. A separate set rather
        # than a third tuple element so every existing call site is unchanged —
        # emptiness is orthogonal to whether the gate ran.
        self._empty = empty

    async def run(
        self, arguments: dict, credentials: RuntimeCredentials, turn: Any = None, tool_call_id=None
    ) -> ToolResult:
        blueprint_id = arguments.get("id")
        terminal_sql, verified = self._registry[blueprint_id]
        is_empty = blueprint_id in self._empty
        rows: list[list[Any]] = [] if is_empty else [["1"]]
        verify: dict[str, Any] | None = None
        if verified:
            # Mirrors `executor._verify_block` exactly, including the honest
            # `grain_checked: False` an empty result carries.
            verify = {"grain_checked": not is_empty}
            if is_empty:
                verify["empty_result"] = True
        result_full = {
            "blueprint_id": blueprint_id,
            "status": "verified" if verified else "unverified",
            "sql": [terminal_sql],
            "terminal_sql": terminal_sql,
            "columns": ["x"],
            "row_count": len(rows),
            "truncated": False,
            "preview_rows": rows,
            "verify": verify,
        }
        return ToolResult(
            status="ok",
            tool_name="runBlueprint",
            error_code=None,
            retryable=None,
            user_message=None,
            provenance=frozenset({(_E, "department_name")}),
            result_preview=ResultPreview(
                columns=["x"], row_count=len(rows), truncated=False, preview_rows=rows
            ),
            result_full=result_full,
        )


def _build(
    turns: list[ModelTurnResult],
    *,
    blueprints: dict[str, tuple[str, bool]] | None = None,
    empty_blueprints: frozenset[str] = frozenset(),
) -> tuple[AgentLoop, InMemorySessionStore, list[tuple[str, dict[str, Any]]]]:
    store = InMemorySessionStore()
    events: list[tuple[str, dict[str, Any]]] = []

    def _observe(event: str, payload: dict[str, Any]) -> None:
        events.append((event, dict(payload)))

    runtime_tools: dict[str, Any] = {
        ANSWER: AnswerWithTableTool(),
        STATE: UpdateAnalysisStateTool(session_store=store, observer=_observe),
    }
    if blueprints:
        runtime_tools["runBlueprint"] = _StubBlueprintTool(blueprints, empty=empty_blueprints)
    loop = AgentLoop(
        model_client=ScriptedModelClient(turns),
        tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG, observer=_observe),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=_observe,
        runtime_tools=runtime_tools,
    )
    return loop, store, events


def _answer(call_id: str, **args: Any) -> ModelTurnResult:
    return ModelTurnResult(
        tool_calls=[ToolCallRequest(id=call_id, name=ANSWER, arguments=dict(args))]
    )


def _run_blueprints(*ids: str) -> ModelTurnResult:
    return ModelTurnResult(
        tool_calls=[
            ToolCallRequest(
                id=f"b{index}",
                name="runBlueprint",
                arguments={"id": blueprint_id, "slot_bindings": {}},
            )
            for index, blueprint_id in enumerate(ids, start=1)
        ]
    )


def _payloads(events: list[tuple[str, dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return [payload for event, payload in events if event == name]


# ---------------------------------------------------------------------------
# The pure resolver — precedence, the flat-schema mis-fill, dedupe, the cap
# ---------------------------------------------------------------------------


def test_an_item_of_tables_is_exactly_what_resolve_designation_already_reads() -> None:
    """08 §B, the load-bearing claim: multi-table adds NO new resolution logic.

    An element of `tables` is the same `{sql?, blueprint_id?}` mapping the existing
    `resolve_designation` takes off a call's top-level arguments, so both forms
    resolve through one function. If this ever needed a second code path, the
    reload-vs-live divergence `resolve_designation` exists to prevent would be back.
    """
    designation = resolve_designations(
        {"tables": [{"blueprint_id": "bp-a"}, {"sql": SALARY_SQL}]},
        {"bp-a": HEADCOUNT_SQL},
    )
    assert [item.sql for item in designation.items] == [HEADCOUNT_SQL, SALARY_SQL]
    # The blueprint attribution is carried only where the SQL actually CAME from a
    # blueprint — a raw `sql=` table must inherit neither chip nor badge.
    assert [item.blueprint_id for item in designation.items] == ["bp-a", None]


def test_the_flat_schema_mis_fill_is_tolerated_item_by_item() -> None:
    """03 §C.3.1's measured behaviour: the live model CANNOT omit keys.

    It emits every declared property and fills the unused ones with placeholders —
    `""` for a string. Six live `updateAnalysisState` calls were rejected for
    exactly that, and `resolve_designation`'s own docstring records `sql=""` beside
    `blueprint_id` as the form the live model actually sends. So the placeholder
    shape is the EXPECTED serialisation here, not an edge case.

    PLACEHOLDER SOUP AT THE TOP LEVEL IS NOT A CONFLICT. The legacy `sql: ""` and
    `blueprint_id: ""` beside a real `tables` array carry no information, so by the
    rule that a key carrying nothing is ABSENT they are simply not there: the array
    wins and nothing is refused. This is the exact serialisation 08 §O retired the
    top-level pair over, arriving from a model still working off a stale context.
    """
    designation = resolve_designations(
        {
            "answer": "…",
            "sql": "",
            "blueprint_id": "",
            "tables": [
                {"sql": "", "blueprint_id": "bp-a", "caption": "Headcount"},
                {"sql": SALARY_SQL, "blueprint_id": "", "caption": ""},
            ],
        },
        {"bp-a": HEADCOUNT_SQL},
    )
    assert [item.sql for item in designation.items] == [HEADCOUNT_SQL, SALARY_SQL]
    # An all-placeholder caption collapses to `None`, so the UI falls back to its
    # own static summary rather than rendering an empty label.
    assert [item.caption for item in designation.items] == ["Headcount", None]


def test_a_non_dict_tables_entry_is_counted_as_a_drop_not_filtered_silently() -> None:
    """`tables: ["SELECT 1"]` — a bare string where an object belongs, which is a
    real thing a model sends.

    It was filtered out ONE LINE ABOVE the counter, so it produced no
    `loop_answer_table_item_dropped` and was indistinguishable from a call that sent
    an empty array. That difference matters now: an empty array is a refusal
    (`carried_designation=False`) while a malformed entry is telemetry, and the two
    were reporting the same nothing.
    """
    designation = resolve_designations(
        {"answer": "x", "tables": ["SELECT 1", 42, None, {"sql": SALARY_SQL}]}, {}
    )
    assert [item.sql for item in designation.items] == [SALARY_SQL]
    assert designation.dropped_unresolvable == 3


def test_dropped_entries_are_still_counted_when_the_legacy_pair_wins() -> None:
    """The count survives the fallback branches, which dropped it on the floor.

    An array of pure placeholders beside a legacy pair reported ZERO drops, because
    only the `tables`-wins branch carried the number out. The entries were sent and
    produced no table either way, so they are counted either way.
    """
    designation = resolve_designations(
        {"answer": "x", "sql": SALARY_SQL, "tables": [{"sql": "", "blueprint_id": ""}]}, {}
    )
    assert [item.sql for item in designation.items] == [SALARY_SQL]
    assert designation.dropped_unresolvable == 1


def test_a_wholly_placeholder_item_is_dropped_and_counted() -> None:
    """The residual risk 08 §B.2 names: an item that designates nothing at all.
    Rule is drop-it-count-it — it cannot be a refusal, because it names no
    blueprint to nudge the model about."""
    designation = resolve_designations(
        {"tables": [{"sql": "", "blueprint_id": "", "caption": ""}, {"sql": SALARY_SQL}]},
        {},
    )
    assert [item.sql for item in designation.items] == [SALARY_SQL]
    assert designation.dropped_unresolvable == 1


def test_a_single_table_answer_is_a_one_entry_tables_list() -> None:
    """08 §O: `tables` is the ONLY carrier the model-facing schema declares, and the
    6/6 single-deliverable case is a list of length one. This is the shape the live
    model sends today; every legacy test below it is a READ-path guarantee."""
    for args, expected in (
        ({"answer": "x", "tables": [{"sql": SALARY_SQL}]}, SALARY_SQL),
        (
            {"answer": "x", "tables": [{"sql": "", "blueprint_id": "bp-a"}]},
            HEADCOUNT_SQL,
        ),
    ):
        designation = resolve_designations(args, {"bp-a": HEADCOUNT_SQL})
        assert [item.sql for item in designation.items] == [expected]
        assert designation.from_tables_array is True


def test_a_legacy_top_level_pair_folds_into_a_one_entry_list() -> None:
    """THE FOLD (08 §O). The top-level `sql`/`blueprint_id` pair is gone from the
    model-facing schema, but it is READ forever, because two callers need it and
    neither is optional: every `answerWithTable` trail entry written before the
    change carries that shape and replays cross-turn, and a model working from a
    stale context can still emit it.

    It is FOLDED, never refused. A non-empty legacy designation with no usable
    `tables` becomes a one-entry list — refusing it would cost the user a finished
    answer over a payload detail the runtime reads perfectly well."""
    for args, expected in (
        ({"answer": "x", "sql": SALARY_SQL}, SALARY_SQL),
        ({"answer": "x", "sql": "", "blueprint_id": "bp-a"}, HEADCOUNT_SQL),
    ):
        designation = resolve_designations(args, {"bp-a": HEADCOUNT_SQL})
        assert [item.sql for item in designation.items] == [expected]
        # The fold is REPORTED as what it is: the array was not the source.
        assert designation.from_tables_array is False


def test_the_r7_q1_placeholder_call_still_resolves_rather_than_refusing() -> None:
    """The live call 08 §O was written from, verbatim.

    R7 q1 sent `{"answer": …, "sql": "", "blueprint_id": "bp-…", "tables": []}` —
    four declared properties carrying ONE field's worth of information, because the
    model cannot omit a declared key. That payload is exactly what the schema
    slim-down deletes, and until every in-flight context has turned over it is also
    exactly what can still arrive. It must resolve, not refuse: the two placeholders
    and the empty array carry nothing, so the blueprint id is the whole call."""
    designation = resolve_designations(
        {"answer": "…", "sql": "", "blueprint_id": "bp-a", "tables": []},
        {"bp-a": HEADCOUNT_SQL},
    )
    assert [item.sql for item in designation.items] == [HEADCOUNT_SQL]
    assert designation.dropped_unresolvable == 0


def test_an_empty_tables_array_falls_back_to_the_legacy_pair() -> None:
    """`tables: []` is the array analogue of `""` — the same placeholder behaviour
    one type up, and now the likelier one: `tables` is REQUIRED, so a model with
    nothing to put there must still emit the key. Falling back (rather than
    unioning) is what stops a call that filled the array with nothing from silently
    losing its table."""
    designation = resolve_designations({"answer": "x", "sql": SALARY_SQL, "tables": []}, {})
    assert [item.sql for item in designation.items] == [SALARY_SQL]


def test_tables_and_a_legacy_pair_never_union() -> None:
    """A call that fills BOTH gets one table, not two. `tables` wins outright, and
    the legacy pair is not consulted at all — it is a stale carrier, not a second
    deliverable."""
    designation = resolve_designations(
        {"answer": "x", "sql": SALARY_SQL, "tables": [{"blueprint_id": "bp-a"}]},
        {"bp-a": HEADCOUNT_SQL},
    )
    assert [item.sql for item in designation.items] == [HEADCOUNT_SQL]


def test_an_unrun_blueprint_inside_tables_stays_a_refusal_not_a_fallback() -> None:
    """The sharp edge of the precedence rule, and the reason it tests "carries a
    designation" rather than "resolves".

    Under a resolves-only test, `tables: [{blueprint_id: X}]` with X unrun would
    yield nothing, fall back to an empty top-level pair, and the model would
    silently lose its table — precisely what §B.4 step 3 forbids. It must survive
    as a refusal so the retryable nudge can fire.
    """
    designation = resolve_designations({"answer": "x", "tables": [{"blueprint_id": "bp-x"}]}, {})
    assert len(designation.items) == 1
    assert designation.items[0].sql is None
    assert designation.items[0].named_blueprint == "bp-x"


def test_duplicate_tables_are_deduped_on_resolved_sql() -> None:
    """One query covering two parts is ONE table. Deduping on the RESOLVED SQL is
    what makes `[{blueprint_id: X}, {sql: <X's SQL>}]` collapse too."""
    designation = resolve_designations(
        {"tables": [{"blueprint_id": "bp-a"}, {"sql": HEADCOUNT_SQL}, {"sql": SALARY_SQL}]},
        {"bp-a": HEADCOUNT_SQL},
    )
    finalized = finalize_designations(designation.items)
    assert [t.sql for t in finalized.tables] == [HEADCOUNT_SQL, SALARY_SQL]
    assert finalized.dropped_duplicate == 1


def test_the_cap_is_max_intents_and_is_imported_not_re_declared() -> None:
    """08 §F. A cap BELOW `MAX_INTENTS` re-creates the merge conflict through the
    payload: a 5-intent turn under a 4-table ceiling is told it cannot show a table
    per part, and the only move left is to merge. Tying the two together means the
    ceiling can never be the thing that forces one — and importing rather than
    re-declaring means they cannot drift apart later."""
    assert MAX_ANSWER_TABLES == MAX_INTENTS == 8


def test_cap_overflow_truncates_and_is_counted_never_refused() -> None:
    """The one place 03 §A.4's "REJECT, never truncate" precedent deliberately does
    NOT transfer. There, truncating rewrote a FROZEN `description` enforcement
    depended on. Here the cap equals `MAX_INTENTS`, so exceeding it means the model
    named more tables than it can have intents — and refusing would cost the user a
    finished answer over the model's own bookkeeping."""
    items = [{"sql": f"SELECT {n} FROM {_E}"} for n in range(MAX_ANSWER_TABLES + 3)]
    finalized = finalize_designations(resolve_designations({"tables": items}, {}).items)
    assert len(finalized.tables) == MAX_ANSWER_TABLES
    assert finalized.dropped_over_cap == 3


# ---------------------------------------------------------------------------
# Verification — the AND roll-up, and the badge that is deliberately lost
# ---------------------------------------------------------------------------


def test_the_roll_up_is_an_and_and_never_emits_passed_false() -> None:
    verified = AnswerTable(
        sql="a",
        verification={
            "passed": True,
            "status": "structural checks passed",
            "method": "blueprint_gate",
            "grain_checked": True,
        },
    )
    unverified = AnswerTable(sql="b")
    assert rollup_verification([verified])["passed"] is True
    # AND, not OR. An OR roll-up is the over-claim restated, and this case is the
    # one that would have accepted it.
    assert rollup_verification([verified, unverified]) is None
    assert rollup_verification([]) is None
    # ABSENCE is the only negative signal. A `passed: False` would render as a red
    # "verification failed" badge whose real meaning is "one of these is a
    # hand-written query" — which is not a failure at all.
    assert json.dumps([rollup_verification([verified, unverified])]) == "[null]"










async def test_a_turn_with_no_designated_table_keeps_its_turn_level_enrichment() -> None:
    """The other side of the same rule, and the reason the roll-up is scoped to
    "when there IS a table".

    A verified blueprint answered in prose has NO grid to over-claim on, so
    `verification`/`blueprint_use` keep the meaning they have always had — this
    turn's answer came from a verified blueprint. Downgrading those to `null` would
    lose information rather than add it (08 §C.2 option 2, rejected), and it is the
    enrichment the blueprint approval-resume seed exists to carry across a pause.
    """
    loop, store, _events = _build(
        [
            _run_blueprints("bp-headcount"),
            final_answer(assistant_text="There are 4 in Sales."),
        ],
        blueprints={"bp-headcount": (HEADCOUNT_SQL, True)},
    )
    await expand_blueprint(store, SESSION_ID, "bp-headcount")
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q?")
    assert outcome.answer_tables is None
    assert outcome.verification == {
        "passed": True,
        "status": "structural checks passed",
        "method": "blueprint_gate",
        "grain_checked": True,
    }
    assert outcome.blueprint_use == {"blueprint_id": "bp-headcount", "slots": {}}


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# Per-table provenance (08 §D)
# ---------------------------------------------------------------------------




def test_a_legacy_trail_document_loads_with_no_answer_table_provenance() -> None:
    """Additive-with-`None`-default is the established pattern (`authoritative`,
    `denial_detail`, `serves_intent`); a document written before the field existed
    must load byte-identically."""
    legacy = {
        "turn_index": 0,
        "tool_call_id": "a1",
        "tool_name": ANSWER,
        "args": {"answer": "x", "sql": SALARY_SQL},
        "status": "ok",
        "error_code": None,
        "provenance": [],
        "result_preview": None,
        "result_full_ref": None,
        "ts": "t",
    }
    assert TrailEntry.from_doc(legacy).answer_table_provenance is None


def test_a_malformed_per_table_provenance_costs_its_table_not_the_session() -> None:
    """The totality this field's loader PROMISES, asserted at the depth that was
    actually broken.

    `_answer_table_provenance_from_doc` type-checked the outer list and then indexed
    `pair[0]`/`pair[1]` blind, so a single inner pair of the wrong length —
    `[[["a"]]]`, one table, one pair, one element — raised `IndexError` out of
    `TrailEntry.from_doc`, out of `SessionDoc.from_doc`, and out of EVERY subsequent
    read of that session. A per-table optimisation bricked the whole conversation.

    The degrade is per POSITION: a malformed table loses its own lineage (`None`,
    which is exactly the legacy shape the reader already handles) and a well-formed
    sibling in the same entry keeps its own.
    """
    base = {
        "turn_index": 0,
        "tool_call_id": "a1",
        "tool_name": ANSWER,
        "args": {"answer": "x"},
        "status": "ok",
        "error_code": None,
        "provenance": [],
        "result_preview": None,
        "result_full_ref": None,
        "ts": "t",
    }

    # The exact shape that raised: one table, one pair, one element.
    entry = TrailEntry.from_doc({**base, "answer_table_provenance": [[["a"]]]})
    assert entry.answer_table_provenance == (None,)

    # And it degrades POSITIONALLY — table 0 is malformed, table 1 is not.
    mixed = TrailEntry.from_doc(
        {**base, "answer_table_provenance": [[["a"]], [[_E, "department_name"]]]}
    )
    assert mixed.answer_table_provenance == (None, frozenset({(_E, "department_name")}))

    # Every other wrong shape is total too, and none of them raises.
    for payload in ([["not-a-pair"]], [[{}]], [[[1, 2, 3]]], [42], ["x"]):
        assert TrailEntry.from_doc({**base, "answer_table_provenance": payload}) is not None

    # THE POINT: the session still loads, which is what the bug destroyed.
    doc = SessionDoc(session_id=SESSION_ID, created_at="t0", last_activity="t0").to_doc()
    doc["tool_trail"] = [{**base, "answer_table_provenance": [[["a"]]]}]
    restored = SessionDoc.from_doc(doc)
    assert len(restored.tool_trail) == 1
    assert restored.tool_trail[0].answer_table_provenance == (None,)






async def test_history_filters_tables_individually_and_the_turn_gate_still_dominates() -> None:
    """Both halves of 08 §D.3, asserted together so a later reader does not mistake
    the limit for a bug.

    (a) Per-table filtering works on RELOAD, from the persisted field, through the
        SAME predicate the live path uses.
    (b) The answer-survival gate is TURN-WIDE and deliberately so: the turn's
        provenance union already contains every table's columns, so a narrowing
        that excludes one table's columns drops the assistant MESSAGE, and all N
        tables go with it. Loosening that is a D44 message-filter change and is out
        of scope here.
    """
    messages = [
        TurnMessage(turn_index=0, role="user", content="q?", ts="t0", provenance=frozenset()),
        TurnMessage(
            turn_index=0,
            role="assistant",
            content="Two things.",
            ts="t1",
            # The turn union, as the loop would have written it: it covers both.
            provenance=frozenset(
                {(_E, "department_name"), (_P, "employee_code"), (_P, "annual_salary")}
            ),
        ),
    ]
    trail = [
        TrailEntry(
            turn_index=0,
            tool_call_id="a1",
            tool_name=ANSWER,
            args={"answer": "x", "tables": [{"sql": HEADCOUNT_SQL}, {"sql": SALARY_SQL}]},
            status="ok",
            error_code=None,
            provenance=frozenset(),
            result_preview=None,
            result_full_ref=None,
            ts="t2",
            answer_table_provenance=(
                frozenset({(_E, "department_name")}),
                frozenset({(_P, "employee_code"), (_P, "annual_salary")}),
            ),
        )
    ]
    # (a) A scope that keeps the ANSWER (its union is still covered) but excludes
    #     table 2's columns drops only table 2.
    wide = frozenset(
        {
            f"{_E}.department_name",
            f"{_P}.employee_code",
            f"{_P}.annual_salary",
        }
    )
    events: list[tuple[str, dict[str, Any]]] = []
    body = project_history(
        messages, trail, wide, None, observer=lambda e, p: events.append((e, dict(p)))
    )
    assert [t["sql"] for t in body["turns"][0]["answer_tables"]] == [HEADCOUNT_SQL, SALARY_SQL]
    assert body["turns"][0]["answer_sql"] == HEADCOUNT_SQL
    assert events == []

    # (b) Narrow to exclude table 2's columns: the turn union is no longer covered,
    #     so the assistant message is withheld and BOTH tables go with it. The
    #     per-table field cannot rescue what the turn-wide gate has already dropped.
    narrow = frozenset({f"{_E}.department_name"})
    narrowed = project_history(messages, trail, narrow, None)
    assert narrowed["turns"][0]["answer"] is None
    assert narrowed["turns"][0]["answer_tables"] is None
    assert narrowed["turns"][0]["answer_sql"] is None


def test_history_emits_the_scope_drop_signal_when_it_bites() -> None:
    """`history_answer_table_scope_dropped` makes the gap between "the message
    survived" and "its tables survived" visible rather than inferred."""
    messages = [
        TurnMessage(turn_index=0, role="user", content="q?", ts="t0", provenance=frozenset()),
        TurnMessage(
            turn_index=0,
            role="assistant",
            content="Two things.",
            ts="t1",
            # The answer itself reads only the in-scope table — which is exactly the
            # documented case: a designated `sql=` need never have been executed.
            provenance=frozenset({(_E, "department_name")}),
        ),
    ]
    trail = [
        TrailEntry(
            turn_index=0,
            tool_call_id="a1",
            tool_name=ANSWER,
            args={"answer": "x", "tables": [{"sql": HEADCOUNT_SQL}, {"sql": SALARY_SQL}]},
            status="ok",
            error_code=None,
            provenance=frozenset(),
            result_preview=None,
            result_full_ref=None,
            ts="t2",
            answer_table_provenance=(
                frozenset({(_E, "department_name")}),
                frozenset({(_P, "annual_salary")}),
            ),
        )
    ]
    events: list[tuple[str, dict[str, Any]]] = []
    body = project_history(
        messages,
        trail,
        frozenset({f"{_E}.department_name"}),
        None,
        observer=lambda e, p: events.append((e, dict(p))),
    )
    assert [t["sql"] for t in body["turns"][0]["answer_tables"]] == [HEADCOUNT_SQL]
    assert ("history_answer_table_scope_dropped", {"table_count": 1}) in events


def _history_app(monkeypatch, model: ScriptedModelClient, scope: frozenset[str]):
    from fastapi.testclient import TestClient

    from data_agent.runtime import app as app_module
    from data_agent.runtime.app import create_app
    from data_agent.runtime.config import RuntimeSettings

    monkeypatch.setattr(app_module, "verify_jwt", lambda *a, **k: scope)
    app = create_app(
        settings=RuntimeSettings(_env_file=None),
        session_store=InMemorySessionStore(),
        mcp_client=FakeMCPClient(),
        model_client=model,
        catalog=CATALOG,
    )
    return TestClient(app)






def test_history_reconstructs_per_table_badges_from_the_same_two_keys() -> None:
    """The reload path reads `terminal_sql` and the verify block out of the SAME
    `result_full` the in-window capture reads, through the same constructor — so a
    reloaded badge is the badge the live turn showed."""
    messages = [
        TurnMessage(turn_index=0, role="user", content="q?", ts="t0", provenance=frozenset()),
        TurnMessage(turn_index=0, role="assistant", content="x", ts="t1", provenance=frozenset()),
    ]
    trail = [
        TrailEntry(
            turn_index=0,
            tool_call_id="a1",
            tool_name=ANSWER,
            args={"answer": "x", "tables": [{"blueprint_id": "bp-a"}, {"blueprint_id": "bp-b"}]},
            status="ok",
            error_code=None,
            provenance=frozenset(),
            result_preview=None,
            result_full_ref=None,
            ts="t2",
        )
    ]
    body = project_history(
        messages,
        trail,
        frozenset(),
        None,
        blueprint_runs={
            "bp-a": BlueprintRun(
                terminal_sql=HEADCOUNT_SQL,
                verification={
                    "passed": True,
                    "status": "structural checks passed",
                    "method": "blueprint_gate",
                    "grain_checked": True,
                },
                slots={"window_months": 6},
            ),
            "bp-b": BlueprintRun(terminal_sql=SALARY_SQL),
        },
    )
    tables = body["turns"][0]["answer_tables"]
    assert tables[0]["blueprint_use"] == {
        "blueprint_id": "bp-a",
        "slots": {"window_months": 6},
    }
    assert tables[0]["verification"]["passed"] is True
    assert tables[1]["verification"] is None


# ---------------------------------------------------------------------------
# The refusal and the terminal exit still behave
# ---------------------------------------------------------------------------


async def test_an_unrun_blueprint_in_tables_refuses_the_whole_call() -> None:
    """08 §B.4 step 3, through the loop: the retryable nudge, naming the blueprint,
    and the turn continues instead of finishing with prose and no table. Dropping
    the item would silently lose a deliverable's table — the failure multi-table
    exists to fix."""
    loop, store, _events = _build(
        [
            _answer(
                "a1",
                answer="See tables.",
                tables=[{"sql": HEADCOUNT_SQL}, {"blueprint_id": "bp-never-ran"}],
            ),
            final_answer(assistant_text="I don't have any information to answer your question."),
        ]
    )
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q?")

    assert outcome.assistant_text == "I don't have any information to answer your question."
    assert outcome.answer_tables is None
    entry = [e for e in await store.load_trail(SESSION_ID) if e.tool_name == ANSWER][0]
    assert entry.error_code == "ANSWER_TABLE_BLUEPRINT_NOT_RUN"
    assert "bp-never-ran" in entry.denial_detail
    # A refused call persists NO per-table provenance: there are no tables.
    assert entry.answer_table_provenance is None




async def test_finalization_enforcement_still_refuses_a_multi_table_answer() -> None:
    """05 §B.1's exit #2, unchanged by the widened payload: a pending intent refuses
    the call BEFORE resolution, so the answer-table lifecycle never fires for a
    designation that is about to be refused."""
    loop, store, events = _build(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="s1",
                        name=STATE,
                        arguments={
                            "intents": [
                                {"description": "first deliverable"},
                                {"description": "second deliverable"},
                            ]
                        },
                    )
                ]
            ),
            _answer(
                "a1",
                answer="Both.",
                tables=[{"sql": HEADCOUNT_SQL}, {"sql": SALARY_SQL}],
            ),
            final_answer(assistant_text="Recovered."),
        ]
    )
    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="two things?")

    entry = [e for e in await store.load_trail(SESSION_ID) if e.tool_name == ANSWER][0]
    assert entry.status == "ok"
    assert entry.error_code is None
    assert [e for e, _ in events if e == "loop_finalization_refused"]
    # The seams never fired for the refused designation.
    assert _payloads(events, "loop_answer_tables_designated")
    assert not (await store.get_or_create_session(SESSION_ID)).messages[-1].content == "Both."


# ---------------------------------------------------------------------------
# Telemetry (08 §L) — shape-only, and it must survive the attribute allowlist
# ---------------------------------------------------------------------------








def test_the_three_new_keys_actually_reach_the_span() -> None:
    """README finding 10: emitting an event does not publish its payload.
    `_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST` is a strict key allowlist, so a new key
    that is not added there produces a correctly-named span carrying nothing.
    Asserted by reading the attributes back OFF the span."""
    from data_agent.runtime.observability import tracing

    captured: list[dict[str, Any]] = []

    class _Span:
        def set_attribute(self, key: str, value: Any) -> None:
            captured[-1][key] = value

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Tracer:
        def start_as_current_span(self, name, **kwargs):
            captured.append(dict(kwargs.get("attributes") or {}))
            return _Span()

    observe = tracing.guardrail_observer(_Tracer())
    observe(
        "loop_answer_tables_designated",
        {"table_count": 3, "blueprint_table_count": 2, "verified_table_count": 1},
    )
    assert captured[-1]["table_count"] == 3
    assert captured[-1]["blueprint_table_count"] == 2
    assert captured[-1]["verified_table_count"] == 1

    # The empty-designation event (08 §O) reaches the span too. It is PAYLOAD-FREE
    # by construction — there is nothing to count and nothing shape-only to say —
    # so what has to be true is that the `loop_` prefix filter admits it and it
    # arrives as a correctly-named span rather than being dropped on the floor.
    # Asserted through the real observer for the same reason the three keys above
    # are: emitting an event and publishing it are different things.
    observe("loop_answer_table_empty_designation", {})
    assert len(captured) == 2, "the payload-free event never opened a span"
    # The GUARDRAIL kind and nothing else: a correctly-classified span carrying no
    # payload, which is the whole intent — the same shape `loop_answer_shape_
    # exhausted` has.
    assert captured[-1] == {"openinference.span.kind": "GUARDRAIL"}


# ---------------------------------------------------------------------------
# Intent coverage — a CHECK, never a source (08 §B.1)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# The resume seed
# ---------------------------------------------------------------------------


async def test_a_paused_turn_resumes_with_the_whole_designated_set() -> None:
    """08 §M: the resume seed reconstructs the whole list, not the first element —
    otherwise a three-part answer that paused silently degrades to one table in
    exactly the turns multi-table exists for."""
    store = InMemorySessionStore()
    await store.get_or_create_session(SESSION_ID)
    await store.append_trail_entry(
        SESSION_ID,
        TrailEntry(
            turn_index=0,
            tool_call_id="a1",
            tool_name=ANSWER,
            args={"answer": "x", "tables": [{"sql": HEADCOUNT_SQL}, {"sql": SALARY_SQL}]},
            status="ok",
            error_code=None,
            provenance=frozenset(),
            result_preview=None,
            result_full_ref=None,
            ts="t",
        ),
    )
    loop = AgentLoop(
        model_client=ScriptedModelClient([final_answer(assistant_text="x")]),
        tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        runtime_tools={ANSWER: AnswerWithTableTool()},
    )
    tables, _runs = await loop._compute_turn_answer_tables(SESSION_ID, 0)
    assert [t.sql for t in tables] == [HEADCOUNT_SQL, SALARY_SQL]
    # The PRIMARY table is the first one — the projection the envelope applies.
    assert tables[0].sql == HEADCOUNT_SQL


# ---------------------------------------------------------------------------
# The pre-08-§O read path — a session document written before the slim-down
# ---------------------------------------------------------------------------


async def test_a_pre_slim_down_document_still_replays_and_still_pages() -> None:
    """08 §O's ONE hard compatibility requirement, asserted end to end.

    The model-facing schema no longer declares a top-level `sql`/`blueprint_id`
    pair, but every `answerWithTable` trail entry ever persisted before that change
    carries exactly that shape and no `tables` key at all. Those entries are
    SUCCESSFUL, and a successful `answerWithTable` entry is not a fire-and-forget
    record: it replays cross-turn into model context, it seeds a resumed window, and
    it rebuilds the transcript a reloaded browser renders. There is no migration and
    no expiry, so the read path has to understand the old shape forever.

    A read path that understood only `tables` would fail SILENTLY in all three
    places at once — the grid vanishes from the reload, the resume comes back with
    `answer_sql=None`, and nothing anywhere reports a problem. That is the same
    class of defect `resolve_designation` was extracted to prevent, and *reload is
    the only place that regression shows*.

    FOUR CONSUMERS, one document, all four asserted:
      1. `project_history` — the reloaded transcript's `answer_sql`/`answer_tables`.
      2. `_compute_turn_answer_tables` — the resume seed.
      3. `ContextAssembler` — cross-turn replay reaching the model as a real result
         rather than the D94 withheld sentinel.
      4. `build_page_sql` — the reconstructed SQL is genuinely pageable, which is
         what "the user can still scroll the grid" actually means. Reconstructing a
         string nothing can page would satisfy 1-3 and still lose the table.
    """
    store = InMemorySessionStore()
    await store.get_or_create_session(SESSION_ID)
    await store.append_message(
        SESSION_ID,
        TurnMessage(turn_index=0, role="user", content="q?", ts="t0", provenance=frozenset()),
    )
    await store.append_message(
        SESSION_ID,
        TurnMessage(
            turn_index=0,
            role="assistant",
            content="By department.",
            ts="t3",
            provenance=frozenset({(_E, "department_name")}),
        ),
    )
    # THE OLD SHAPE, verbatim: top-level designation, no `tables` key.
    legacy_args = {"answer": "By department.", "sql": HEADCOUNT_SQL}
    tool_result = await AnswerWithTableTool().run(legacy_args, _creds())
    await store.append_trail_entry(
        SESSION_ID,
        TrailEntry(
            turn_index=0,
            tool_call_id="a1",
            tool_name=ANSWER,
            args=legacy_args,
            status="ok",
            error_code=None,
            provenance=tool_result.provenance,
            result_preview=tool_result.result_preview,
            result_full_ref=None,
            ts="t2",
        ),
    )
    doc = await store.get_or_create_session(SESSION_ID)

    # 1. The reloaded transcript.
    body = project_history(doc.messages, doc.tool_trail, frozenset(), None)
    turn = body["turns"][0]
    assert turn["answer_sql"] == HEADCOUNT_SQL
    assert [t["sql"] for t in turn["answer_tables"]] == [HEADCOUNT_SQL]

    # 2. The resume seed.
    loop = AgentLoop(
        model_client=ScriptedModelClient([final_answer(assistant_text="x")]),
        tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        runtime_tools={ANSWER: AnswerWithTableTool()},
    )
    seeded, _runs = await loop._compute_turn_answer_tables(SESSION_ID, 0)
    assert [t.sql for t in seeded] == [HEADCOUNT_SQL]

    # 3. Cross-turn replay — read on turn 1, the turn AFTER the one that wrote it.
    assembled = await ContextAssembler(
        session_store=store,
        base_system_prompt="BASE",
        preview_row_count=20,
    ).assemble(SESSION_ID, frozenset(), current_turn_index=1)
    replayed = [m for m in assembled.messages if m.get("tool_name") == ANSWER]
    assert len(replayed) == 1
    assert "result withheld" not in json.dumps(replayed[0])




@pytest.mark.parametrize(
    "invalid_ref",
    ["syn_databases", "prepared_widget", "bp-active", "missing", "old", "denied", "hidden"],
)
async def test_result_table_repair_reuses_scoped_execution(invalid_ref: str) -> None:
    loop, store, _ = _build([])
    for ref, name, turn, status, provenance in [
        ("syn_databases", "listDatabases", 1, "ok", frozenset()),
        ("prepared_widget", "get_hr_head_count", 1, "ok", frozenset()),
        ("old", "runQuery", 0, "ok", frozenset()),
        ("denied", "runQuery", 1, "error", frozenset()),
        ("hidden", "runQuery", 1, "ok", frozenset({(_P, "annual_salary")})),
        ("headcount_result", "runBlueprint", 1, "ok", frozenset({(_E, "department_name")})),
    ]:
        full_ref = None
        if name == "runBlueprint":
            full_ref = await store.write_full_result(
                SESSION_ID,
                ref,
                {
                    "blueprint_id": "bp-active",
                    "terminal_sql": HEADCOUNT_SQL,
                    "status": "verified",
                    "verify": {"grain_checked": True},
                },
            )
        await store.append_trail_entry(
            SESSION_ID,
            TrailEntry(
                turn_index=turn,
                tool_call_id=ref,
                tool_name=name,
                args={"sql": HEADCOUNT_SQL} if name == "runQuery" else {},
                status=status,
                error_code=None,
                provenance=provenance,
                result_preview=None,
                result_full_ref=full_ref,
                ts="t",
                authoritative=name == "runBlueprint",
            ),
        )
    kwargs = dict(
        blueprint_runs={"bp-active": BlueprintRun(terminal_sql=HEADCOUNT_SQL)},
        credentials=_creds(frozenset({f"{_E}.department_name"})),
        session_id=SESSION_ID,
        turn_index=1,
    )
    tables, refusal, carried = await loop._resolve_answer_tables(
        {"tables": [{"result_id": invalid_ref}]},
        **kwargs,
    )
    assert tables == [] and carried
    assert refusal.error_code == "ANSWER_TABLE_RESULT_INVALID"
    assert 'Available table result IDs: ["headcount_result"]' in refusal.denial_detail
    assert "do not rerun completed analysis" in refusal.denial_detail
    tables, refusal, carried = await loop._resolve_answer_tables(
        {"tables": [{"result_id": "headcount_result", "caption": "Active employees"}]},
        **kwargs,
    )
    assert refusal is None and carried
    assert len(tables) == 1
    assert tables[0].sql == HEADCOUNT_SQL


async def test_invalid_result_reference_returns_presentation_repair_in_live_loop() -> None:
    loop, store, _ = _build(
        [
            final_answer(
                assistant_text="See the breakdown.", tables=[{"result_id": "syn_databases"}]
            ),
            final_answer(assistant_text="I don't have any information to answer your question."),
        ]
    )
    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q?")
    refused = [
        e
        for e in await store.load_trail(SESSION_ID)
        if e.error_code == "ANSWER_TABLE_RESULT_INVALID"
    ]
    assert len(refused) == 1
    assert "syn_databases" in refused[0].denial_detail
    assert "presentation repair" in refused[0].denial_detail
