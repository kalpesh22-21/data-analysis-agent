"""Unit tests for `project_history` — the pure D44 read-surface projection
(UI Slice 3, docs/decisions/ui-slice3-history-lineage-contract.md §7).

HTTP-free / store-free: exercises the two D44 filters + the independent
messages↔trail join + the Slice-1 encodings over hand-built `TurnMessage`/
`TrailEntry` fixtures. Column scope is a `frozenset[str]` of
`"db.table.column"` (`[]`/`frozenset()` == allow-all, D80b).
"""

from __future__ import annotations

from data_agent.runtime.session.models import (
    PauseCheckpoint,
    ResultPreview,
    TrailEntry,
    TurnMessage,
)
from data_agent.runtime.session_history import project_history

# A shared provenance pair-set and its projected form.
_DEPT = ("hr.employees", "department")
_ID = ("hr.employees", "id")
_SALARY = ("hr.employees", "salary")

_ALLOW_ALL: frozenset[str] = frozenset()


def _user(turn_index: int, content: str) -> TurnMessage:
    return TurnMessage(turn_index=turn_index, role="user", content=content, ts="t")


def _assistant(
    turn_index: int, content: str, provenance: frozenset[tuple[str, str]] | None
) -> TurnMessage:
    return TurnMessage(
        turn_index=turn_index,
        role="assistant",
        content=content,
        ts="t",
        provenance=provenance,
    )


def _preview() -> ResultPreview:
    return ResultPreview(
        columns=["department", "headcount"],
        row_count=3,
        truncated=False,
        preview_rows=[["Engineering", 3], ["Sales", 3], ["Ops", 3]],
    )


def _run_query_entry(
    turn_index: int,
    sql: str,
    provenance: frozenset[tuple[str, str]] | None,
    *,
    status: str = "ok",
    preview: ResultPreview | None = None,
) -> TrailEntry:
    return TrailEntry(
        turn_index=turn_index,
        tool_call_id=f"c{turn_index}",
        tool_name="runQuery",
        args={"sql": sql},
        status=status,
        error_code=None,
        provenance=provenance,
        result_preview=preview,
        result_full_ref=None,
        ts="t",
    )


# --- allow-all: everything determined is kept, ordered by turn_index ----------


def test_allow_all_keeps_everything_ordered() -> None:
    messages = [
        _assistant(1, "second answer", frozenset({_ID})),
        _user(1, "second question"),
        _user(0, "first question"),
        _assistant(0, "first answer", frozenset({_DEPT, _ID})),
    ]
    trail = [
        _run_query_entry(1, "SELECT id FROM hr.employees", frozenset({_ID}), preview=_preview()),
        _run_query_entry(
            0, "SELECT department FROM hr.employees", frozenset({_DEPT}), preview=_preview()
        ),
    ]

    doc = project_history(messages, trail, _ALLOW_ALL, None)

    assert [t["turn_index"] for t in doc["turns"]] == [0, 1]
    first = doc["turns"][0]
    assert first["question"] == "first question"
    assert first["answer"] == "first answer"
    assert first["provenance_union"] == ["hr.employees.department", "hr.employees.id"]
    assert len(first["tool_calls"]) == 1
    tc = first["tool_calls"][0]
    assert tc["tool_name"] == "runQuery"
    assert tc["sql"] == "SELECT department FROM hr.employees"
    assert tc["provenance"] == ["hr.employees.department"]
    assert doc["pending_question"] is None


def test_result_table_equals_result_preview_to_doc() -> None:
    preview = _preview()
    trail = [_run_query_entry(0, "SELECT 1", frozenset({_DEPT}), preview=preview)]
    doc = project_history([_user(0, "q")], trail, _ALLOW_ALL, None)
    assert doc["turns"][0]["tool_calls"][0]["result_table"] == preview.to_doc()


# --- narrowed scope: out-of-scope past answer withheld, question kept ---------


