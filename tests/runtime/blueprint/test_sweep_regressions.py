"""Runtime regressions from the September 30 blueprint sweep."""

import pytest

from data_agent.corpus.seeds import BlueprintSeed, CorpusLoadError
from data_agent.runtime.blueprint.compiler import validate_blueprint_dag
from data_agent.runtime.blueprint.executor import BlueprintExecutor, ExecCompleted
from data_agent.runtime.blueprint.models import BlueprintParseError, SlotSpec
from data_agent.runtime.blueprint.slots import AskUser, SlotBinding, resolve_slot
from tests.runtime.blueprint.test_executor import (
    FakeDispatcher,
    _creds,
    _detail,
    _index,
    _ok_result,
    _rq,
)

T = "dbpcm_warehouse.employee"


def validate(sql):
    validate_blueprint_dag(
        BlueprintSeed(
            id="bp-sweep",
            intent="test",
            slots_summary="",
            uses=[
                f"{T}.{c}"
                for c in [
                    "employee_code",
                    "gender",
                    "hire_date",
                    "employee_status",
                    "department_name",
                ]
            ],
            sql_template=sql,
        )
    )


@pytest.mark.parametrize(
    "sql",
    [
        f'SELECT count(*) AS "Total" FROM {T}',
        f'''SELECT multiIf(gender = '', 'Unknown', gender) AS "Gender", count(*) AS "Total"
        FROM {T} GROUP BY "Gender" ORDER BY "Gender"''',
        f'''SELECT multiIf(yrs < 1, 'New', 'Old') AS "Tenure Band", count(*) AS "Total"
        FROM (SELECT dateDiff('day', hire_date, today()) / 365.25 AS yrs FROM {T})
        GROUP BY "Tenure Band"''',
        f"""SELECT "Quarter", "Terms", "Quarter Total" FROM (
        SELECT "Quarter", "Terms", sum("Terms") OVER (PARTITION BY "Quarter") AS "Quarter Total"
        FROM (SELECT toStartOfQuarter(hire_date) AS "Quarter", count(*) AS "Terms"
              FROM {T} GROUP BY "Quarter"))""",
        f'''SELECT department_name AS "Department Name", count(*) AS "Total"
        FROM {T} GROUP BY "Department Name"''',
    ],
)
def test_count_and_quoted_display_aliases_compile(sql):
    validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT * FROM {T}",
        f"SELECT e.* FROM {T} e",
        f"SELECT count(*) FROM (SELECT * FROM {T})",
        f'SELECT gender AS "Secret" FROM {T} WHERE "Secret" = 1',
        f'SELECT multiIf(secret = 1, 1, 0) AS "Secret Flag" FROM {T} GROUP BY "Secret Flag"',
        f"SELECT count(*) FROM {T} JOIN other.secret s ON s.id = employee_code",
    ],
)
def test_alias_and_count_support_do_not_hide_source_reads(sql):
    with pytest.raises(CorpusLoadError):
        validate(sql)


@pytest.mark.parametrize(
    "kind,lo,hi,value",
    [
        ("positive_integer", 1, 10000, 10000),
        ("positive_integer", 1, 365, 365),
        ("positive_integer", 1, 300, 300),
        ("nonnegative_integer", 0, 3650, 0),
    ],
)
def test_numeric_bounds_are_independent_of_temporal_ceiling(kind, lo, hi, value):
    spec = SlotSpec.parse({"name": "n", "type": kind, "min_value": lo, "max_value": hi})
    assert isinstance(resolve_slot(value, spec), SlotBinding)
    for invalid in (lo - 1, hi + 1, True, 1.5, "6 months"):
        assert isinstance(resolve_slot(invalid, spec), AskUser)


def test_relative_window_retains_its_ceiling():
    with pytest.raises(BlueprintParseError):
        SlotSpec.parse({"name": "n", "type": "relative_window", "max_value": 121})


async def test_enum_binding_skips_domain_query():
    detail = _detail(
        slots=[
            {
                "name": "status",
                "type": "enum",
                "required": True,
                "binds_to": f"{T}.employee_status",
                "enum_values": ["Active"],
            }
        ],
        sql_template=f"SELECT count(*) AS n FROM {T} WHERE employee_status = {{status}}",
        result_grain={"columns": [], "verifiable": False},
    )
    dispatcher = FakeDispatcher([_ok_result(frozenset(), _rq(["n"], [[0]]))])
    outcome = await BlueprintExecutor(
        tool_dispatcher=dispatcher, vector_index=_index(detail)
    ).execute(blueprint_id=detail.id, slot_bindings={"status": "Active"}, credentials=_creds())
    assert isinstance(outcome, ExecCompleted)
    assert len(dispatcher.calls) == 1
    assert "SELECT DISTINCT" not in dispatcher.calls[0].sql


async def test_grain_failure_names_declared_and_returned_columns():
    detail = _detail(
        sql_template=f'SELECT employee_code AS "Employee Code" FROM {T}', result_grain=["Employee"]
    )
    dispatcher = FakeDispatcher([_ok_result(frozenset(), _rq(["Employee Code"], []))])
    outcome = await BlueprintExecutor(
        tool_dispatcher=dispatcher, vector_index=_index(detail)
    ).execute(blueprint_id=detail.id, slot_bindings={}, credentials=_creds())
    assert outcome.error_code == "RUN_BLUEPRINT_VERIFY_FAILED"
    assert "Employee Code" in outcome.user_message
    assert "Declared grain: ['Employee']" in outcome.user_message
    assert len(dispatcher.calls) == 1


async def test_invalid_enum_does_not_probe_or_execute():
    detail = _detail(
        slots=[
            {
                "name": "status",
                "type": "enum",
                "required": True,
                "binds_to": f"{T}.employee_status",
                "enum_values": ["Active"],
            }
        ],
        sql_template=f"SELECT count(*) FROM {T} WHERE employee_status = {{status}}",
    )
    dispatcher = FakeDispatcher([])
    outcome = await BlueprintExecutor(
        tool_dispatcher=dispatcher, vector_index=_index(detail)
    ).execute(blueprint_id=detail.id, slot_bindings={"status": "A"}, credentials=_creds())
    assert outcome.reason == "blueprint_slot"
    assert dispatcher.calls == []
