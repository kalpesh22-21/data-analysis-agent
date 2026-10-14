"""Ingestion timestamps stay tied to the serving execution through every projection."""

from dataclasses import replace

import pytest

from data_agent.runtime.blueprint.executor import (
    BlueprintExecutor,
    ExecCompleted,
    _dumps_completed,
    _node_record,
    _rehydrate_completed,
)
from data_agent.runtime.blueprint.tool import blueprint_outcome_to_tool_result
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.provenance.capture import capture_data_as_of, union_data_as_of
from data_agent.runtime.session.models import TrailEntry
from data_agent.runtime.session_history import project_history
from tests.runtime.blueprint.test_executor import (
    FakeDispatcher,
    _creds,
    _detail,
    _index,
    _ok_result,
    _rq,
)
from tests.runtime.dispatch.test_tool_dispatcher import CATALOG, _credentials
from tests.runtime.test_query_page import HEADERS, _client
from tests.runtime.test_session_history import _assistant, _run_query_entry, _user

OLDER = "2026-10-12T06:00:00.000Z"
NEWER = "2026-10-13T06:00:00.000Z"


@pytest.mark.parametrize(
    "value",
    [None, {}, 123, "garbage", "2026-10-13", "2026-10-13T06:00:00", "2026-02-30T00:00:00Z", True],
)
def test_invalid_timestamp_is_unknown(value):
    assert capture_data_as_of("runQuery", {"data_as_of": value}) is None


@pytest.mark.parametrize("tool", ["runQuery", "sampleRows", "explainQuery"])
def test_capture_normalizes_timezone(tool):
    assert capture_data_as_of(tool, {"data_as_of": "2026-10-13T01:00:00-05:00"}) == NEWER
    assert capture_data_as_of(tool, {}) is None
    assert capture_data_as_of(tool, NEWER) is None


def test_metadata_tools_cannot_claim_freshness():
    assert capture_data_as_of("getTableSchema", {"data_as_of": NEWER}) is None


@pytest.mark.parametrize(
    "values,expected",
    [([], None), ([NEWER, OLDER], OLDER), ([NEWER, None], None), ([NEWER, "garbage"], None)],
)
def test_union_is_fail_closed(values, expected):
    assert union_data_as_of(values) == expected


async def test_dispatch_extracts_and_strips_metadata():
    raw = {
        "columns": ["EmployeeCode"],
        "rows": [["E1"]],
        "row_count": 1,
        "truncated": False,
        "provenance": {"version": 1, "columns": [["dbpcm_warehouse.employee", "EmployeeCode"]]},
    }
    mcp = FakeMCPClient(scripted={"runQuery": [raw]}, data_as_of=NEWER)
    result = await ToolDispatcher(mcp, CATALOG).dispatch(
        "runQuery", {"sql": "SELECT EmployeeCode FROM dbpcm_warehouse.employee"}, _credentials()
    )
    assert result.status == "ok"
    assert result.data_as_of == NEWER
    assert "data_as_of" not in result.result_full
    assert "provenance" not in result.result_full
    assert NEWER not in str(result.result_preview)


def test_trail_roundtrip_and_legacy_identity():
    entry = _run_query_entry(0, "SELECT 1", frozenset())
    legacy = entry.to_doc()
    assert "data_as_of" not in legacy
    assert TrailEntry.from_doc(legacy).to_doc() == legacy
    fresh = replace(entry, data_as_of=NEWER)
    assert TrailEntry.from_doc(fresh.to_doc()) == fresh
    assert TrailEntry.from_doc({**legacy, "data_as_of": "bad"}).data_as_of is None


@pytest.mark.parametrize(
    "first,second,expected",
    [(OLDER, NEWER, OLDER), (NEWER, OLDER, OLDER), (None, NEWER, None), (OLDER, None, None)],
)
async def test_blueprint_aggregates_nodes(first, second, expected):
    detail = _detail(
        sql_template=None,
        result_grain=[],
        composes=[
            {
                "order": 0,
                "output": {"n": "scalar"},
                "sql_template": "SELECT count() AS n FROM dbpcm_warehouse.employee",
            },
            {
                "order": 1,
                "feeds_from": [0],
                "consumes": {"n": "$0.n"},
                "sql_template": "SELECT {n} AS n",
            },
        ],
    )
    dispatcher = FakeDispatcher(
        [
            replace(_ok_result(frozenset(), _rq(["n"], [[2]])), data_as_of=first),
            replace(_ok_result(frozenset(), _rq(["n"], [[2]])), data_as_of=second),
        ]
    )
    outcome = await BlueprintExecutor(
        tool_dispatcher=dispatcher, vector_index=_index(detail)
    ).execute(blueprint_id=detail.id, slot_bindings={}, credentials=_creds())
    assert isinstance(outcome, ExecCompleted)
    assert outcome.data_as_of == expected
    assert blueprint_outcome_to_tool_result(outcome).data_as_of == expected