def test_narrowed_scope_withholds_answer_but_keeps_question() -> None:
    # Answer derived from salary; caller's scope is department+id only.
    messages = [
        _user(0, "what is the salary?"),
        _assistant(0, "Jane earns $85,000.", frozenset({_SALARY})),
    ]
    scope = frozenset({"hr.employees.department", "hr.employees.id"})

    doc = project_history(messages, [], scope, None)

    turn = doc["turns"][0]
    assert turn["question"] == "what is the salary?"  # user msg always survives
    assert turn["answer"] is None
    assert turn["provenance_union"] is None
    assert turn["tool_calls"] == []


# --- independent filtering: out-of-scope tool-call omitted, in-scope sibling kept


def test_independent_filter_omits_out_of_scope_tool_keeps_sibling() -> None:
    # One turn, two tool calls: one in scope (department), one out (salary).
    # The assistant answer's union is department-only so it survives too.
    messages = [
        _user(0, "headcount by dept?"),
        _assistant(0, "Engineering 3, Sales 3.", frozenset({_DEPT})),
    ]
    trail = [
        _run_query_entry(
            0, "SELECT department FROM hr.employees", frozenset({_DEPT}), preview=_preview()
        ),
        _run_query_entry(
            0, "SELECT salary FROM hr.employees", frozenset({_SALARY}), preview=_preview()
        ),
    ]
    scope = frozenset({"hr.employees.department"})

    doc = project_history(messages, trail, scope, None)

    turn = doc["turns"][0]
    assert turn["answer"] == "Engineering 3, Sales 3."  # answer union in scope
    assert len(turn["tool_calls"]) == 1
    assert turn["tool_calls"][0]["sql"] == "SELECT department FROM hr.employees"


def test_answer_withheld_while_tool_call_survives() -> None:
    # Proves the reverse coupling: answer union is None (fail-closed) so the
    # answer is withheld, but an in-scope determined tool-call still surfaces.
    messages = [
        _user(0, "q"),
        _assistant(0, "leaky answer", None),  # None union → withheld even under allow-all
    ]
    trail = [_run_query_entry(0, "SELECT department FROM hr.employees", frozenset({_DEPT}))]

    doc = project_history(messages, trail, _ALLOW_ALL, None)

    turn = doc["turns"][0]
    assert turn["answer"] is None
    assert turn["provenance_union"] is None
    assert len(turn["tool_calls"]) == 1  # sibling survives — independent filtering


# --- None provenance dropped even under allow-all (fail-closed) ---------------


def test_none_provenance_dropped_under_allow_all() -> None:
    messages = [
        _user(0, "q"),
        _assistant(0, "undetermined answer", None),
    ]
    # A denied/errored entry carries None provenance.
    trail = [
        _run_query_entry(0, "SELECT * FROM secret", None, status="error"),
    ]

    doc = project_history(messages, trail, _ALLOW_ALL, None)

    turn = doc["turns"][0]
    assert turn["question"] == "q"
    assert turn["answer"] is None  # None-union assistant withheld
    assert turn["tool_calls"] == []  # None-provenance entry omitted


# --- pure-chat turn -----------------------------------------------------------


def test_pure_chat_turn() -> None:
    messages = [
        _user(0, "hello"),
        _assistant(0, "Hi! Ask me a question.", frozenset()),  # determined-empty
    ]

    doc = project_history(messages, [], _ALLOW_ALL, None)

    turn = doc["turns"][0]
    assert turn["answer"] == "Hi! Ask me a question."
    assert turn["provenance_union"] == []  # frozenset() → []
    assert turn["tool_calls"] == []


# --- runQuery sql inline vs runBlueprint sql null -----------------------------


