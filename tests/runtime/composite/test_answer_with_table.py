"""Backend tests for the `answerWithTable` runtime tool + the `answer_sql` result
field (the replacement for `result_table`).

Covers: `clean_answer_sql` normalization; the tool's `ToolResult` shape and
no-raise-on-malformed-args contract; the TERMINAL contract (a successful call ends
the turn, and its `answer` becomes the persisted assistant message); designation by
raw `sql` and by `blueprint_id`; last-designation-wins; the dormant
`hooks/answer_table.py` seams; and that a successful call reaches the model as a
real confirmation rather than the D94 withheld sentinel.
"""

from __future__ import annotations

import json

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.answer_with_table import (
    AnswerWithTableTool,
    clean_answer_sql,
)
from data_agent.runtime.composite.record_assumptions import RecordAssumptionsTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.hooks.answer_table import AnswerTableEvent, AnswerTableHooks
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import TrailEntry, TurnMessage

SESSION_ID = "sess-answer-with-table"
CATALOG = CatalogHandle({"db.t": {"c": "String"}})
_ANSWER_SQL = "SELECT department, count() AS n FROM dbpcm_warehouse.employee GROUP BY department"


def _creds(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="SECRET", column_scope=scope)


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return [{"type": "function", "name": "listDatabases", "description": "", "parameters": {}}]


# --- clean_answer_sql ------------------------------------------------------


def test_clean_answer_sql_strips_and_keeps() -> None:
    assert clean_answer_sql("  SELECT 1  ") == "SELECT 1"


def test_clean_answer_sql_rejects_non_strings_and_blanks() -> None:
    # Total on any shape — it is called on unvalidated model output.
    for raw in (None, 42, True, ["SELECT 1"], {"sql": "SELECT 1"}, "", "   ", "\t\n"):
        assert clean_answer_sql(raw) is None


def test_clean_answer_sql_truncates_absurd_length() -> None:
    assert len(clean_answer_sql("S" * 50_000)) == 20_000


# --- the tool itself -------------------------------------------------------


async def test_tool_returns_ok_confirmation_without_echoing_rows() -> None:
    result = await AnswerWithTableTool().run(
        {"answer": "Headcount by department.", "sql": _ANSWER_SQL}, _creds()
    )
    assert result.status == "ok"
    assert result.tool_name == "answerWithTable"
    assert result.error_code is None
    # DETERMINED-EMPTY: this tool reads no warehouse data. `None` would mean
    # UNDETERMINED and would hand the model the D94 withheld sentinel on every
    # successful call (the bug fixed for recordAssumptions).
    assert result.provenance == frozenset()
    assert result.result_full is None
    # The confirmation carries a flag, NOT the table — echoing rows back here would
    # re-create the very behaviour this tool exists to stop.
    assert result.result_preview.preview_rows == [[True, True]]
    assert "department" not in json.dumps(result.result_preview.to_doc())


async def test_tool_never_raises_on_malformed_args() -> None:
    for args in ({}, {"sql": None}, {"sql": 42}, {"sql": "   "}, {"wrong": "key"}, None, 7):
        result = await AnswerWithTableTool().run(args, _creds())  # type: ignore[arg-type]
        assert result.status == "ok"
        # Neither answered nor designated — the flags report that honestly.
        assert result.result_preview.preview_rows == [[False, False]]


async def test_confirmation_flags_report_answer_and_designation_separately() -> None:
    """The two flags are independent: prose without a table is a real (if unusual)
    call, and the confirmation must not claim a designation that did not happen."""
    tool = AnswerWithTableTool()
    prose_only = await tool.run({"answer": "There are 3."}, _creds())
    assert prose_only.result_preview.preview_rows == [[True, False]]
    by_blueprint = await tool.run({"answer": "See table.", "blueprint_id": "bp-x"}, _creds())
    assert by_blueprint.result_preview.preview_rows == [[True, True]]


