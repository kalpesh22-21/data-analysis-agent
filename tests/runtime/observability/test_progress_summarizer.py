"""Unit tests for observability/progress_summarizer.py (Layer 1, all fakes).

A `ProgressSummarizer` over a FAKE `ModelClient`: it returns the trimmed
present-tense line the model produced, and fails soft (returns `None`) on a
model error, a timeout, or empty output — never raising into the caller.

The second half of the file guards the DISCLOSURE boundary: this line reaches the
UI verbatim (`progress.py` bypasses the `shape` allowlist for it), so the model
must never be SHOWN a physical identifier (default-deny argument projection), and
a line that repeats one anyway must be replaced by the tool's static phrasing.
"""

from __future__ import annotations

import asyncio

from data_agent.runtime.model.client import ModelTurnResult
from data_agent.runtime.observability.progress_summarizer import (
    _ARG_ALLOWLIST,
    _STATIC_LINES,
    _SYSTEM_PROMPT,
    ProgressSummarizer,
)


class _FakeModelClient:
    """Records the (messages, tools) it saw and returns a canned line."""

    def __init__(self, text: str | None) -> None:
        self._text = text
        self.calls: list[tuple[list[dict], list[dict]]] = []

    async def send_turn(self, messages: list[dict], tools: list[dict]) -> ModelTurnResult:
        self.calls.append((messages, tools))
        return ModelTurnResult(assistant_text=self._text, tool_calls=[], usage={})


class _RaisingModelClient:
    async def send_turn(self, messages: list[dict], tools: list[dict]) -> ModelTurnResult:
        raise RuntimeError("model exploded")


class _SlowModelClient:
    def __init__(self, delay: float) -> None:
        self._delay = delay

    async def send_turn(self, messages: list[dict], tools: list[dict]) -> ModelTurnResult:
        await asyncio.sleep(self._delay)
        return ModelTurnResult(assistant_text="too late", tool_calls=[], usage={})


async def test_summarize_returns_trimmed_line() -> None:
    fake = _FakeModelClient("  Querying overtime pay by department  ")
    summarizer = ProgressSummarizer(fake)

    line = await summarizer.summarize("runQuery", {"sql": "SELECT 1"})

    assert line == "Querying overtime pay by department"
    # One round-trip, no tools advertised (tools=[]).
    assert len(fake.calls) == 1
    _messages, tools = fake.calls[0]
    assert tools == []


async def test_summarize_strips_wrapping_quotes() -> None:
    fake = _FakeModelClient('"Listing tables in the warehouse"')
    summarizer = ProgressSummarizer(fake)

    assert await summarizer.summarize("listTables", {}) == "Listing tables in the warehouse"


async def test_summarize_passes_tool_name_and_allowlisted_args_to_prompt() -> None:
    """`concept` is the natural-language concept being resolved — allowlisted.
    `table`/`column` are physical identifiers and are withheld by omission."""
    fake = _FakeModelClient("Resolving department codes")
    summarizer = ProgressSummarizer(fake)

    await summarizer.summarize(
        "resolveValues",
        {"concept": "earnings", "table": "dbpcm_warehouse.payroll", "column": "EarnCode"},
    )

    messages, _tools = fake.calls[0]
    user_content = messages[-1]["content"]
    assert "resolveValues" in user_content
    assert "earnings" in user_content
    assert "payroll" not in user_content
    assert "EarnCode" not in user_content


async def test_summarize_truncates_huge_arg_to_bound_tokens() -> None:
    fake = _FakeModelClient("Searching for a matching analysis")
    summarizer = ProgressSummarizer(fake)
    huge_query = "overtime " * 2000

    await summarizer.summarize("searchBlueprints", {"query": huge_query})

    messages, _tools = fake.calls[0]
    user_content = messages[-1]["content"]
    # The 18,000-char arg must have been bounded well below its original size.
    assert len(user_content) < 2000


async def test_summarize_returns_none_on_model_error() -> None:
    summarizer = ProgressSummarizer(_RaisingModelClient())
    assert await summarizer.summarize("runQuery", {"sql": "SELECT 1"}) is None


