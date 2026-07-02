"""QA6 Layer-1: scalar-passing injection — the KEY Slice-C attack surface.

A node's SCALAR output is DATA (an untrusted warehouse cell) that becomes a
downstream node's bound param (runblueprint-design §2.4/§3.3, F1/D10). This suite
hammers that seam: an adversarial scalar STRING (SQL, quotes, backslash, unicode,
a `{token}`, 1 MB) must bind as exactly ONE ClickHouse-escaped string literal in
the downstream node SQL — never as SQL, never re-scanned as a `{slot}` token — and
a numeric / null scalar must type-correctly or fail-closed.

All fakes; the executor's real bind path (`template.bind_template`) is under test.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlglot
from sqlglot import exp

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import (
    SLOT_INVALID_CODE,
    BlueprintExecutor,
    ExecCompleted,
    ExecFailed,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle(
    {
        _E: {
            "EmployeeCode": "String",
            "Department": "Nullable(String)",
            "AnnualSalary": "Nullable(Float64)",
            "StatusCode": "Nullable(String)",
        }
    }
)


def _creds() -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s-qa6", jwt="jwt", column_scope=frozenset())


def _rq(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


def _detail(composes: list[dict[str, Any]], result_grain: Any) -> BlueprintDetail:
    return BlueprintDetail(
        id="bp-inj",
        intent="scalar injection probe",
        slots_summary="",
        uses=frozenset({f"{_E}.Department", f"{_E}.AnnualSalary", f"{_E}.EmployeeCode"}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        composes=composes,
        result_grain=result_grain,
    )


def _executor(mcp: FakeMCPClient, detail: BlueprintDetail) -> BlueprintExecutor:
    index = FakeVectorIndex()
    index.add_detail(detail)
    return BlueprintExecutor(tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=index)


# A string-scalar DAG: node 0 emits a scalar `tok` (a Department cell); node 1
# consumes it into a `WHERE Department = {tok}` predicate and is the terminal.
def _string_scalar_dag() -> BlueprintDetail:
    return _detail(
        composes=[
            {
                "order": 0,
                "output": {"tok": "scalar"},
                "sql_template": "SELECT Department AS tok FROM dbpcm_warehouse.employee LIMIT 1",
            },
            {
                "order": 1,
                "feeds_from": [0],
                "consumes": {"tok": "$0.tok"},
                "sql_template": (
                    "SELECT Department AS department FROM dbpcm_warehouse.employee "
                    "WHERE Department = {tok} GROUP BY Department"
                ),
                "output": {},
            },
        ],
        result_grain=["Department"],
    )


def _string_literals(sql: str) -> list[str]:
    """Every string-literal VALUE in *sql*, parsed structurally (not by regex)."""
    tree = sqlglot.parse_one(sql, dialect="clickhouse")
    return [
        node.this
        for node in tree.walk()
        if isinstance(node, exp.Literal) and node.args.get("is_string")
    ]


# The adversarial scalar payloads. Each must survive as ONE literal downstream.
_HOSTILE_SCALARS = [
    "1); DROP TABLE employee;--",
    "x' OR '1'='1",
    "a'; DELETE FROM employee WHERE '1'='1",
    "back\\slash\\path",
    "quote\"double\"quote",
    "café ☃ Ω 你好",           # unicode must pass through intact
    "{company_avg}",           # a brace token must NOT be re-read as a {slot}
    "{tok}",                   # even its OWN placeholder name is inert data
    "' UNION SELECT SSN FROM secretpayroll --",
    "Sales'--",
]


@pytest.mark.parametrize("payload", _HOSTILE_SCALARS)
async def test_hostile_string_scalar_binds_as_one_escaped_literal(payload: str) -> None:
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["tok"], [[payload]]),        # node 0 → adversarial scalar
                _rq(["department"], [["Sales"]]),  # node 1 terminal
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),
            ]
        }
    )
    outcome = await _executor(mcp, _string_scalar_dag()).execute(
        blueprint_id="bp-inj", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    node1_sql = mcp.calls[1].args["sql"]
    # The payload appears as EXACTLY ONE string literal — round-tripped verbatim,
    # never split into SQL tokens. sqlglot re-parses the bound SQL and the literal
    # value equals the untrusted payload byte-for-byte.
    literals = _string_literals(node1_sql)
    assert payload in literals, f"payload not a single literal in: {node1_sql!r}"
    # Structural proof the bind site was fully consumed: NO unbound placeholder
    # node survives (a brace-bearing VALUE like `{tok}` is inert data inside the
    # literal, never re-read as a new bind site).
    bound_tree = sqlglot.parse_one(node1_sql, dialect="clickhouse")
    assert not any(isinstance(n, exp.Placeholder) for n in bound_tree.walk())
    # The bound SQL is still a single well-formed statement (no smuggled second
    # statement rode through the literal).
    assert len(sqlglot.parse(node1_sql, dialect="clickhouse")) == 1


async def test_one_megabyte_scalar_binds_without_crash() -> None:
    payload = "A" * (1024 * 1024)  # 1 MB string cell
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["tok"], [[payload]]),
                _rq(["department"], [["Sales"]]),
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),
            ]
        }
    )
    outcome = await _executor(mcp, _string_scalar_dag()).execute(
        blueprint_id="bp-inj", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    assert payload in _string_literals(mcp.calls[1].args["sql"])


async def test_numeric_scalar_binds_as_a_number_literal_not_quoted() -> None:
    # A numeric cell binds as a NUMBER literal into a numeric comparison — no quotes.
    detail = _detail(
        composes=[
            {
                "order": 0,
                "output": {"company_avg": "scalar"},
                "sql_template": "SELECT AVG(AnnualSalary) AS company_avg FROM dbpcm_warehouse.employee",
            },
            {
                "order": 1,
                "feeds_from": [0],
                "consumes": {"company_avg": "$0.company_avg"},
                "sql_template": (
                    "SELECT Department AS department FROM dbpcm_warehouse.employee "
                    "GROUP BY Department HAVING AVG(AnnualSalary) > {company_avg}"
                ),
                "output": {},
            },
        ],
        result_grain=["Department"],
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["company_avg"], [[55000.0]]),
                _rq(["department"], [["Sales"]]),
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),
            ]
        }
    )
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-inj", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    node1_sql = mcp.calls[1].args["sql"]
    # The value bound as a bare numeric literal (no surrounding quotes) — typed.
    assert "55000" in node1_sql
    assert "'55000" not in node1_sql
    # And it is NOT a string literal in the parsed tree.
    assert "55000.0" not in _string_literals(node1_sql)
    assert "55000" not in _string_literals(node1_sql)


async def test_string_number_scalar_stays_a_string_literal() -> None:
    # A cell that is a numeric-looking STRING ("55000") keeps string typing — the
    # cell's Python type is authoritative, never re-coerced to a number.
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["tok"], [["55000"]]),         # a STRING cell, not a number
                _rq(["department"], [["Sales"]]),
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),
            ]
        }
    )
    outcome = await _executor(mcp, _string_scalar_dag()).execute(
        blueprint_id="bp-inj", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    assert "55000" in _string_literals(mcp.calls[1].args["sql"])  # bound AS a string


async def test_null_scalar_fails_closed_never_binds_null_as_sql() -> None:
    # A scalar that came back NULL cannot be bound as a typed literal — fail-closed
    # (SLOT_INVALID → raw loop), never emitting a broken `WHERE Department = NULL`
    # or a smuggled literal. The downstream node must NOT dispatch.
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["tok"], [[None]]),  # NULL scalar
            ]
        }
    )
    outcome = await _executor(mcp, _string_scalar_dag()).execute(
        blueprint_id="bp-inj", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == SLOT_INVALID_CODE
    assert len(mcp.calls) == 1  # node 1 (the consumer) never dispatched