async def test_successful_call_reaches_the_model_as_its_real_confirmation() -> None:
    """Same regression the recordAssumptions fix closed: an `ok` entry with
    determined-empty provenance must render as itself, not the withheld sentinel."""
    store = InMemorySessionStore()
    await store.get_or_create_session(SESSION_ID)
    await store.append_message(
        SESSION_ID,
        TurnMessage(turn_index=0, role="user", content="by dept?", ts="t0", provenance=frozenset()),
    )
    result = await AnswerWithTableTool().run({"sql": _ANSWER_SQL}, _creds())
    await store.append_trail_entry(
        SESSION_ID,
        TrailEntry(
            turn_index=0, tool_call_id="pt1", tool_name="answerWithTable",
            args={"sql": _ANSWER_SQL}, status=result.status, error_code=result.error_code,
            provenance=result.provenance, result_preview=result.result_preview,
            result_full_ref=None, ts="t1",
        ),
    )
    assembled = await ContextAssembler(
        session_store=store, base_system_prompt="BASE",
        preview_row_count=20, history_token_budget=100_000,
    ).assemble(SESSION_ID, frozenset(), current_turn_index=0)

    rendered = [m for m in assembled.messages if m.get("tool_name") == "answerWithTable"]
    assert len(rendered) == 1
    assert not rendered[0].get("withheld_sentinel")
    assert "result withheld" not in json.dumps(rendered[0])


# --- the TERMINAL contract + loop accumulation -----------------------------


def _build_loop(
    model: ScriptedModelClient, *, hooks: AnswerTableHooks | None = None
) -> tuple[AgentLoop, InMemorySessionStore]:
    store = InMemorySessionStore()
    return (
        AgentLoop(
            model_client=model,
            tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG),
            context_assembler=ContextAssembler(store, history_token_budget=100_000),
            session_store=store,
            tools_provider=_tools_provider,
            max_loop_iterations=15,
            max_wall_clock_seconds=60,
            max_budget_windows=3,
            runtime_tools={
                "answerWithTable": AnswerWithTableTool(),
                "recordAssumptions": RecordAssumptionsTool(),
            },
            answer_table_hooks=hooks,
        ),
        store,
    )


def _loop_over(store: InMemorySessionStore) -> AgentLoop:
    """An AgentLoop bound to an EXISTING store, for exercising the trail-reseed
    helpers directly against a hand-built trail."""
    return AgentLoop(
        model_client=ScriptedModelClient([ModelTurnResult(assistant_text="x")]),
        tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG),
        context_assembler=ContextAssembler(store, history_token_budget=100_000),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        runtime_tools={"answerWithTable": AnswerWithTableTool()},
    )


def _answer(call_id: str, **args: object) -> ModelTurnResult:
    return ModelTurnResult(
        tool_calls=[ToolCallRequest(id=call_id, name="answerWithTable", arguments=dict(args))]
    )


async def test_a_successful_call_ends_the_turn() -> None:
    """THE POINT of making it terminal. The model used to designate, receive a
    trivial confirmation, and then re-send the ENTIRE conversation purely to emit a
    sentence it could already have written — a round-trip that bought nothing.

    The second scripted result below must never be consumed: exactly ONE model call.
    """
    model = ScriptedModelClient(
        [
            _answer("a1", answer="Three departments.", sql=_ANSWER_SQL),
            ModelTurnResult(assistant_text="THIS MUST NOT BE REACHED."),
        ]
    )
    loop, _ = _build_loop(model)

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="by dept?")

    assert len(model.calls) == 1, "the designation must end the turn, not cost another round-trip"
    assert outcome.status == "done"
    assert outcome.assistant_text == "Three departments."
    assert outcome.answer_sql == _ANSWER_SQL


async def test_the_terminal_answer_is_persisted_like_an_ordinary_one() -> None:
    """`assistant_text` now originates in tool ARGUMENTS rather than a model
    message, but replay / session_history / the D44 scope gate all read the
    persisted assistant `TurnMessage`. If it were not written identically, history
    would silently disagree with the live answer for exactly the table turns."""
    loop, store = _build_loop(
        ScriptedModelClient([_answer("a1", answer="Three departments.", sql=_ANSWER_SQL)])
    )
    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="by dept?")

    doc = await store.get_or_create_session(SESSION_ID)
    assistants = [m for m in doc.messages if m.role == "assistant"]
    assert [m.content for m in assistants] == ["Three departments."]
    # Tagged with the turn's provenance union, exactly like the no-tool-calls exit.
    assert assistants[0].provenance is not None


async def test_a_scalar_answer_still_ends_the_old_way() -> None:
    """The safe default is untouched: a model that never calls the tool cannot hang
    — it falls through to the no-tool-calls exit with no table."""
    loop, _ = _build_loop(ScriptedModelClient([ModelTurnResult(assistant_text="There are 5.")]))
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="how many?")
    assert outcome.status == "done"
    assert outcome.assistant_text == "There are 5."
    assert outcome.answer_sql is None


