"""The finish-time ANSWER RULES (Release 1, doc 05 §L).

The registry exists because its rules have DIFFERENT PRECONDITIONS, and that is the
property most of these tests defend: `turn_sql is empty` belongs to the grounding
rule alone, so a markdown table or a pasted query is refused whether or not a query
ran — and both are likeliest exactly when one did.

The other half is the ALLOWANCE. Rules charge to an existing kind rather than
minting one each, so `store.claims` is where a rule's complaint is attributed:
`ungrounded_quantity` spends `ungrounded_answer`, while `sql_in_answer` and
`markdown_table` both spend the answer-shape gate's own grant, and N rules never
mean N extra round-trips.

PRECEDENCE IS CONTRACT, so no test here asserts merely that "a nudge appeared":
grounding outranks form, and between the two form rules content outranks channel.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import UpdateAnalysisStateTool
from data_agent.runtime.composite.answer_with_table import AnswerWithTableTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.loop.answer_rules import (
    ANSWER_RULE_EXHAUSTED_EVENT,
    ANSWER_RULE_REFUSED_EVENT,
    asserts_quantity,
    contains_sql,
    first_match,
    has_markdown_table,
)
from data_agent.runtime.loop.finalization import (
    EMPTY_ANSWER_REFUSED_EVENT,
)
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import FinalizationBlockKind

SESSION_ID = "sess-answer-rules"
_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle(
    {_E: {"EmployeeCode": "String", "Department": "Nullable(String)", "Name": "String"}}
)

STATE = "updateAnalysisState"
ANSWER = "answerWithTable"

# Phrases unique to each nudge, so a test can say WHICH rule fired. Precedence
# between the rules — and between them and the three gates — is part of the
# contract, so "a nudge appeared" is never a sufficient assertion.
_GROUNDING_MARK = "this turn has no supporting evidence"
_MARKDOWN_MARK = "contains a markdown table"
_SQL_MARK = "That answer contains SQL"

# The failure this shipped for: a real trace where the model made ONE tool call
# (a schema fetch), ran no query, and reported a headcount that existed nowhere.
_FABRICATED = "The current active employee count is **9,184**."


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return []


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


class CountingStore(InMemorySessionStore):
    """The real store, recording the KIND of every allowance claim attempt — the only
    place a rule's charge is observable."""

    def __init__(self) -> None:
        super().__init__()
        self.claims: list[str] = []

    async def claim_finalization_block(
        self,
        session_id: str,
        turn_index: int,
        window_count: int,
        kind: FinalizationBlockKind,
    ) -> bool:
        self.claims.append(kind)
        return await super().claim_finalization_block(session_id, turn_index, window_count, kind)


def _build(
    turns: list[ModelTurnResult],
    *,
    mcp: FakeMCPClient | None = None,
) -> tuple[AgentLoop, CountingStore, list[tuple[str, dict[str, Any]]], ScriptedModelClient]:
    store = CountingStore()
    events: list[tuple[str, dict[str, Any]]] = []

    def _observe(event: str, payload: dict[str, Any]) -> None:
        events.append((event, payload))

    model = ScriptedModelClient(turns)
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(mcp or FakeMCPClient(), CATALOG, observer=_observe),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=_observe,
        runtime_tools={
            STATE: UpdateAnalysisStateTool(session_store=store, observer=_observe),
            ANSWER: AnswerWithTableTool(),
        },
    )
    return loop, store, events, model


def _rows_mcp(tool: str, *row_counts: int) -> FakeMCPClient:
    return FakeMCPClient(
        scripted={
            tool: [
                {
                    "columns": ["Department", "n"],
                    "rows": [[f"D{i}", i] for i in range(n)],
                    "row_count": n,
                    "truncated": False,
                }
                for n in row_counts
            ]
        }
    )


def _query(call_id: str) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name="runQuery",
        arguments={"sql": f"SELECT Department, count() AS n FROM {_E} GROUP BY Department"},
    )