def test_run_query_sql_inline_run_blueprint_sql_null() -> None:
    run_query = _run_query_entry(0, "SELECT department FROM hr.employees", frozenset({_DEPT}))
    run_blueprint = TrailEntry(
        turn_index=0,
        tool_call_id="bp1",
        tool_name="runBlueprint",
        args={"id": "bp-avg", "slot_bindings": {"department": "Sales"}, "sql": "should be ignored"},
        status="ok",
        error_code=None,
        provenance=frozenset({_DEPT}),
        result_preview=_preview(),
        result_full_ref="result::abc",
        ts="t",
    )

    doc = project_history([_user(0, "q")], [run_query, run_blueprint], _ALLOW_ALL, None)

    calls = doc["turns"][0]["tool_calls"]
    assert calls[0]["tool_name"] == "runQuery"
    assert calls[0]["sql"] == "SELECT department FROM hr.employees"
    # §0 YELLOW-1: runBlueprint node SQL is behind a KV ref → null, even though a
    # stray "sql" arg is present. Provenance + result_table still surface.
    assert calls[1]["tool_name"] == "runBlueprint"
    assert calls[1]["sql"] is None
    assert calls[1]["provenance"] == ["hr.employees.department"]
    assert calls[1]["result_table"] == _preview().to_doc()


# --- pending_question mirrors an unconsumed pause checkpoint -------------------


def test_pending_question_mirrors_unconsumed_checkpoint() -> None:
    pause = PauseCheckpoint(
        reason="askUser",
        pending_question={"question": "Which department?", "options": None},
        awaiting="user_answer",
        consumed=False,
    )
    doc = project_history([_user(0, "payroll?")], [], _ALLOW_ALL, pause)
    assert doc["pending_question"] == {"question": "Which department?", "options": None}


def test_pending_question_null_when_consumed() -> None:
    pause = PauseCheckpoint(
        reason="askUser",
        pending_question={"question": "Which department?", "options": None},
        awaiting="user_answer",
        consumed=True,
    )
    doc = project_history([_user(0, "payroll?")], [], _ALLOW_ALL, pause)
    assert doc["pending_question"] is None


def test_empty_session_yields_no_turns() -> None:
    doc = project_history([], [], _ALLOW_ALL, None)
    assert doc == {"turns": [], "pending_question": None}


# --- GAP: an orphan trail entry (turn_index with no user question) is dropped --


def test_orphan_trail_entry_with_no_user_message_is_dropped() -> None:
    # Turns are anchored on the user question (turn 0); a trail entry at turn 5
    # has no matching user message, so it has no turn to hang on. It must be
    # silently dropped — turns[] never invents a turn from a bare trail entry.
    messages = [_user(0, "q0"), _assistant(0, "a0", frozenset({_DEPT}))]
    trail = [
        _run_query_entry(0, "SELECT department FROM hr.employees", frozenset({_DEPT})),
        _run_query_entry(5, "SELECT id FROM hr.employees", frozenset({_ID})),  # orphan
    ]

    doc = project_history(messages, trail, _ALLOW_ALL, None)

    assert [t["turn_index"] for t in doc["turns"]] == [0]  # no phantom turn 5
    assert len(doc["turns"][0]["tool_calls"]) == 1  # only turn 0's own entry


# --- GAP: scratch-table columns are session-gated exempt, kept under narrow scope


def test_scratch_table_columns_kept_under_narrowed_scope() -> None:
    # A turn that read a scratch-table column: scratch pairs are session-gated,
    # not scope-gated (`is_provenance_in_scope` skips `scratch.` prefixes), so a
    # narrowed warehouse scope must NOT drop them. Answer + tool-call survive.
    scratch = ("scratch.tmp_agg", "headcount")
    messages = [_user(0, "from my scratch table?"), _assistant(0, "42 rows.", frozenset({scratch}))]
    trail = [_run_query_entry(0, "SELECT headcount FROM scratch.tmp_agg", frozenset({scratch}))]
    scope = frozenset({"hr.employees.department"})  # narrow, excludes scratch

    doc = project_history(messages, trail, scope, None)

    turn = doc["turns"][0]
    assert turn["answer"] == "42 rows."  # scratch-derived answer survives
    assert turn["provenance_union"] == ["scratch.tmp_agg.headcount"]
    assert len(turn["tool_calls"]) == 1
    assert turn["tool_calls"][0]["sql"] == "SELECT headcount FROM scratch.tmp_agg"