async def test_a_call_without_answer_text_does_not_end_the_turn() -> None:
    """`answer` is REQUIRED by the schema, but a model can still omit it. Ending the
    turn on a text-less call would deliver an EMPTY answer — a total, silent
    failure — so the loop keeps going and the ordinary exit supplies the prose."""
    model = ScriptedModelClient(
        [_answer("a1", sql=_ANSWER_SQL), ModelTurnResult(assistant_text="Recovered prose.")]
    )
    loop, _ = _build_loop(model)

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q?")

    assert len(model.calls) == 2
    assert outcome.assistant_text == "Recovered prose."
    assert outcome.answer_sql == _ANSWER_SQL  # the designation still counted


async def test_batched_assumptions_and_answer_both_land() -> None:
    """A model may batch recordAssumptions with the terminal call. The terminal exit
    fires only AFTER the whole batch drains, so the assumptions are not lost."""
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="r1", name="recordAssumptions",
                        arguments={"assumptions": ["'Active' meant currently employed."]},
                    ),
                    ToolCallRequest(
                        id="a1", name="answerWithTable",
                        arguments={"answer": "See the table.", "sql": _ANSWER_SQL},
                    ),
                ]
            )
        ]
    )
    loop, _ = _build_loop(model)

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q?")

    assert outcome.assumptions == ["'Active' meant currently employed."]
    assert outcome.answer_sql == _ANSWER_SQL
    assert outcome.assistant_text == "See the table."


async def test_last_designation_wins() -> None:
    second = "SELECT month, count() FROM dbpcm_warehouse.employee GROUP BY month"
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="a1", name="answerWithTable",
                                    arguments={"answer": "one", "sql": _ANSWER_SQL}),
                    ToolCallRequest(id="a2", name="answerWithTable",
                                    arguments={"answer": "two", "sql": second}),
                ]
            )
        ]
    )
    loop, _ = _build_loop(model)
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q?")
    assert outcome.answer_sql == second


# --- blueprint_id designation ----------------------------------------------


async def test_an_unrun_blueprint_id_nudges_instead_of_ending_the_turn() -> None:
    """A blueprint that did not run this turn has no terminal SQL to resolve to.
    Silently shipping prose with no table would leave the model none the wiser, so
    the call becomes a RETRYABLE nudge and the turn continues — the fix is one
    runBlueprint away and the model can make it in the same turn.

    (Re-running the blueprint HERE to find out is not the answer: it would decouple
    the paged table from the D56 verification that gated the answer the user sees.)
    """
    model = ScriptedModelClient(
        [
            _answer("a1", answer="See table.", blueprint_id="bp-never-ran"),
            ModelTurnResult(assistant_text="Recovered without a table."),
        ]
    )
    loop, store = _build_loop(model)

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q?")

    # It did NOT terminate on the designation — the model got another round-trip.
    assert len(model.calls) == 2
    assert outcome.assistant_text == "Recovered without a table."
    assert outcome.answer_sql is None

    entry = [e for e in await store.load_trail(SESSION_ID) if e.tool_name == "answerWithTable"][0]
    assert entry.status == "error"
    assert entry.error_code == "ANSWER_TABLE_BLUEPRINT_NOT_RUN"


async def test_the_nudge_is_visible_to_the_model_on_the_next_round_trip() -> None:
    """The instruction has to SURVIVE the rebuild. `user_message` is not persisted on
    TrailEntry, so `_render_entry` re-derives it from `error_code` — an unregistered
    code would degrade to the generic "Something went wrong processing that request."
    and the model would be told nothing actionable. Asserted on the RENDERED context,
    not on the ToolResult, because that is what the model actually reads."""
    model = ScriptedModelClient(
        [
            _answer("a1", answer="See table.", blueprint_id="bp-never-ran"),
            ModelTurnResult(assistant_text="ok"),
        ]
    )
    loop, _ = _build_loop(model)
    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q?")

    rendered = [
        m for m in model.calls[1].messages
        if m.get("role") == "tool" and "runBlueprint" in str(m.get("content", ""))
    ]
    assert rendered, "the nudge must reach the model's next round-trip"
    assert "run" in str(rendered[0]["content"]).lower()


async def test_a_raw_sql_designation_is_never_nudged() -> None:
    """The nudge is only for an unresolvable BLUEPRINT reference. A raw-SQL answer
    has nothing to look up, so it must terminate normally."""
    model = ScriptedModelClient([_answer("a1", answer="Done.", sql=_ANSWER_SQL)])
    loop, _ = _build_loop(model)
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q?")
    assert len(model.calls) == 1
    assert outcome.answer_sql == _ANSWER_SQL