async def test_summarize_returns_none_on_empty_output() -> None:
    assert await ProgressSummarizer(_FakeModelClient("")).summarize("runQuery", {}) is None
    assert await ProgressSummarizer(_FakeModelClient(None)).summarize("runQuery", {}) is None
    assert await ProgressSummarizer(_FakeModelClient("   ")).summarize("runQuery", {}) is None


async def test_summarize_returns_none_on_timeout() -> None:
    summarizer = ProgressSummarizer(_SlowModelClient(delay=5.0), timeout_seconds=0.01)
    assert await summarizer.summarize("runQuery", {"sql": "SELECT 1"}) is None


# --- the disclosure boundary ------------------------------------------------


async def test_run_query_sql_never_reaches_the_model_payload() -> None:
    """The core rule: `runQuery` contributes its NAME ONLY. The whole prompt is
    inspected (not just the args blob), because anything the model is shown can
    come back in a line that goes to the UI verbatim."""
    fake = _FakeModelClient("Running a query against the warehouse")
    summarizer = ProgressSummarizer(fake)
    sql = (
        "SELECT d.DeptName, sum(p.OvertimePay) FROM dbpcm_warehouse.payroll p "
        "JOIN dbpcm_warehouse.department d ON d.DeptId = p.DeptId GROUP BY 1"
    )

    await summarizer.summarize("runQuery", {"sql": sql, "limit": 100})

    messages, _tools = fake.calls[0]
    user_content = str(messages[-1]["content"])
    payload = " ".join(str(message["content"]) for message in messages)
    for fragment in ("SELECT", "payroll", "department", "OvertimePay", "DeptId"):
        assert fragment not in user_content, f"{fragment!r} reached the summarizer prompt"
    # The distinctive identifiers are absent from the WHOLE payload, system
    # instruction included (the word "department" appears there as an example of a
    # business-level parameter, which is why the scan above is user-message-scoped).
    assert "dbpcm_warehouse" not in payload
    assert "OvertimePay" not in payload
    # The tool name is all it gets.
    assert "runQuery" in user_content
    assert "{}" in user_content


async def test_run_query_sql_never_reaches_the_model_for_any_argument_shape() -> None:
    """The rule above holds for the SHAPE of the arguments too, not just the one
    well-formed call the model usually makes.

    `_project_args` denies `runQuery` wholesale, so it never inspects the value of
    `sql` — this pins that, because a guard written the other way round (find
    `sql`, drop it) breaks on every one of these: a non-string `sql`, a missing
    `sql`, one buried under another key, one too long to have been read, one
    written in quoted or non-ASCII identifiers.
    """
    shapes: list[tuple[str, dict]] = [
        ("well-formed", {"sql": "SELECT 1 FROM dbpcm_warehouse.employee", "limit": 100}),
        ("sql as a dict", {"sql": {"text": "SELECT 1 FROM dbpcm_warehouse.employee"}}),
        ("sql as a list", {"sql": ["SELECT 1 FROM dbpcm_warehouse.employee"]}),
        ("sql as a number", {"sql": 42, "database": "dbpcm_warehouse"}),
        ("no sql key at all", {"limit": 100, "database": "dbpcm_warehouse"}),
        (
            "nested under another key",
            {"payload": {"q": {"sql": "SELECT * FROM dbpcm_warehouse.payroll"}}},
        ),
        ("extremely long", {"sql": "SELECT " + "col, " * 5000 + "1 FROM dbpcm_warehouse.employee"}),
        ("backquoted identifiers", {"sql": "SELECT 1 FROM `dbpcm_warehouse`.`employee`"}),
        ("double-quoted identifiers", {"sql": 'SELECT 1 FROM "dbpcm_warehouse"."employee"'}),
        ("non-ascii identifiers", {"sql": "SELECT 1 FROM dbpcm_warehouse.Mitarbeiter_übersicht"}),
        ("empty arguments", {}),
    ]

    for label, arguments in shapes:
        fake = _FakeModelClient("Running a query against the warehouse")
        await ProgressSummarizer(fake).summarize("runQuery", arguments)

        messages, _tools = fake.calls[0]
        user_content = str(messages[-1]["content"])
        # Name only, every time — the argument blob is the empty object.
        assert user_content == "Tool: runQuery. Arguments: {}", label
        payload = " ".join(str(message["content"]) for message in messages)
        for fragment in ("SELECT", "dbpcm_warehouse", "employee", "payroll", "Mitarbeiter"):
            assert fragment not in payload, f"{fragment!r} reached the prompt for {label}"


