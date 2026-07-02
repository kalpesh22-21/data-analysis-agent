"""Layer-1: Slice-B REVIEW fixes at the executor READ side (runblueprint-design,
reviewer B3(b) + S3 + S-read-only) — the backstops for a POISONED/legacy stored
record the write-time loader gate never saw.

  - B3(b): a resolved slot whose `{token}` the template does NOT reference ⇒
    RUN_BLUEPRINT_SLOT_INVALID, NEVER a silent filter drop (the D56 wrong-answer
    class — a query without the user's filter that still passes the grain gate).
  - S3: >16 slots on a stored record ⇒ BlueprintParseError → UNSUPPORTED (never an
    unbounded number of inner probes).
  - S-read-only: a stored template that PARSES but is a DDL/DML/multi-statement
    construct ⇒ UNSUPPORTED before any dispatch (defense-in-depth over the loader).
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import (
    SLOT_INVALID_CODE,
    UNSUPPORTED_CODE,
    BlueprintExecutor,
    ExecFailed,
)
from data_agent.runtime.blueprint.models import Blueprint, BlueprintParseError
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle(
    {_E: {"EmployeeCode": "String", "Department": "Nullable(String)", "AnnualSalary": "Nullable(Float64)"}}
)


def _creds() -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s", jwt="jwt", column_scope=frozenset())


def _detail(*, slots: list[dict[str, Any]], sql_template: str, result_grain: Any = None) -> BlueprintDetail:
    return BlueprintDetail(
        id="bp-x",
        intent="x",
        slots_summary="",
        uses=frozenset({f"{_E}.Department", f"{_E}.EmployeeCode", f"{_E}.AnnualSalary"}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        slots=slots,
        sql_template=sql_template,
        result_grain=result_grain if result_grain is not None else [],
    )


def _executor(mcp: FakeMCPClient, detail: BlueprintDetail) -> BlueprintExecutor:
    idx = FakeVectorIndex()
    idx.add_detail(detail)
    return BlueprintExecutor(tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=idx)


# -- B3(b): unreferenced resolved slot never silently drops the filter ------


async def test_resolved_unreferenced_slot_is_slot_invalid_not_silent_drop() -> None:
    # `department` resolves fine, but the (poisoned) template does NOT reference it —
    # binding it away would run a company-wide query that still passes the grain gate.
    # The executor MUST fail-closed to SLOT_INVALID, never dispatch the filterless SQL.
    detail = _detail(
        slots=[{"name": "department", "type": "string", "required": True}],
        sql_template="SELECT Department AS department FROM dbpcm_warehouse.employee",  # no {department}
    )
    mcp = FakeMCPClient(scripted={"runQuery": []})  # nothing may dispatch
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-x", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == SLOT_INVALID_CODE
    assert mcp.calls == []  # the filterless query was NEVER run


# -- S3: slot cap on a stored record ----------------------------------------


def test_models_slot_cap_rejects_over_sixteen() -> None:
    many = [{"name": f"s{i}", "type": "string", "required": False} for i in range(17)]
    try:
        Blueprint.parse(id="bp", intent="x", slots=many, sql_template="SELECT 1")
    except BlueprintParseError as exc:
        assert "16" in str(exc) or "slot" in str(exc).lower()
    else:  # pragma: no cover
        raise AssertionError("expected BlueprintParseError for >16 slots")


async def test_stored_record_with_too_many_slots_is_unsupported() -> None:
    many = [{"name": f"s{i}", "type": "string", "required": False} for i in range(17)]
    detail = _detail(slots=many, sql_template="SELECT Department FROM dbpcm_warehouse.employee")
    mcp = FakeMCPClient(scripted={"runQuery": []})
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-x", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == UNSUPPORTED_CODE
    assert mcp.calls == []


# -- S-read-only: a parsing-but-non-read-only stored template ---------------


async def test_stored_ddl_template_is_unsupported_never_dispatched() -> None:
    # A poisoned READ record whose template PARSES but is a DROP (or a multi-statement
    # block) ⇒ UNSUPPORTED before any resolve/probe/dispatch (defense-in-depth).
    detail = _detail(
        slots=[],
        sql_template="DROP TABLE dbpcm_warehouse.employee",
    )
    mcp = FakeMCPClient(scripted={"runQuery": []})
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-x", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == UNSUPPORTED_CODE
    assert mcp.calls == []