async def test_raw_sql_wins_when_both_are_given() -> None:
    loop, _ = _build_loop(
        ScriptedModelClient(
            [_answer("a1", answer="x", sql=_ANSWER_SQL, blueprint_id="bp-never-ran")]
        )
    )
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q?")
    assert outcome.answer_sql == _ANSWER_SQL


# --- the dormant hook seams -------------------------------------------------


async def test_hooks_are_dormant_by_default() -> None:
    assert AnswerTableHooks().is_dormant


async def test_an_unresolved_hook_can_supply_a_replacement() -> None:
    seen: list[AnswerTableEvent] = []

    def _hook(event: AnswerTableEvent) -> str:
        seen.append(event)
        return "SELECT 1 AS recovered"

    hooks = AnswerTableHooks()
    hooks.register_unresolved(_hook)
    loop, _ = _build_loop(
        ScriptedModelClient([_answer("a1", answer="x", blueprint_id="bp-never-ran")]), hooks=hooks
    )

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q?")

    assert outcome.answer_sql == "SELECT 1 AS recovered"
    assert [e.blueprint_id for e in seen] == ["bp-never-ran"]
    # D5: a hook is never handed the raw session id or any credential.
    assert seen[0].session_id_hash != SESSION_ID
    assert not hasattr(seen[0], "jwt")


async def test_an_ephemeral_hook_fires_for_a_scratch_backed_table() -> None:
    """A scratch-backed designation works NOW and dies at the TTL. The seam exists
    so a durable replacement can be swapped in without reopening the loop."""
    scratch_sql = "SELECT * FROM scratch.s_abc_bp_0001"
    seen: list[AnswerTableEvent] = []

    def _hook(event: AnswerTableEvent) -> str:
        seen.append(event)
        return "SELECT * FROM durable.answer_1"

    hooks = AnswerTableHooks()
    hooks.register_ephemeral(_hook)
    loop, _ = _build_loop(
        ScriptedModelClient([_answer("a1", answer="x", sql=scratch_sql)]), hooks=hooks
    )

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q?")

    assert outcome.answer_sql == "SELECT * FROM durable.answer_1"
    assert [e.sql for e in seen] == [scratch_sql]


async def test_an_ephemeral_hook_does_not_fire_for_an_ordinary_table() -> None:
    calls: list[AnswerTableEvent] = []
    hooks = AnswerTableHooks()
    hooks.register_ephemeral(lambda e: calls.append(e) or None)  # type: ignore[func-returns-value]
    loop, _ = _build_loop(
        ScriptedModelClient([_answer("a1", answer="x", sql=_ANSWER_SQL)]), hooks=hooks
    )
    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q?")
    assert calls == []


async def test_a_raising_hook_never_breaks_the_turn() -> None:
    """Degrade-not-fail: an extension point must not be able to fail a user's turn."""
    hooks = AnswerTableHooks()
    hooks.register_ephemeral(_boom)
    loop, _ = _build_loop(
        ScriptedModelClient([_answer("a1", answer="x", sql="SELECT * FROM scratch.t")]),
        hooks=hooks,
    )
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q?")
    assert outcome.status == "done"
    assert outcome.answer_sql == "SELECT * FROM scratch.t"  # unchanged, not lost


def _boom(event: AnswerTableEvent) -> str:
    raise RuntimeError("hook exploded")


# --- blueprint_id resolves to the blueprint's TERMINAL sql ------------------


class _StubBlueprintTool:
    """A `runBlueprint` stand-in returning the `result_full` shape the real
    executor produces — `terminal_sql` being the key under test."""

    tool_name = "runBlueprint"

    def __init__(self, blueprint_id: str, terminal_sql: str) -> None:
        self._result_full = {
            "blueprint_id": blueprint_id,
            "status": "verified",
            # `sql` is the full transparency list and is NOT ordered by terminality
            # (rehydrated nodes land first on a D45 resume) — resolution must read
            # `terminal_sql`, never "the last element of sql".
            "sql": ["SELECT 1 /* an earlier node */", terminal_sql],
            "terminal_sql": terminal_sql,
            "columns": ["month"],
            "row_count": 6,
            "truncated": False,
            "preview_rows": [["2026-01"]],
            "verify": {"grain_checked": True},
        }

    async def run(self, arguments: dict, credentials: RuntimeCredentials):
        from data_agent.runtime.dispatch.tool_dispatcher import ToolResult
        from data_agent.runtime.session.models import ResultPreview

        return ToolResult(
            status="ok", tool_name="runBlueprint", error_code=None, retryable=None,
            user_message=None, provenance=frozenset({("db.t", "c")}),
            result_preview=ResultPreview(
                columns=["month"], row_count=6, truncated=False, preview_rows=[["2026-01"]]
            ),
            result_full=self._result_full,
        )