def test_blueprint_checkpoint_retains_timestamp():
    running = {0: _node_record({"n": 2}, frozenset(), "SELECT 2", data_as_of=OLDER)}
    assert _rehydrate_completed(_dumps_completed(running)) == running
    legacy = {0: _node_record({"n": 2}, frozenset(), "SELECT 2")}
    assert _rehydrate_completed(_dumps_completed(legacy)) == legacy


def test_history_uses_selected_execution_even_when_sql_is_identical():
    sql = "SELECT id FROM hr.employees"
    old = replace(_run_query_entry(0, sql, frozenset()), tool_call_id="old", data_as_of=OLDER)
    new = replace(old, tool_call_id="new", data_as_of=NEWER)
    designation = replace(
        old,
        tool_call_id="answer",
        tool_name="answerWithTable",
        args={"tables": [{"result_id": "old", "caption": "Employees"}]},
        data_as_of=None,
        answer_table_provenance=(frozenset(),),
    )
    history = project_history(
        [_user(0, "Employees"), _assistant(0, "Here", frozenset())],
        [old, new, designation],
        frozenset(),
        None,
    )
    turn = history["turns"][0]
    assert turn["answer_tables"][0]["data_as_of"] == OLDER
    assert [c.get("data_as_of") for c in turn["tool_calls"]] == [OLDER, NEWER, None]


@pytest.mark.parametrize("stamp", [NEWER, None])
def test_page_uses_fresh_dispatch_timestamp(monkeypatch, stamp):
    raw = {"columns": ["n"], "rows": [[1]], "row_count": 1, "truncated": False}
    if stamp:
        raw["data_as_of"] = stamp
    client, _ = _client(monkeypatch, scripted={"runQuery": [raw]})
    response = client.post("/query/page", json={"sql": "SELECT 1 AS n"}, headers=HEADERS)
    assert response.status_code == 200
    if stamp:
        assert response.json()["data_as_of"] == stamp
    else:
        assert "data_as_of" not in response.json()


async def test_live_answer_and_replay_keep_serving_timestamp():
    from data_agent.runtime.app import _outcome_to_dict
    from tests.runtime.test_harness_improvements import CREDS, batch, build, call, discovery, run

    sql = 'SELECT count(Id) AS "Employee Count" FROM hr.employee'
    loop, store, _, _, _ = build(
        [
            discovery(),
            batch(call("runQuery", "q", sql=sql)),
            batch(
                call(
                    "finalizeAnswer",
                    "answer",
                    answer="There are 120 employees.",
                    tables=[{"result_id": "q", "caption": "Employees"}],
                    evidence=["q"],
                    capability_refs=[],
                )
            ),
        ],
        rows=[
            {"columns": ["Employee Count"], "rows": [[120]], "row_count": 1, "data_as_of": NEWER}
        ],
    )
    outcome = await run(loop, "Count employees and show the result.")
    wire = _outcome_to_dict(outcome)
    assert wire["answer_tables"][0]["data_as_of"] == NEWER
    doc = await store.get_or_create_session(CREDS.session_id)
    persisted = [TrailEntry.from_doc(e.to_doc()) for e in doc.tool_trail]
    history = project_history(doc.messages, persisted, CREDS.column_scope, None)["turns"][0]
    assert history["answer_tables"] == wire["answer_tables"]
    tables, _ = await loop._compute_turn_answer_tables(CREDS.session_id, 0)
    assert tables[0].data_as_of == NEWER


