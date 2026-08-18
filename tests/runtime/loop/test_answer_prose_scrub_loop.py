"""The answer-prose scrub AT THE SEAMS (ISSUES I1) — every surface that hands
model prose to a user, driven through the real `AgentLoop`.

`tests/runtime/test_answer_scrub.py` pins the RULES. These pin the WIRING, and
each one exists because the rule being right is not the same as the rule being
applied:

  1. BOTH DONE EXITS, and the LIVE/HISTORY PARITY between them. The scrub runs
     once at the top of `_finish`, so the string in `TurnOutcome.assistant_text`
     and the string in the persisted `TurnMessage` are the same object — a second
     scrub at the persist site would be a second chance to diverge, and history
     disagreeing with the live answer on exactly the redacted turns is a bug the
     user would find long after anyone could explain it.

  2. THE I2 BOUNDARY. The structured payload — `answer_sql`, `answer_tables[].sql`,
     `blueprint_use` — is deliberately NOT scrubbed. An answer may say "[saved
     analysis]" while `blueprint_use` names the blueprint id one field away, and
     that is the design, not a leak: prose is the agent's voice, the payload is
     the audit surface. A future "consistency" fix that scrubs both would break
     the D56 transparency contract, so the divergence is asserted.

  3. THE PAUSES, both flavours, and the `askUser` QUESTION — which is model prose
     shown to a user exactly like an answer, and which is scrubbed at the point it
     is extracted so the persisted checkpoint and the outcome cannot disagree
     across a resume.

  4. THE EVENT is a count and a label, never a token. A telemetry payload that
     carried the matched identifier would publish precisely what the answer
     withheld.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.answer_scrub import (
    ANSWER_PROSE_REDACTED_EVENT,
    SAVED_ANALYSIS_MARKER,
    SCHEMA_DETAIL_MARKER,
)
from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.answer_with_table import AnswerWithTableTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolPause, ToolResult
from data_agent.runtime.loop.agent_loop import AgentLoop, TurnContext
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import ResultPreview
from tests._blueprint_gate import expand_blueprint

SESSION_ID = "sess-answer-scrub"
_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Department": "Nullable(String)"}})
_SQL = f"SELECT Department, count() AS n FROM {_E} GROUP BY Department"
_BP = "bp-active-headcount-by-department"

TOOLS_SCHEMA = [
    {"type": "function", "name": "runQuery", "description": "", "parameters": {}},
    {"type": "function", "name": "askUser", "description": "", "parameters": {}},
]


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return list(TOOLS_SCHEMA)


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


def _build(
    turns: list[ModelTurnResult],
    *,
    runtime_tools: dict[str, Any] | None = None,
    mcp: FakeMCPClient | None = None,
) -> tuple[AgentLoop, InMemorySessionStore, list[tuple[str, dict[str, Any]]]]:
    store = InMemorySessionStore()
    events: list[tuple[str, dict[str, Any]]] = []

    def _observe(event: str, payload: dict[str, Any]) -> None:
        events.append((event, payload))

    loop = AgentLoop(
        model_client=ScriptedModelClient(turns),
        tool_dispatcher=ToolDispatcher(mcp or FakeMCPClient(), CATALOG, observer=_observe),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=6,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=_observe,
        runtime_tools=runtime_tools or {},
    )
    return loop, store, events


def _redactions(events: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [payload for name, payload in events if name == ANSWER_PROSE_REDACTED_EVENT]


async def _assistant_messages(store: InMemorySessionStore) -> list[str | None]:
    doc = await store.get_or_create_session(SESSION_ID)
    return [m.content for m in doc.messages if m.role == "assistant"]


# --- exit A: the no-tool-calls finish ---------------------------------------


async def test_the_no_tool_calls_exit_scrubs_the_answer_and_persists_the_same_string() -> None:
    """THE PARITY ASSERTION. Two reads of one scrub: what the user is shown now,
    and what `/session/history` will show them tomorrow (it projects the persisted
    message). Asserting only the outcome would pass with the persist site
    unscrubbed, which is the worse of the two leaks — it survives the session."""
    loop, store, events = _build(
        [ModelTurnResult(assistant_text=f"I read {_E} to get this. Sales leads with 3.")]
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="who leads?"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == (
        f"I read {SCHEMA_DETAIL_MARKER} to get this. Sales leads with 3."
    )
    assert await _assistant_messages(store) == [outcome.assistant_text]
    assert _redactions(events) == [{"redaction_count": 1, "exit": "no_tool_calls"}]


async def test_a_clean_answer_is_untouched_and_emits_no_event() -> None:
    """The silent half at the seam: an ordinary business answer must come back
    byte-identical and cost no telemetry, or the event's rate stops meaning
    "disclosures" and the sentence boundaries stop meaning sentences."""
    loop, store, events = _build(
        [ModelTurnResult(assistant_text="Sales has 3 people. Engineering has 2.")]
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="headcount?"
    )

    assert outcome.assistant_text == "Sales has 3 people. Engineering has 2."
    assert await _assistant_messages(store) == ["Sales has 3 people. Engineering has 2."]
    assert _redactions(events) == []


# --- exit B: answerWithTable, and the I2 boundary ---------------------------


async def test_the_answer_with_table_exit_scrubs_prose_and_leaves_the_sql_alone() -> None:
    """THE I2 BOUNDARY, as a test. The same identifier appears in both halves of
    this turn — withheld in the sentence, verbatim in the SQL the UI renders
    beside it. Both assertions are load-bearing in OPPOSITE directions."""
    loop, store, events = _build(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="a1",
                        name="answerWithTable",
                        arguments={
                            "answer": f"Grouped {_E} by department_name.",
                            "tables": [{"sql": _SQL}],
                        },
                    )
                ]
            )
        ],
        runtime_tools={"answerWithTable": AnswerWithTableTool()},
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="by dept?"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == (
        f"Grouped {SCHEMA_DETAIL_MARKER} by {SCHEMA_DETAIL_MARKER}."
    )
    assert await _assistant_messages(store) == [outcome.assistant_text]
    # I2: the structured payload still names exactly what ran.
    assert outcome.answer_sql == _SQL
    assert outcome.answer_tables is not None
    assert outcome.answer_tables[0]["sql"] == _SQL
    assert _redactions(events) == [{"redaction_count": 2, "exit": "answer_with_table"}]


class _FakeBlueprintTool:
    """A `runBlueprint` that returns a successful `result_full` — enough for the
    loop to fold a `BlueprintRun` and project `blueprint_use`, without standing up
    the executor (this test is about the prose, not the DAG)."""

    async def run(
        self,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: TurnContext | None = None,
    ) -> ToolResult:
        return ToolResult(
            status="ok",
            tool_name="runBlueprint",
            error_code=None,
            retryable=None,
            user_message=None,
            provenance=frozenset({(_E, "Department")}),
            result_preview=ResultPreview(
                columns=["Department"], row_count=1, truncated=False, preview_rows=[["Sales"]]
            ),
            result_full={
                "blueprint_id": _BP,
                "terminal_sql": _SQL,
                "verify": {"passed": True, "grain_checked": True},
            },
        )


async def test_a_blueprint_id_is_withheld_from_prose_while_blueprint_use_still_names_it() -> None:
    """The extension the user asked for: the agent describes what it ran, it does
    not NAME the saved analysis. And the other half of I2 — `blueprint_use` is the
    field that names it, precisely, one hop away in the same result."""
    loop, store, events = _build(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="rb1",
                        name="runBlueprint",
                        arguments={"id": _BP, "slot_bindings": {"department": "Sales"}},
                    )
                ]
            ),
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="a1",
                        name="answerWithTable",
                        arguments={
                            "answer": f"I ran {_BP}: Sales leads with 3.",
                            "tables": [{"blueprint_id": _BP}],
                        },
                    )
                ]
            ),
        ],
        runtime_tools={
            "answerWithTable": AnswerWithTableTool(),
            "runBlueprint": _FakeBlueprintTool(),
        },
    )
    # The getBlueprint-before-runBlueprint gate reads the trail, so say the model
    # expanded it (tests/_blueprint_gate.py).
    await expand_blueprint(store, SESSION_ID, _BP)

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="headcount?"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == f"I ran {SAVED_ANALYSIS_MARKER}: Sales leads with 3."
    assert _BP not in (outcome.assistant_text or "")
    # I2: the id is still reported — as machine detail, where it belongs.
    assert outcome.blueprint_use == {"blueprint_id": _BP, "slots": {"department": "Sales"}}
    assert _redactions(events) == [{"redaction_count": 1, "exit": "answer_with_table"}]


# --- the pauses --------------------------------------------------------------


class _PausingTool:
    """Any wired runtime tool may raise a `ToolPause`; registered under a real
    advertised name so no fictional tool is introduced (the same device
    `test_turn_exit_contract.py` uses)."""

    async def run(
        self,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: TurnContext | None = None,
    ) -> ToolResult:
        return ToolResult(
            status="ok",
            tool_name="resolveValues",
            error_code=None,
            retryable=None,
            user_message=None,
            provenance=frozenset(),
            result_preview=None,
            result_full=None,
            pause=ToolPause(
                reason="blueprint_slot",
                pending_question={"question": "Which region?", "options": None},
            ),
        )


async def test_a_runtime_tool_pause_scrubs_its_partial_prose() -> None:
    """`_pause_from_runtime_tool` does NOT route through `_finish` (by design), so
    it carries its own call — the one place a copy of this rule can rot. Its
    `provenance` is `None`, which is why the id and the snake_case token below are
    the shapes asserted: they are the rules that need no knowledge of the turn."""
    loop, _store, events = _build(
        [
            ModelTurnResult(
                assistant_text=f"Started from {_BP} over employee_master.",
                tool_calls=[ToolCallRequest(id="r1", name="resolveValues", arguments={})],
            )
        ],
        runtime_tools={"resolveValues": _PausingTool()},
    )

    paused = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="go"
    )

    assert paused.status == "paused_ask_user"
    assert paused.assistant_text == (
        f"Started from {SAVED_ANALYSIS_MARKER} over {SCHEMA_DETAIL_MARKER}."
    )
    assert _redactions(events) == [{"redaction_count": 2, "exit": "pause"}]


async def test_the_ask_user_question_is_scrubbed_in_both_the_outcome_and_the_checkpoint() -> None:
    """The `askUser` QUESTION is model prose on a user's screen too — "which
    department_id did you mean?" discloses exactly what an answer would.

    BOTH READS ARE ASSERTED because they have different lifetimes: the outcome is
    what the UI shows now, the persisted checkpoint is what a resume replays. A
    scrub applied on the way out only would hand the unscrubbed question back the
    moment the session was reloaded.
    """
    loop, store, events = _build(
        [
            ModelTurnResult(
                assistant_text="One thing first.",
                tool_calls=[
                    ToolCallRequest(
                        id="q1",
                        name="askUser",
                        arguments={
                            "question": "Which department_id did you mean?",
                            "options": ["Sales", "Engineering"],
                        },
                    )
                ],
            )
        ],
    )

    paused = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="headcount?"
    )

    expected = f"Which {SCHEMA_DETAIL_MARKER} did you mean?"
    assert paused.status == "paused_ask_user"
    assert paused.pending_question == {"question": expected, "options": ["Sales", "Engineering"]}

    doc = await store.get_or_create_session(SESSION_ID)
    assert doc.pause_checkpoint is not None
    assert doc.pause_checkpoint.pending_question == {
        "question": expected,
        # The OPTIONS are untouched: they are the values the user clicks.
        "options": ["Sales", "Engineering"],
    }
    assert _redactions(events) == [{"redaction_count": 1, "exit": "ask_user_question"}]


# --- the event ---------------------------------------------------------------


async def test_the_event_carries_a_count_and_a_label_and_never_a_token() -> None:
    """D25: a matched token is BY DEFINITION something the runtime just decided the
    user may not see, and it may be a column name — which is deliberately not on
    the guardrail attribute allowlist. Publishing it as telemetry would undo the
    redaction through the side door, so the payload is asserted by its WHOLE
    contents, not by "the token is absent"."""
    loop, _store, events = _build(
        [
            ModelTurnResult(
                assistant_text=f"From {_E} and employee_master, via {_BP}."
            )
        ]
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q?")

    assert _redactions(events) == [{"redaction_count": 3, "exit": "no_tool_calls"}]
    blob = repr(events)
    assert "employee_master" not in blob
    assert _BP not in blob
