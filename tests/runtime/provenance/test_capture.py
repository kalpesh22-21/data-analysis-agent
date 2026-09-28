"""The execution API owns provenance; the agent validates its wire shape."""

import pytest

from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.provenance.capture import capture_provenance


@pytest.mark.parametrize("tool", ["runQuery", "sampleRows", "explainQuery"])
def test_api_columns_include_dependencies_not_output_aliases(tool):
    result = {
        "columns": ["Total Pay"],
        "provenance": {
            "version": 1,
            "columns": [["hr.payroll", "GrossPay"], ["hr.payroll", "PayDate"]],
        },
    }
    assert capture_provenance(tool, result) == frozenset(
        {("hr.payroll", "GrossPay"), ("hr.payroll", "PayDate")}
    )


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        {},
        {"version": 2, "columns": []},
        {"version": True, "columns": []},
        {"version": 1, "columns": None},
        {"version": 1, "columns": ["hr.payroll.GrossPay"]},
        {"version": 1, "columns": [["payroll", "GrossPay"]]},
        {"version": 1, "columns": [["hr.payroll", ""]]},
    ],
)
def test_missing_or_malformed_receipt_is_api_error(receipt):
    with pytest.raises(MCPToolError) as error:
        capture_provenance("runQuery", {"rows": [[123]], "provenance": receipt})
    assert error.value.code == "API_PROVENANCE_INVALID"
    assert "123" not in error.value.message


def test_empty_provenance_is_valid_for_constant_result():
    assert (
        capture_provenance("runQuery", {"provenance": {"version": 1, "columns": []}}) == frozenset()
    )


@pytest.mark.parametrize(
    "tool", ["getTableSchema", "listTables", "listDatabases", "searchKnowledge", "searchBlueprints"]
)
def test_metadata_tools_do_not_require_data_provenance(tool):
    assert capture_provenance(tool, {}) == frozenset()


def test_unknown_tool_does_not_imply_empty_provenance():
    assert capture_provenance("futureTool", {}) is None
