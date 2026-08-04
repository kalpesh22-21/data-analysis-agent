"""Layer-1: SemanticCatalogHandle — the F3 grain/measures/temporal runtime view.

Built from the full Semantic Catalog overlay; read-only + immutable; surfaces the
per-table grain the D56 verify gate needs (payroll grain_verifiable:false vs
employee grain:[EmployeeCode]).
"""

from __future__ import annotations

import pytest

from data_agent.runtime.provenance.catalog_handle import SemanticCatalogHandle
from tests._catalog_fixture import fixture_semantic_catalog_handle

_EMP = {
    "grain": ["EmployeeCode"],
    "temporal": {"dimensions": [], "default_pin": None},
}
_PAYROLL = {
    "grain": [],
    "grain_verifiable": False,
    "temporal": {"default_pin": "pay_period"},
    "measures": {
        "gross_earnings": {"column": "Amount", "agg": "sum", "defined_over": "RegisterType='EARN'"},
    },
}


def _handle() -> SemanticCatalogHandle:
    return SemanticCatalogHandle(
        {"dbpcm_warehouse.employee": _EMP, "dbpcm_warehouse.payroll": _PAYROLL}
    )


def test_employee_grain_verifiable_true_by_default() -> None:
    h = _handle()
    emp = h.grain_for("dbpcm_warehouse.employee")
    assert emp.grain == ("EmployeeCode",)
    assert emp.grain_verifiable is True
    assert h.is_grain_verifiable("dbpcm_warehouse.employee") is True


def test_payroll_grain_unverifiable() -> None:
    h = _handle()
    pay = h.grain_for("dbpcm_warehouse.payroll")
    assert pay.grain == ()
    assert pay.grain_verifiable is False
    assert h.is_grain_verifiable("dbpcm_warehouse.payroll") is False


def test_measures_parsed_with_agg_and_defined_over() -> None:
    measure = _handle().grain_for("dbpcm_warehouse.payroll").measures["gross_earnings"]
    assert measure.column == "Amount"
    assert measure.agg == "sum"
    assert "EARN" in measure.defined_over


def test_temporal_surfaced() -> None:
    assert _handle().grain_for("dbpcm_warehouse.payroll").temporal["default_pin"] == "pay_period"


def test_uncatalogued_table_returns_none() -> None:
    assert _handle().grain_for("dbpcm_warehouse.nope") is None
    assert _handle().is_grain_verifiable("dbpcm_warehouse.nope") is False


def test_handle_is_read_only() -> None:
    h = _handle()
    with pytest.raises(TypeError):
        h.tables["x"] = None  # type: ignore[index]


def test_load_from_real_catalog() -> None:
    # The real overlay (from the committed MCP export fixture, D75 Wave 1b): both
    # employee and payroll declare a verifiable grain in the Wave-1 catalog (payroll
    # now carries an explicit line-item grain), and payroll retains its measures.
    h = fixture_semantic_catalog_handle()
    assert h.is_grain_verifiable("dbpcm_warehouse.employee") is True
    assert h.is_grain_verifiable("dbpcm_warehouse.payroll") is True
    assert "gross_earnings" in h.grain_for("dbpcm_warehouse.payroll").measures