async def test_malformed_arguments_fail_soft_instead_of_raising() -> None:
    """A non-dict `arguments` is a model/transport bug, not a reason to break the
    turn: the summarizer is fire-and-forget, so it returns `None` and the instant
    template label stands. Asserted for a DENIED tool (never touched) and an
    ALLOWLISTED one (projection actually reads `.items()`)."""
    for tool in ("runQuery", "runBlueprint"):
        for arguments in (None, ["SELECT 1"], "SELECT 1", 7):
            fake = _FakeModelClient("Running something")
            assert await ProgressSummarizer(fake).summarize(tool, arguments) is None, (
                f"{tool} / {arguments!r}"
            )


async def test_unknown_tool_contributes_its_name_only() -> None:
    """Default-deny: a tool nobody has vetted passes NO arguments."""
    fake = _FakeModelClient("Doing something")
    summarizer = ProgressSummarizer(fake)

    await summarizer.summarize("someNewTool", {"database": "dbpcm_warehouse", "table": "employee"})

    messages, _tools = fake.calls[0]
    payload = " ".join(str(message["content"]) for message in messages)
    assert "someNewTool" in payload
    assert "dbpcm_warehouse" not in payload
    assert "employee" not in payload


async def test_listing_and_schema_tools_never_show_database_or_table_names() -> None:
    fake = _FakeModelClient("Checking what data is available")
    summarizer = ProgressSummarizer(fake)

    for tool, args in (
        ("listTables", {"database": "dbpcm_warehouse"}),
        ("getTableSchema", {"database": "dbpcm_warehouse", "table": "employee"}),
        ("listDatabases", {}),
        ("sampleRows", {"database": "dbpcm_warehouse", "table": "employee", "limit": 5}),
    ):
        fake.calls.clear()
        await summarizer.summarize(tool, args)
        messages, _tools = fake.calls[0]
        payload = " ".join(str(message["content"]) for message in messages)
        assert "dbpcm_warehouse" not in payload, tool
        assert "employee" not in payload, tool


async def test_run_blueprint_slot_values_and_id_pass_through() -> None:
    """The D25 relaxation this channel exists for: business values are the point."""
    fake = _FakeModelClient("Running headcount for Engineering in January")
    summarizer = ProgressSummarizer(fake)

    line = await summarizer.summarize(
        "runBlueprint",
        {
            "id": "bp-active-headcount-by-department",
            "slot_bindings": {"department": "Engineering", "period": "2026-01"},
            "serves_intent": "i1",
        },
    )

    assert line == "Running headcount for Engineering in January"
    messages, _tools = fake.calls[0]
    user_content = messages[-1]["content"]
    assert "Engineering" in user_content
    assert "2026-01" in user_content
    assert "bp-active-headcount-by-department" in user_content


async def test_search_tools_pass_the_user_language_query() -> None:
    fake = _FakeModelClient("Looking for an overtime analysis")
    summarizer = ProgressSummarizer(fake)

    for tool in ("searchBlueprints", "searchKnowledge"):
        fake.calls.clear()
        await summarizer.summarize(tool, {"query": "overtime pay by department", "k": 5})
        messages, _tools = fake.calls[0]
        assert "overtime pay by department" in messages[-1]["content"], tool


async def test_a_line_repeating_a_withheld_identifier_falls_back_to_the_static_line() -> None:
    """Second layer. The model cannot have been shown this SQL — but if a line
    names a `db.table` from the RAW arguments anyway, it is not emitted."""
    fake = _FakeModelClient("Reading dbpcm_warehouse.employee for active staff")
    summarizer = ProgressSummarizer(fake)

    line = await summarizer.summarize(
        "runQuery", {"sql": "SELECT count() FROM dbpcm_warehouse.employee"}
    )

    assert line == "running a query against the warehouse"