def _events(events: list[tuple[str, dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return [payload for event, payload in events if event == name]


def _requests_carrying(model: ScriptedModelClient, mark: str) -> list[int]:
    return [
        index
        for index, call in enumerate(model.calls)
        if any(mark in str(message.get("content") or "") for message in call.messages)
    ]


def _nudge_text(model: ScriptedModelClient, mark: str) -> str:
    return next(
        str(m.get("content"))
        for call in model.calls
        for m in call.messages
        if mark in str(m.get("content") or "")
    )


# --- the grounding rule FIRES -----------------------------------------------


async def test_a_figure_with_no_query_behind_it_is_refused_and_recovers() -> None:
    """The trace this shipped for, end to end: prose reporting a headcount, no
    runQuery and no runBlueprint anywhere in the turn, one refusal, and a grounded
    answer on the round that was handed back — accepted VERBATIM."""
    loop, store, events, model = _build(
        [
            ModelTurnResult(assistant_text=_FABRICATED),
            ModelTurnResult(tool_calls=[_query("c1")]),
            ModelTurnResult(assistant_text="There are 412 active employees."),
        ],
        mcp=_rows_mcp("runQuery", 1),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="count of employees"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "There are 412 active employees."
    assert _events(events, ANSWER_RULE_REFUSED_EVENT) == [{"rule": "ungrounded_quantity"}]
    assert not _events(events, ANSWER_RULE_EXHAUSTED_EVENT)
    assert store.claims == ["ungrounded_answer"]
    # ONE round-trip of nudge lifetime (05 §D).
    assert _requests_carrying(model, _GROUNDING_MARK) == [1]


async def test_the_refused_draft_is_carried_back_and_never_persisted() -> None:
    """Exit #1 persists nothing, so the echo is the model's only surviving copy of
    what it wrote — and the nudge itself must not reach the user as something they
    said."""
    loop, _store, _events_, model = _build(
        [
            ModelTurnResult(assistant_text=_FABRICATED),
            ModelTurnResult(assistant_text="I could not run a query for that."),
        ]
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="count of employees"
    )

    nudge = _nudge_text(model, _GROUNDING_MARK)
    assert _FABRICATED in nudge, "the draft must come back — nothing else holds it"
    assert "complete Help Center document" in nudge
    assert "getHelpCenterDocument" in nudge
    assert outcome.assistant_text == "I could not run a query for that."


async def test_a_second_ungrounded_finish_passes_and_says_so() -> None:
    """The runtime never hard-locks a turn. These rules read SHAPE, not truth, so a
    second refusal would be the runtime destroying an answer it cannot prove wrong —
    the prose passes and the EXHAUSTED event is what makes the pass visible."""
    loop, store, events, _model = _build(
        [
            ModelTurnResult(assistant_text=_FABRICATED),
            ModelTurnResult(assistant_text=_FABRICATED),
        ]
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="count of employees"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == _FABRICATED
    assert _events(events, ANSWER_RULE_EXHAUSTED_EVENT) == [{"rule": "ungrounded_quantity"}]
    assert store.claims == ["ungrounded_answer", "ungrounded_answer"]


# --- the grounding rule STAYS SILENT ----------------------------------------


async def test_a_figure_with_a_query_behind_it_is_untouched() -> None:
    """The precondition, and the case that must never cost a round-trip: the turn
    ran a query, so the figure has something behind it and the rule does not apply.
    ONE row, so the answer-shape gate is disarmed and this is the only rule in play."""
    loop, store, events, _model = _build(
        [
            ModelTurnResult(tool_calls=[_query("c1")]),
            ModelTurnResult(assistant_text="There are 9,184 active employees."),
        ],
        mcp=_rows_mcp("runQuery", 1),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="count of employees"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "There are 9,184 active employees."
    assert not _events(events, ANSWER_RULE_REFUSED_EVENT)
    assert store.claims == [], "a silent rule must not touch the store"


async def test_tool_calls_with_no_prose_are_untouched() -> None:
    """The shape most round-trips have."""
    loop, store, events, _model = _build(
        [
            ModelTurnResult(tool_calls=[_query("c1")]),
            ModelTurnResult(assistant_text="Done."),
        ],
        mcp=_rows_mcp("runQuery", 1),
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    assert not _events(events, ANSWER_RULE_REFUSED_EVENT)
    assert store.claims == []


async def test_empty_prose_belongs_to_the_empty_answer_gate() -> None:
    """`first_match` returns `None` for blank prose, so the two conditions are
    disjoint by construction and this rule can never pre-empt the more specific
    complaint about a silent finish."""
    loop, store, events, _model = _build(
        [
            ModelTurnResult(assistant_text=None),
            ModelTurnResult(assistant_text="I could not run a query for that."),
        ]
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    assert _events(events, EMPTY_ANSWER_REFUSED_EVENT) == [{"incomplete_reason": ""}]
    assert not _events(events, ANSWER_RULE_REFUSED_EVENT)
    assert store.claims == ["empty_answer"]


# --- the markdown rule: a DIFFERENT precondition ----------------------------


_MARKDOWN_ANSWER = (
    "Here is the breakdown:\n\n"
    "| Department | Headcount |\n"
    "| --- | --- |\n"
    "| Sales | 120 |\n"
    "| Ops | 98 |\n"
)


async def test_a_markdown_table_is_refused_even_though_a_query_ran() -> None:
    """THE REASON THE REGISTRY EXISTS. Hoisting `turn_sql is empty` into a shared
    condition would exclude exactly this case — rows pasted as markdown, which is
    likeliest when a query DID run. One row, so the answer-shape gate is disarmed
    and this rule is covering the gap rather than duplicating it."""
    loop, store, events, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("c1")]),
            ModelTurnResult(assistant_text=_MARKDOWN_ANSWER),
            ModelTurnResult(assistant_text="Sales leads on headcount."),
        ],
        mcp=_rows_mcp("runQuery", 1),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="headcount by dept"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "Sales leads on headcount."
    assert _events(events, ANSWER_RULE_REFUSED_EVENT) == [{"rule": "markdown_table"}]
    assert _requests_carrying(model, _MARKDOWN_MARK) == [2]


async def test_the_markdown_rule_charges_the_answer_shape_allowance() -> None:
    """A FORM complaint spends the form gate's grant, not a fourth kind. This is what
    keeps N rules from meaning N extra round-trips per window."""
    loop, store, _events_, _model = _build(
        [
            ModelTurnResult(tool_calls=[_query("c1")]),
            ModelTurnResult(assistant_text=_MARKDOWN_ANSWER),
            ModelTurnResult(assistant_text="Sales leads on headcount."),
        ],
        mcp=_rows_mcp("runQuery", 1),
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    assert store.claims == ["answer_shape"]


async def test_grounding_beats_form_on_a_fabricated_table() -> None:
    """A table with no query behind it matches BOTH rules. Order is precedence, and
    the grounding complaint wins: telling the model to reformat would be correcting
    the presentation of an invented answer."""
    loop, store, events, _model = _build(
        [
            ModelTurnResult(assistant_text=_MARKDOWN_ANSWER),
            ModelTurnResult(assistant_text="I could not run a query for that."),
        ]
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    assert _events(events, ANSWER_RULE_REFUSED_EVENT) == [{"rule": "ungrounded_quantity"}]
    assert store.claims == ["ungrounded_answer"]


# --- the SQL rule: the prompt's unconditional rule, enforced -----------------


_SQL_ANSWER = (
    "There are 412 active employees. I got that with:\n\n"
    "```sql\n"
    "SELECT count() FROM dbpcm_warehouse.employee WHERE Status = 'Active'\n"
    "```\n"
)


async def test_sql_in_the_answer_is_refused_even_though_a_query_ran() -> None:
    """The reported case, and the reason it needs a RULE rather than a scrub: the
    turn did everything right except paste its query into the prose. `answer_scrub`
    would not have caught it — it redacts identifier SHAPES, so it takes the names
    out of the statement and leaves the keywords, and the user reads a half-redacted
    query where the answer should be."""
    loop, store, events, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("c1")]),
            ModelTurnResult(assistant_text=_SQL_ANSWER),
            ModelTurnResult(assistant_text="There are 412 active employees."),
        ],
        mcp=_rows_mcp("runQuery", 1),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="how many employees"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "There are 412 active employees."
    assert _events(events, ANSWER_RULE_REFUSED_EVENT) == [{"rule": "sql_in_answer"}]
    assert _requests_carrying(model, _SQL_MARK) == [2]


async def test_the_sql_rule_charges_the_answer_shape_allowance() -> None:
    """A second FORM complaint, and it must not mint a kind either — that is the
    property that keeps the window's worst case at four extra round-trips however
    many rules the registry grows to."""
    loop, store, _events_, _model = _build(
        [
            ModelTurnResult(tool_calls=[_query("c1")]),
            ModelTurnResult(assistant_text=_SQL_ANSWER),
            ModelTurnResult(assistant_text="There are 412 active employees."),
        ],
        mcp=_rows_mcp("runQuery", 1),
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    assert store.claims == ["answer_shape"]


async def test_the_sql_draft_comes_back_and_a_second_leak_passes() -> None:
    """Same posture as every other rule: the draft is echoed because exit #1 holds no
    other copy of it, and the runtime never hard-locks a turn — a second leak spends
    the allowance, emits EXHAUSTED and SHIPS."""
    loop, _store, events, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("c1")]),
            ModelTurnResult(assistant_text=_SQL_ANSWER),
            ModelTurnResult(assistant_text=_SQL_ANSWER),
        ],
        mcp=_rows_mcp("runQuery", 1),
    )

    outcome = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    assert "SELECT count() FROM" in _nudge_text(model, _SQL_MARK)
    assert _events(events, ANSWER_RULE_EXHAUSTED_EVENT) == [{"rule": "sql_in_answer"}]
    assert outcome.status == "done"
    assert outcome.assistant_text is not None


async def test_grounding_beats_the_sql_rule() -> None:
    """A figure with a query pasted beside it but no query RUN matches both. The
    grounding complaint wins for §L.5's reason — the SQL is evidence of nothing if the
    turn never executed it."""
    loop, store, events, _model = _build(
        [
            ModelTurnResult(assistant_text=_SQL_ANSWER),
            ModelTurnResult(assistant_text="I could not run a query for that."),
        ]
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    assert _events(events, ANSWER_RULE_REFUSED_EVENT) == [{"rule": "ungrounded_quantity"}]
    assert store.claims == ["ungrounded_answer"]


def test_content_beats_channel_between_the_two_form_rules() -> None:
    """Rows AND the query behind them. `sql_in_answer` wins: text that must not be in
    an answer at all outranks the right content sent out the wrong door, and its fix is
    a deletion the model can always make."""
    both = _MARKDOWN_ANSWER + "\n```sql\nSELECT Department, count() FROM employee\n```"

    assert has_markdown_table(both) and contains_sql(both)
    assert first_match(both, ["SELECT 1"]).name == "sql_in_answer"


# --- the predicates ---------------------------------------------------------


def test_the_quantity_predicate_is_deliberately_narrow() -> None:
    """A false negative costs nothing beyond the status quo; a false positive burns a
    round-trip and tells a correct model it fabricated. So the threshold is three
    digits or a thousands separator — which lets through the small numbers a
    KNOWLEDGE-grounded answer legitimately carries with no query behind it."""
    assert asserts_quantity("The current active employee count is **9,184**.")
    assert asserts_quantity("There are 412 active employees.")
    assert asserts_quantity("Average tenure is 4.25 years.")

    assert not asserts_quantity("Overtime is paid at 1.5 times the base hourly rate.")
    assert not asserts_quantity("Hours beyond 40 in a single work week count as overtime.")
    assert not asserts_quantity("There are 3 pay frequencies in use.")
    assert not asserts_quantity("The warehouse covers pay, time and hiring.")


def test_prose_punctuation_is_not_a_thousands_separator() -> None:
    """REGRESSION. A `\\d[\\d,]*` scan reads the comma in "Sales 3, Eng 2" as a thousands
    separator and refuses the sentence as a reported figure — the false-positive class
    this predicate is tuned to avoid, and one that shows up in ordinary prose
    constantly. The separator only counts BETWEEN digits."""
    assert not asserts_quantity("Sales 3, Eng 2.")
    assert not asserts_quantity("For Sales in 2026, the answer is 7.")
    assert not asserts_quantity("Sales, Ops and Engineering each have 2 openings.")
    assert asserts_quantity("Headcount is 9,184.")


def test_years_and_dates_are_not_reported_figures() -> None:
    assert not asserts_quantity("The latest data on record is from 2026.")
    assert not asserts_quantity("Figures are as of 2026-03-31.")
    assert not asserts_quantity("Figures are as of 3/31/2026.")
    # A year-shaped token is only exempt as a BARE year: the same digits with a
    # thousands separator are a quantity.
    assert asserts_quantity("Headcount reached 2,026 last quarter.")


def test_the_markdown_predicate_needs_a_header_and_a_separator() -> None:
    assert has_markdown_table(_MARKDOWN_ANSWER)
    assert has_markdown_table("| a | b |\n|---|---|\n| 1 | 2 |")

    # Prose can carry a stray pipe, and a bare rule is not a table.
    assert not has_markdown_table("Use the pay period | the pay date to reconcile.")
    assert not has_markdown_table("Summary\n---\nSales leads on headcount.")
    assert not has_markdown_table("| a | b |\nno separator here")


def test_the_sql_predicate_matches_fences_code_spans_and_caps() -> None:
    """The three shapes that are not ordinary English: a tagged fence, a fence whose
    first word is a statement keyword (any case), and the `SELECT`…`FROM` pair either
    in CAPS or inside a code span."""
    assert contains_sql(_SQL_ANSWER)
    assert contains_sql("```\nselect count() from dbpcm_warehouse.employee\n```")
    assert contains_sql("I ran `select count() from employee` to get it.")
    assert contains_sql("Counted with SELECT count() FROM employee WHERE Status = 'Active'.")
    assert contains_sql("```\nWITH active AS (select 1)\nselect * from active\n```")


def test_ordinary_prose_is_not_sql() -> None:
    """THE FALSE POSITIVE THIS PREDICATE IS TUNED AGAINST. `select`, `from` and `where`
    are among the commonest words in an English answer, so keyword membership is
    unusable — a correct, business-language answer must be free to use all three. The
    cost of getting this wrong is a round-trip spent telling a clean answer it leaked
    schema."""
    assert not contains_sql("The data comes from the employee table where department is Sales.")
    assert not contains_sql("You can select from the following options: Sales, Ops.")
    assert not contains_sql("Sales leads on headcount with 412 people.")
    assert not contains_sql("Pick a SELECT option.")
    assert not contains_sql("The FROM clause was rejected by the warehouse.")
    assert not contains_sql(_MARKDOWN_ANSWER)
    # A fence is not enough on its own — a fenced block of plain output is not SQL.
    assert not contains_sql("```\nDepartment  Headcount\nSales       120\n```")


def test_first_match_returns_at_most_one_rule() -> None:
    """One refusal per round-trip is the rule the whole gate chain expresses
    structurally, and the registry must not break it."""
    assert first_match("", None) is None
    assert first_match("   ", None) is None
    assert first_match("Sales leads on headcount.", None) is None
    assert first_match(_FABRICATED, None).name == "ungrounded_quantity"
    assert first_match(_FABRICATED, None, has_alternative_evidence=True) is None
    assert first_match(_FABRICATED, ["SELECT 1"]) is None
    assert first_match("There are 9,184 employees.", ["SELECT 1"]) is None
    assert first_match(_MARKDOWN_ANSWER, ["SELECT 1"]).name == "markdown_table"
    assert first_match(_SQL_ANSWER, ["SELECT 1"]).name == "sql_in_answer"
