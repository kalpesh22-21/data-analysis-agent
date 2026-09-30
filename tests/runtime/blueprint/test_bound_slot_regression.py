"""Concrete bindings must not be lost outside a capped domain enumeration."""

import pytest

from data_agent.runtime.blueprint.executor import BlueprintExecutor, ExecCompleted
from data_agent.runtime.blueprint.tool import RunBlueprintTool
from data_agent.runtime.dispatch.tool_dispatcher import ToolResult
from tests.runtime.blueprint.test_executor import (
    FakeDispatcher,
    _creds,
    _detail,
    _index,
    _ok_result,
    _rq,
)

TABLE = "dbpcm_warehouse.payroll"
PROVENANCE = frozenset(
    (TABLE, c)
    for c in ("employee_code", "pay_period_end_date", "register_type", "type_code", "amount")
)


def detail():
    return _detail(
        bid="bp-employee-check-detail-for-period",
        slots=[
            {
                "name": "employee",
                "type": "string",
                "required": True,
                "binds_to": TABLE + ".employee_code",
            },
            {
                "name": "period",
                "type": "period",
                "required": True,
                "binds_to": TABLE + ".pay_period_end_date",
            },
        ],
        uses=frozenset(f"{t}.{c}" for t, c in PROVENANCE),
        sql_template="SELECT register_type, type_code, sum(amount) AS amount FROM dbpcm_warehouse.payroll WHERE employee_code = {employee} AND pay_period_end_date = {period} GROUP BY register_type, type_code",
        result_grain=["register_type", "type_code"],
    )


@pytest.mark.parametrize("period_result", ["2026-08-08", "2026-08-08 00:00:00"])
async def test_bound_employee_and_period_execute_the_template_after_targeted_verification(
    period_result,
):
    bp = detail()
    dispatcher = FakeDispatcher(
        [
            _ok_result(PROVENANCE, _rq(["employee_code"], [["A4I8"]])),
            _ok_result(PROVENANCE, _rq(["pay_period_end_date"], [[period_result]])),
            _ok_result(
                PROVENANCE, _rq(["register_type", "type_code", "amount"], [["R", "REG", 12]])
            ),
            _ok_result(PROVENANCE, _rq(["__bp_n", "__bp_d"], [[1, 1]])),
        ]
    )
    executor = BlueprintExecutor(tool_dispatcher=dispatcher, vector_index=_index(bp))
    outcome = await executor.execute(
        blueprint_id=bp.id,
        slot_bindings={"employee": "A4I8", "period": "2026-08-08"},
        credentials=_creds(),
    )
    assert isinstance(outcome, ExecCompleted)
    assert "WHERE" in dispatcher.calls[0].sql and "'a4i8'" in dispatcher.calls[0].sql
    assert "WHERE" in dispatcher.calls[1].sql and "'2026-08-08'" in dispatcher.calls[1].sql
    template = dispatcher.calls[2].sql
    assert "employee_code = 'A4I8'" in template
    assert "pay_period_end_date = '2026-08-08'" in template
    assert "GROUP BY register_type, type_code" in template
    assert outcome.result_full["preview_rows"] == [["R", "REG", 12]]


async def test_failed_resolution_is_tool_error_not_ok_or_a_substitute_result():
    bp = detail()
    dispatcher = FakeDispatcher(
        [
            ToolResult(
                status="error",
                tool_name="runQuery",
                error_code="API_PROVENANCE_INVALID",
                retryable=False,
                user_message="Slot lookup API failure",
                provenance=None,
                result_preview=None,
                result_full=None,
            )
        ]
    )
    tool = RunBlueprintTool(
        executor=BlueprintExecutor(tool_dispatcher=dispatcher, vector_index=_index(bp))
    )
    result = await tool.run(
        {"id": bp.id, "slot_bindings": {"employee": "A4I8", "period": "2026-08-08"}}, _creds()
    )
    assert result.status == "error"
    assert result.error_code == "API_PROVENANCE_INVALID"
    assert result.result_full is None and result.pause is None
    assert len(dispatcher.calls) == 1