async def test_the_post_filter_catches_a_bare_table_name_from_a_withheld_arg() -> None:
    fake = _FakeModelClient("Fetching the columns of EmployeeMaster")
    summarizer = ProgressSummarizer(fake)

    line = await summarizer.summarize(
        "getTableSchema", {"database": "dbpcm_warehouse", "table": "EmployeeMaster"}
    )

    assert line == "checking what data is available"


async def test_the_post_filter_catches_a_bare_table_name_from_the_withheld_sql() -> None:
    """The other half of the bare-name case: the name is not a whole argument
    value here, it is a `FROM` target inside a `sql` string the model never saw."""
    fake = _FakeModelClient("Counting rows in employee_master")
    summarizer = ProgressSummarizer(fake)

    line = await summarizer.summarize(
        "runQuery", {"sql": "SELECT count() FROM employee_master WHERE Active = 1"}
    )

    assert line == "running a query against the warehouse"


async def test_the_post_filter_is_case_insensitive() -> None:
    """A model that title-cases or upper-cases the identifier is still leaking it,
    so the match is on the lowered form of both sides."""
    sql = "SELECT count() FROM dbpcm_warehouse.employee"
    for produced in (
        "Reading DBPCM_WAREHOUSE.EMPLOYEE for active staff",
        "Reading Dbpcm_Warehouse.Employee for active staff",
        "Counting rows in EMPLOYEE",
    ):
        fake = _FakeModelClient(produced)
        line = await ProgressSummarizer(fake).summarize("runQuery", {"sql": sql})
        assert line == "running a query against the warehouse", produced


async def test_the_post_filter_reads_a_withheld_identifier_out_of_a_nested_argument() -> None:
    """`_collect_strings` flattens the withheld args, so an identifier nested a
    level down is still forbidden material for the produced line."""
    fake = _FakeModelClient("Reading dbpcm_warehouse.payroll")
    summarizer = ProgressSummarizer(fake)

    line = await summarizer.summarize(
        "runQuery", {"payload": {"q": {"sql": "SELECT 1 FROM dbpcm_warehouse.payroll"}}}
    )

    assert line == "running a query against the warehouse"


async def test_the_post_filter_leaves_a_clean_line_alone() -> None:
    """A false positive downgrades every line for a tool, so the match is on
    identifier-looking tokens from the withheld args only — ordinary business
    prose passes through untouched."""
    fake = _FakeModelClient("Counting active staff for January")
    summarizer = ProgressSummarizer(fake)

    line = await summarizer.summarize(
        "runQuery", {"sql": "SELECT count() FROM dbpcm_warehouse.employee"}
    )

    assert line == "Counting active staff for January"


async def test_an_allowlisted_value_is_never_treated_as_a_leak() -> None:
    """`slot_bindings` values are ALLOWED in the line; scanning for them would
    reject exactly the lines this channel exists to produce."""
    fake = _FakeModelClient("Running headcount for Engineering")
    summarizer = ProgressSummarizer(fake)

    line = await summarizer.summarize(
        "runBlueprint",
        {"id": "bp-headcount", "slot_bindings": {"department": "Engineering"}},
    )

    assert line == "Running headcount for Engineering"


async def test_an_unknown_tool_falls_back_to_the_generic_line() -> None:
    fake = _FakeModelClient("Reading dbpcm_warehouse.employee")
    summarizer = ProgressSummarizer(fake)

    line = await summarizer.summarize("someNewTool", {"table": "dbpcm_warehouse.employee"})

    assert line == "working on your question"


# --- the UNCONDITIONAL structural check (provenance-independent) ------------


async def test_a_dotted_identifier_from_an_allowlisted_arg_is_still_replaced() -> None:
    """The hole the shape check closes.

    `resolveValues.concept` is allowlisted, so it reaches the summarizer legally,
    and it is authored by the SCHEMA-AWARE main model — so it can name a table.
    The withheld-token scan cannot see it (nothing was withheld that contains it),
    which is why the line's own SHAPE is checked as well.
    """
    fake = _FakeModelClient("Resolving codes in dbpcm_warehouse.payroll")
    summarizer = ProgressSummarizer(fake)

    line = await summarizer.summarize(
        "resolveValues", {"concept": "earnings codes in dbpcm_warehouse.payroll"}
    )

    assert line == "matching your wording to the stored values"