def test_scratch_mixed_with_out_of_scope_warehouse_column_is_dropped() -> None:
    # A tool-call whose provenance mixes a scratch pair (exempt) with an
    # out-of-scope warehouse pair (salary) is still dropped — the warehouse pair
    # fails the allowlist, so exemption is per-pair, not per-entry.
    scratch = ("scratch.tmp_agg", "headcount")
    messages = [_user(0, "q")]
    trail = [
        _run_query_entry(
            0, "SELECT headcount, salary FROM ...", frozenset({scratch, _SALARY})
        )
    ]
    scope = frozenset({"hr.employees.department"})

    doc = project_history(messages, trail, scope, None)

    assert doc["turns"][0]["tool_calls"] == []  # out-of-scope warehouse pair sinks it


# ---------------------------------------------------------------------------
# `answer_sql` on a history turn — so a RELOADED transcript can page the same
# table the live turn showed, instead of degrading to a static preview.
# ---------------------------------------------------------------------------

_AWT_SQL = "SELECT department_code, department_name FROM dbpcm_warehouse.department"


def _awt_entry(turn: int, **args) -> TrailEntry:
    return TrailEntry(
        turn_index=turn, tool_call_id=f"awt{turn}", tool_name="answerWithTable",
        args=dict(args), status="ok", error_code=None, provenance=frozenset(),
        result_preview=None, result_full_ref=None, ts="t9",
    )


def _turn_messages(turn: int = 0) -> list[TurnMessage]:
    return [
        TurnMessage(turn_index=turn, role="user", content="q?", ts="t0", provenance=frozenset()),
        TurnMessage(turn_index=turn, role="assistant", content="a", ts="t8", provenance=frozenset()),
    ]


def test_history_exposes_a_raw_sql_designation() -> None:
    body = project_history(_turn_messages(), [_awt_entry(0, answer="x", sql=_AWT_SQL)], frozenset(), None)
    assert body["turns"][0]["answer_sql"] == _AWT_SQL


def test_history_resolves_a_blueprint_designation_via_the_supplied_map() -> None:
    """The live model designates with `sql=""` beside `blueprint_id`. Reading only
    `args["sql"]` would report `answer_sql: null` for every blueprint-answered turn —
    precisely the turns where the model was told not to copy the SQL."""
    terminal = "SELECT department, headcount FROM dbpcm_warehouse.headcount_by_dept"
    body = project_history(
        _turn_messages(),
        [_awt_entry(0, answer="x", sql="", blueprint_id="bp-headcount")],
        frozenset(),
        None,
        blueprint_terminal_sql={"bp-headcount": terminal},
    )
    assert body["turns"][0]["answer_sql"] == terminal


def test_an_unresolvable_blueprint_designation_reports_null_not_an_error() -> None:
    """A blueprint whose result_full ref expired leaves the id unresolved. The turn
    still renders — it just has no table, exactly as before this field existed."""
    body = project_history(
        _turn_messages(),
        [_awt_entry(0, answer="x", blueprint_id="bp-gone")],
        frozenset(),
        None,
        blueprint_terminal_sql={},
    )
    assert body["turns"][0]["answer_sql"] is None
    assert body["turns"][0]["answer"] == "a"


def test_answer_sql_is_withheld_with_the_answer() -> None:
    """It is derived from the answer, so it inherits the answer's scope treatment:
    if the assistant message is dropped by the scope filter, the query behind it goes
    too. Leaking it would hand back a runnable query for an answer just withheld."""
    messages = [
        TurnMessage(turn_index=0, role="user", content="q?", ts="t0", provenance=frozenset()),
        TurnMessage(
            turn_index=0, role="assistant", content="a", ts="t8",
            provenance=frozenset({("dbpcm_warehouse.employee", "AnnualSalary")}),
        ),
    ]
    body = project_history(
        messages, [_awt_entry(0, answer="x", sql=_AWT_SQL)],
        frozenset({"dbpcm_warehouse.employee.EmployeeCode"}), None,
    )
    turn = body["turns"][0]
    assert turn["answer"] is None          # withheld by scope
    assert turn["answer_sql"] is None      # …and so is the query behind it


def test_a_turn_with_no_designation_reports_null() -> None:
    body = project_history(_turn_messages(), [], frozenset(), None)
    assert body["turns"][0]["answer_sql"] is None
