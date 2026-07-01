"""Adversarial D25 PII-redaction coverage: the FULL `ToolDispatcher`/`AgentLoop`
observer path with real PII-laden SQL + result rows (QA hardening pass).

`tests/runtime/observability/test_redaction.py` already proves `mask_sql` /
`hash_scope` / `redact_tool_args` in isolation (pure-function level).
`tests/runtime/observability/test_progress.py` already proves
`to_progress_event`'s allowlist behavior against a hand-built payload. This
file proves the property those two suites individually assume but don't
wire together: when a REAL `ToolDispatcher.dispatch(...)` call carries a SQL
string with string+numeric PII literals and the (fake) MCP returns actual
result rows containing PII, the observer callback the dispatcher/loop
actually invokes — the same one `ProgressEmitter`/tracing wire into —
NEVER receives the raw SQL, the raw result rows/cell values, the JWT, or the
raw column_scope, anywhere, at any stage boundary. This is checked by
scanning the raw `(event, payload)` calls the dispatcher/loop *actually
produce* (its real observer contract), not a hand-constructed payload.
"""

from __future__ import annotations

import json

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.observability.progress import ProgressEmitter, combine_observers
from data_agent.runtime.observability.redaction import mask_sql
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Salary": "Decimal(18,2)"}})

SESSION_ID = "sess-redaction-e2e"
JWT = "jwt-secret-value-should-never-appear"
PII_NAME = "Jane Doe"
PII_SALARY = "128000"
PII_SQL = f"SELECT EmployeeCode FROM employee WHERE Name = '{PII_NAME}' AND Salary > {PII_SALARY}"
PII_SCOPE_COLUMN = "dbpcm_warehouse.employee.Salary"

TOOLS_SCHEMA = [
    {"type": "function", "name": "runQuery", "description": "", "parameters": {}},
]


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return list(TOOLS_SCHEMA)


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(
        session_id=SESSION_ID, jwt=JWT, column_scope=frozenset({PII_SCOPE_COLUMN})
    )


def _blob_of(events: list[tuple[str, dict]]) -> str:
    return json.dumps(events, default=str)


def _assert_no_pii(blob: str) -> None:
    assert PII_NAME not in blob, f"PII name leaked into observer payload: {blob!r}"
    assert PII_SALARY not in blob, f"PII salary literal leaked into observer payload: {blob!r}"
    assert JWT not in blob, f"JWT leaked into observer payload: {blob!r}"
    assert "E1" not in blob, f"result row cell value leaked into observer payload: {blob!r}"
    assert "employee_row_value_E1" not in blob


async def test_dispatcher_observer_never_receives_pii_sql_or_result_rows() -> None:
    events: list[tuple[str, dict]] = []

    def observer(event: str, payload: dict) -> None:
        events.append((event, dict(payload)))

    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                {
                    "columns": ["EmployeeCode", "Name", "Salary"],
                    "rows": [["E1", PII_NAME, PII_SALARY], ["employee_row_value_E1", PII_NAME, PII_SALARY]],
                    "row_count": 2,
                    "truncated": False,
                }
            ]
        }
    )
    dispatcher = ToolDispatcher(mcp, CATALOG, observer=observer)

    result = await dispatcher.dispatch("runQuery", {"sql": PII_SQL}, _credentials())
    assert result.status == "ok"
    # Sanity: the PII genuinely reached the ToolResult (the dispatcher is not
    # simply dropping it) — the point is the *observer* channel is clean, not
    # that the whole pipeline is PII-free (the UI legitimately shows raw
    # rows/SQL per 08-ui.md's transparency principle; only telemetry is
    # scrubbed).
    assert PII_NAME in json.dumps(result.result_full, default=str)

    blob = _blob_of(events)
    _assert_no_pii(blob)
    # The dispatcher's own observer contract carries only tool_name/error_code
    # (see tool_dispatcher.py) — args/results are never forwarded to it at all.
    for _event, payload in events:
        assert set(payload.keys()) <= {"tool_name", "error_code"}


async def test_progress_emitter_wired_into_a_real_turn_never_carries_pii() -> None:
    """End-to-end: a full `AgentLoop` turn with PII-laden SQL/results, wired
    through `ProgressEmitter` exactly as `app.py` would — the UI-facing
    progress stream must never carry PII, SQL text, the JWT, or the raw
    column_scope, anywhere in any emitted event's `step`/`shape`."""
    emitter = ProgressEmitter()
    seen_tracing_events: list[tuple[str, dict]] = []

    def tracing_observer(event: str, payload: dict) -> None:
        seen_tracing_events.append((event, dict(payload)))

    observer = combine_observers(emitter.observe, tracing_observer)

    store = InMemorySessionStore()
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                {
                    "columns": ["EmployeeCode", "Name"],
                    "rows": [["E1", PII_NAME]],
                    "row_count": 1,
                    "truncated": False,
                }
            ]
        }
    )
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[ToolCallRequest(id="call_1", name="runQuery", arguments={"sql": PII_SQL})]
            ),
            ModelTurnResult(assistant_text="Here is Jane's record."),
        ]
    )
    dispatcher = ToolDispatcher(mcp, CATALOG, observer=observer)
    assembler = ContextAssembler(store, history_token_budget=100_000)
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=observer,
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Find Jane Doe's record."
    )
    assert outcome.status == "done"
    emitter.close()

    progress_events = [e async for e in emitter.stream()]
    progress_blob = json.dumps([{"step": e.step, "shape": e.shape} for e in progress_events], default=str)
    _assert_no_pii(progress_blob)
    assert PII_SCOPE_COLUMN not in progress_blob

    tracing_blob = _blob_of(seen_tracing_events)
    _assert_no_pii(tracing_blob)
    assert PII_SCOPE_COLUMN not in tracing_blob

    # Sanity: real progress events were actually produced (not a vacuous
    # "empty stream trivially has no PII" pass).
    assert len(progress_events) >= 3
    assert any(e.step.startswith("running") for e in progress_events)


async def test_mask_sql_applied_before_any_span_write_strips_the_exact_pii_used_here() -> None:
    """A tight, non-redundant check that the specific PII literals used in
    this file's SQL are the ones `mask_sql` is responsible for stripping —
    ties the e2e assertions above back to the unit-level masking contract."""
    masked = mask_sql(PII_SQL)
    assert PII_NAME not in masked
    assert PII_SALARY not in masked
