"""The agent loop hands the turn's QUESTION to the schema fit (ISSUES C5).

`dispatch/schema_preview.py` can only spend its remaining detail budget on the
columns the user is actually asking about if something tells it what was asked.
`_run_loop_body` is the ONE place that knows: it holds the raw user text, and its
`_tool_dispatcher.dispatch(...)` call is the only dispatch site that passes
`question=`. This test drives the REAL AgentLoop / ContextAssembler /
ToolDispatcher / schema-fit stack (only the model and the MCP are doubles) over a
schema too wide to show in full, and asserts on the CANONICAL MESSAGE the model
receives on the following round-trip:

  * every column is NAMED, including the salary column at the very end of the
    list, which the old head-cut dropped outright;
  * the salary column's DOCUMENTATION survived, because the question named it,
    and it is emitted in the DETAILED GROUP AT THE FRONT of the list (C5b);
  * a filler column the question says nothing about did NOT keep its
    documentation at the same budget — i.e. the ordering is what made the
    difference, not a bigger budget;
  * the table-level sections ride complete beside it (C5b: they are outside the
    columns budget entirely);
  * the raw question text is nowhere in the message (D25).

C5b: the budget this exercises is `schema_columns_token_budget` (the COLUMNS
SECTION alone), not the generic `max_tool_result_tokens`. It is set explicitly
below so the contrast between the two runs is a property of the ORDERING and not
of whichever default happens to ship.
"""

from __future__ import annotations

import json
from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore

_E = "dbpcm_warehouse.employee"

CATALOG = CatalogHandle({_E: {"employee_code": "String", "annual_salary": "Float64"}})

SESSION_ID = "sess-wide-schema-question"
JWT = "jwt-not-under-test"
NONCE = "zqx-nonce-42"
QUESTION = f"what is the average annual salary by department, {NONCE}?"

# 300 filler columns, then the two the question is about — LAST, where no
# head-cut could ever have reached them.
SALARY_DESCRIPTION = (
    "This is the annual salary of the employee. For a MEDIAN of this column use "
    "(quantileExactLow(0.5)(annual_salary) + quantileExactHigh(0.5)(annual_salary)) / 2."
)
WIDE_SCHEMA: dict[str, Any] = {
    "database": "dbpcm_warehouse",
    "table": "employee",
    "catalogued": True,
    # Table-level sections: never budgeted, so they must arrive whole (C5b).
    "primary_key": ["employee_code"],
    "rules": ["Exclude employees who never started: hire_date IS NOT NULL."],
    "ambiguities": ["'headcount' may mean active or all employees."],
    "columns": [
        {
            "name": f"filler_{i}",
            "type": "String",
            "comment": "",
            "description": f"An unrelated attribute of the employee record, number {i}.",
            "synonyms": None,
            "unit": None,
            "values": None,
            "observed_values": None,
            "client_defined": False,
            "sensitive": False,
        }
        for i in range(300)
    ]
    + [
        {
            "name": "department_name",
            "type": "Nullable(String)",
            "comment": "",
            "description": "The name of the department the employee is assigned to.",
            "synonyms": None,
            "unit": None,
            "values": None,
            "observed_values": None,
            "client_defined": False,
            "sensitive": False,
        },
        {
            "name": "annual_salary",
            "type": "Nullable(Decimal(18, 6))",
            "comment": "",
            "description": SALARY_DESCRIPTION,
            "synonyms": ["salary", "pay"],
            "unit": "USD",
            "values": None,
            "observed_values": None,
            "client_defined": False,
            "sensitive": False,
        },
    ],
}

TOOLS_SCHEMA = [
    {"type": "function", "name": "getTableSchema", "description": "", "parameters": {}},
]


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return list(TOOLS_SCHEMA)


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=JWT, column_scope=frozenset())