async def test_a_dotted_identifier_in_a_slot_value_or_search_query_is_replaced() -> None:
    """Same hole, the other allowlisted free-text carriers."""
    cases = (
        (
            "runBlueprint",
            {"id": "bp-headcount", "slot_bindings": {"department": "dbpcm_warehouse.employee"}},
            "Running headcount over dbpcm_warehouse.employee",
            "running a saved analysis",
        ),
        (
            "searchBlueprints",
            {"query": "overtime from payroll_detail table"},
            "Searching blueprints over payroll_detail",
            "looking for a matching saved analysis",
        ),
        (
            "recordAssumptions",
            {"assumptions": ["Used employee.AnnualSalary for pay"]},
            "Noting that employee.AnnualSalary was used",
            "noting the assumptions behind the answer",
        ),
    )
    for tool, arguments, produced, expected in cases:
        line = await ProgressSummarizer(_FakeModelClient(produced)).summarize(tool, arguments)
        assert line == expected, produced


async def test_a_backtick_quoted_identifier_is_replaced() -> None:
    """Backticks defeat the dotted pattern entirely (`db`.`table`), and a progress
    line for a business reader has no reason to contain one at all."""
    for produced in (
        "Reading `dbpcm_warehouse`.`employee`",
        'Reading "dbpcm_warehouse"."employee"',
        "Counting rows in `employee`",
    ):
        line = await ProgressSummarizer(_FakeModelClient(produced)).summarize(
            "searchBlueprints", {"query": "headcount"}
        )
        assert line == "looking for a matching saved analysis", produced


async def test_a_sql_fragment_in_the_line_is_replaced() -> None:
    for produced in (
        "Running SELECT count() FROM employee",
        "Joining employee_master to the payroll rows",
        "Reading from EmployeeMaster",
    ):
        line = await ProgressSummarizer(_FakeModelClient(produced)).summarize(
            "searchBlueprints", {"query": "headcount"}
        )
        assert line == "looking for a matching saved analysis", produced


async def test_the_shape_check_leaves_ordinary_business_prose_alone() -> None:
    """The false-positive budget. A downgraded line costs the feature its whole
    point, so ordinary English — including a lower-case "from" before a plain
    word, and a decimal or a date — must pass through untouched.
    """
    for produced in (
        "Counting active staff for January",
        "Pulling headcount from last month",
        "Querying overtime pay by department for Q1.2026",
        "Comparing pay against the 4.5 percent target",
        "Looking up leave policy, e.g. carryover rules",
    ):
        line = await ProgressSummarizer(_FakeModelClient(produced)).summarize(
            "searchBlueprints", {"query": "headcount"}
        )
        assert line == produced, produced


def test_the_system_prompt_forbids_internal_identifiers() -> None:
    """The instruction half of the guard (the projection is the enforcing half)."""
    assert "MUST NOT contain SQL, table names, column names, database names" in _SYSTEM_PROMPT
    assert "plain business English" in _SYSTEM_PROMPT
    # The bounds the feature depends on are still stated.
    assert "max ~12 words" in _SYSTEM_PROMPT
    assert "No preamble" in _SYSTEM_PROMPT


def test_every_dispatchable_tool_has_a_static_fallback_line() -> None:
    """The fallback is per tool, so a tool with an allowlist entry (or one the
    loop dispatches at all) must have somewhere safe to fall back to."""
    for tool in _ARG_ALLOWLIST:
        assert tool in _STATIC_LINES, f"{tool} has no static fallback line"
    for tool in ("runQuery", "listTables", "getTableSchema", "listDatabases", "sampleRows"):
        assert tool in _STATIC_LINES
    # No static line may name a tool — the instant template label already does.
    for tool, line in _STATIC_LINES.items():
        assert tool not in line


def test_no_physical_identifier_argument_is_ever_allowlisted() -> None:
    """Derive-the-guard: the allowlist is the whole boundary, so the keys that
    carry physical structure must not appear in it for ANY tool."""
    for tool, allowed in _ARG_ALLOWLIST.items():
        for key in ("sql", "database", "table", "column", "columns", "tables"):
            assert key not in allowed, f"{tool} allowlists {key!r}"