async def test_a_blueprint_id_resolves_to_that_blueprints_terminal_sql() -> None:
    """The positive `blueprint_id` case: the model names the blueprint it ran and
    the runtime resolves it to a concrete pageable query, so the model never has to
    copy rendered SQL it may only partly see — and the UI still receives ONE field.
    """
    terminal = "SELECT month, hires FROM dbpcm_warehouse.employee_hires_by_month"
    store = InMemorySessionStore()
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="b1", name="runBlueprint",
                        arguments={"id": "bp-hires-per-month", "slot_bindings": {"window_months": 6}},
                    )
                ]
            ),
            _answer("a1", answer="Six months of hires.", blueprint_id="bp-hires-per-month"),
        ]
    )
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG),
        context_assembler=ContextAssembler(store, history_token_budget=100_000),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        runtime_tools={
            "answerWithTable": AnswerWithTableTool(),
            "runBlueprint": _StubBlueprintTool("bp-hires-per-month", terminal),
        },
    )

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="hires?")

    assert outcome.answer_sql == terminal
    assert outcome.assistant_text == "Six months of hires."
    # The transparency list still records what RAN, distinctly from what the answer IS.
    assert outcome.sql_executed == ["SELECT 1 /* an earlier node */", terminal]


async def test_a_blueprint_designation_survives_a_pause_and_resume() -> None:
    """REGRESSION. `_compute_turn_answer_sql` reseeds a resumed window from the trail,
    and it originally read only `args["sql"]` — silently dropping every BLUEPRINT
    designation. That is the form the live model actually emits: observed in a real
    turn it sent `sql=""` next to `blueprint_id`, which cleans to `None`. So a
    blueprint-answered turn that paused came back with `answer_sql=None` and the user
    lost the table, in exactly the case the blueprint path exists for.

    Reconstructing it requires de-referencing the blueprint's `result_full` (D46 KV
    pointer) to reach `terminal_sql` — the in-window path reads that straight off the
    dispatch result and never pays the cost.
    """
    terminal = "SELECT month, hires FROM dbpcm_warehouse.employee_hires_by_month"
    store = InMemorySessionStore()
    await store.get_or_create_session(SESSION_ID)

    # The blueprint ran: its result_full lives behind a KV ref, as in production.
    ref = await store.write_full_result(
        SESSION_ID, "r1",
        {"blueprint_id": "bp-hires-per-month", "status": "verified",
         "sql": ["SELECT 1 /* earlier node */", terminal], "terminal_sql": terminal},
    )
    await store.append_trail_entry(SESSION_ID, TrailEntry(
        turn_index=0, tool_call_id="b1", tool_name="runBlueprint",
        args={"id": "bp-hires-per-month", "slot_bindings": {}}, status="ok",
        error_code=None, provenance=frozenset({("db.t", "c")}), result_preview=None,
        result_full_ref=ref, ts="t1",
    ))
    # …and the model designated it the ANSWER using the exact live shape: empty
    # `sql` beside `blueprint_id`.
    await store.append_trail_entry(SESSION_ID, TrailEntry(
        turn_index=0, tool_call_id="a1", tool_name="answerWithTable",
        args={"answer": "Six months of hires.", "sql": "", "blueprint_id": "bp-hires-per-month"},
        status="ok", error_code=None, provenance=frozenset(), result_preview=None,
        result_full_ref=None, ts="t2",
    ))

    loop = _loop_over(store)
    assert await loop._compute_turn_answer_sql(SESSION_ID, 0) == terminal


async def test_a_raw_sql_designation_still_reseeds() -> None:
    """The `sql=` form must keep working — the blueprint branch is additive."""
    store = InMemorySessionStore()
    await store.get_or_create_session(SESSION_ID)
    await store.append_trail_entry(SESSION_ID, TrailEntry(
        turn_index=0, tool_call_id="a1", tool_name="answerWithTable",
        args={"answer": "x", "sql": _ANSWER_SQL}, status="ok", error_code=None,
        provenance=frozenset(), result_preview=None, result_full_ref=None, ts="t",
    ))
    loop = _loop_over(store)
    assert await loop._compute_turn_answer_sql(SESSION_ID, 0) == _ANSWER_SQL