class _FetchThenAnswerModel:
    """Round 1: fetch the schema. Round 2: answer. The schema the model sees on
    round 2 is what this test is about."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fetched = False

    async def send_turn(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurnResult:
        self.calls.append({"messages": messages})
        if self._fetched:
            return ModelTurnResult(assistant_text="Here it is.", usage={"total_tokens": 1})
        self._fetched = True
        return ModelTurnResult(
            tool_calls=[
                ToolCallRequest(
                    id="gts_1",
                    name="getTableSchema",
                    arguments={"database": "dbpcm_warehouse", "table": "employee"},
                )
            ],
            usage={"total_tokens": 1},
        )

    def begin_turn(self) -> _FetchThenAnswerModel:
        return self


def _schema_tool_message(messages: list[dict[str, Any]]) -> str:
    call_ids = {
        tc["id"]
        for m in messages
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
        if tc.get("function", {}).get("name") == "getTableSchema"
    }
    for m in messages:
        if m.get("role") == "tool" and m.get("tool_call_id") in call_ids:
            content = m.get("content")
            assert isinstance(content, str)
            return content
    raise AssertionError("no getTableSchema tool message in the replayed context")


async def _run(question: str | None) -> str:
    model = _FetchThenAnswerModel()
    mcp = FakeMCPClient(scripted={"getTableSchema": [json.loads(json.dumps(WIDE_SCHEMA))]})
    store = InMemorySessionStore()
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(
            mcp, CATALOG, schema_columns_token_budget=4_000
        ),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=6,
        max_wall_clock_seconds=60,
        max_budget_windows=2,
    )
    outcome = await loop.run(
        session_id=SESSION_ID,
        credentials=_credentials(),
        user_message=question or "describe the employee table",
    )
    assert outcome.status == "done"
    assert len(model.calls) == 2
    return _schema_tool_message(model.calls[1]["messages"])


def _emitted_columns(content: str) -> list[dict[str, Any]]:
    """The fitted schema out of the rendered tool message. `content` is the
    canonical trail rendering (`context/budget.py::_render_entry`), whose
    `result_preview.preview_rows[0][0]` cell IS the schema dict the fitter
    returned — the one surface the model actually reads."""
    rendered = json.loads(content)
    schema = rendered["result_preview"]["preview_rows"][0][0]
    return schema["columns"]


async def test_the_question_steers_which_columns_keep_their_documentation() -> None:
    content = await _run(QUESTION)

    # Every column is NAMED — including the two at the very end of a 302-column
    # list, which the old head-cut dropped along with 250-odd others.
    for name in ("filler_0", "filler_299", "department_name", "annual_salary"):
        assert f'"{name}"' in content

    # The question's own columns kept their DOCUMENTATION, all the way down to the
    # median guidance a salary answer is wrong without.
    assert "quantileExactLow" in content
    assert '"USD"' in content
    assert "The name of the department the employee is assigned to." in content

    # The question's columns are in the DETAILED GROUP AT THE FRONT of the list —
    # ahead of the 300 filler columns they physically sit behind.
    columns = _emitted_columns(content)
    detailed = [c["name"] for c in columns if set(c) - {"name", "type"}]
    assert [c["name"] for c in columns][: len(detailed)] == detailed
    assert detailed[0] == "annual_salary"

    # The table-level sections were never in the budget and arrive whole.
    assert "Exclude employees who never started" in content
    assert "'headcount' may mean active or all employees." in content

    # The marker is present and honest.
    assert "302 of 302 columns are listed" in content
    assert "name and type ONLY" in content
    assert "RELEVANCE order" in content
    assert "re-fetch" not in content

    # D25: the raw user text never reaches the model-facing tool result.
    assert NONCE not in content
    assert "average annual salary by department" not in content


async def test_without_the_question_the_same_cap_loses_the_salary_documentation() -> None:
    """The control. Same schema, same cap, a question that names none of it: the
    detail budget goes to the head of the list in order, and the salary column —
    still NAMED — keeps only its name and type. That difference is the whole
    value of threading the hint."""
    content = await _run("hello")

    assert '"annual_salary"' in content  # existence never depends on the question
    assert "quantileExactLow" not in content
    assert '"filler_0"' in content