@pytest.mark.parametrize("before_pause", [OLDER, None])
async def test_blueprint_resume_includes_pre_pause_freshness(before_pause):
    from data_agent.runtime.blueprint.executor import ExecPaused
    from tests.runtime.blueprint.test_pause_resume_qa6 import _approval_detail, _executor

    initial = FakeMCPClient(scripted={"runQuery": [_rq(["n"], [[42]])]}, data_as_of=before_pause)
    paused = await _executor(initial, _approval_detail()).execute(
        blueprint_id="bp-pr", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(paused, ExecPaused)
    resumed = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department"], [["Sales"], ["Eng"]]),
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),
            ]
        },
        data_as_of=NEWER,
    )
    outcome = await _executor(resumed, _approval_detail()).resume(
        blueprint_id="bp-pr",
        slot_bindings={},
        completed_nodes_json=paused.completed_nodes_json,
        awaiting_node=1,
        approval_answer="approve",
        credentials=_creds(),
    )
    assert isinstance(outcome, ExecCompleted)
    assert outcome.data_as_of == before_pause
    assert len(resumed.calls) == 2


@pytest.mark.parametrize("stamp", [NEWER, None])
def test_blueprint_capture_preserves_optional_timestamp(stamp):
    from data_agent.runtime.loop.turn_accumulators import capture_terminal_sql

    result = replace(
        _ok_result(frozenset(), _rq(["n"], [[1]])),
        tool_name="runBlueprint",
        result_full={"blueprint_id": "bp", "terminal_sql": "SELECT 1"},
        data_as_of=stamp,
    )
    runs = {}
    capture_terminal_sql("runBlueprint", result, into=runs)
    assert runs["bp"].data_as_of == stamp


async def test_disabled_runtime_discards_incoming_metadata():
    raw = {
        "columns": ["n"],
        "rows": [[1]],
        "data_as_of": NEWER,
        "provenance": {"version": 1, "columns": []},
    }
    result = await ToolDispatcher(
        FakeMCPClient(scripted={"runQuery": [raw]}),
        CATALOG,
        data_as_of_enabled=False,
    ).dispatch("runQuery", {"sql": "SELECT 1 AS n"}, _credentials())
    assert result.status == "ok"
    assert result.data_as_of is None
    assert "data_as_of" not in result.result_full


def test_disabled_environment_hides_page_metadata(monkeypatch):
    from data_agent.runtime.config import RuntimeSettings

    monkeypatch.setenv("DATA_AS_OF_ENABLED", "false")
    assert RuntimeSettings(_env_file=None).data_as_of_enabled is False
    client, _ = _client(
        monkeypatch,
        scripted={
            "runQuery": [
                {"columns": ["n"], "rows": [[1]], "row_count": 1, "data_as_of": NEWER},
            ]
        },
    )
    result = client.post("/query/page", json={"sql": "SELECT 1 AS n"}, headers=HEADERS)
    assert result.status_code == 200
    assert "data_as_of" not in result.json()


def test_disabled_flag_hides_persisted_history_without_mutating_receipts():
    entry = replace(_run_query_entry(0, "SELECT 1", frozenset()), data_as_of=NEWER)
    designation = replace(
        entry,
        tool_call_id="answer",
        tool_name="answerWithTable",
        args={"tables": [{"result_id": entry.tool_call_id}]},
        answer_table_provenance=(frozenset(),),
    )
    history = project_history(
        [_user(0, "Count?"), _assistant(0, "One", frozenset())],
        [entry, designation],
        frozenset(),
        None,
        data_as_of_enabled=False,
    )
    turn = history["turns"][0]
    assert "data_as_of" not in turn["answer_tables"][0]
    assert all("data_as_of" not in call for call in turn["tool_calls"])
    assert entry.data_as_of == NEWER


async def test_disabled_flag_hides_resumed_outcome_in_sse():
    from data_agent.runtime.app import _stream_turn
    from data_agent.runtime.loop.agent_loop import TurnOutcome
    from data_agent.runtime.observability.progress import ProgressEmitter

    outcome = TurnOutcome(
        status="done",
        assistant_text="One",
        pending_question=None,
        tool_calls_made=0,
        answer_tables=[{"sql": "SELECT 1", "data_as_of": NEWER}],
    )

    async def call():
        return outcome

    events = [
        event async for event in _stream_turn(call, ProgressEmitter(), data_as_of_enabled=False)
    ]
    assert "event: result" in "".join(events)
    assert "data_as_of" not in "".join(events)
    assert outcome.answer_tables[0]["data_as_of"] == NEWER
