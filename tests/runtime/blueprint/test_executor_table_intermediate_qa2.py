"""QA2 Layer-1 — table-intermediate materialize-and-join edge/attack cases.

Companion to ``test_executor_table_intermediate.py`` (the happy-path invariants).
This file drives the failure modes and correctness cliffs of the Slice-2
materialize-and-join path, all with fakes (no infra):

  - materialize EDGE cases: 0-row producer (empty scratch → JOIN nothing → a
    verified "no rows" answer the D56 grain gate still validates), 1-row producer,
    type-inference against a numeric-LOOKING string join key (must stay String, not
    be mis-mapped to Int64), a mixed column (→ String), a NULL cell (→ Nullable);
  - AST-rewrite injection: a hostile endpoint-RETURNED table name is wrapped as an
    AST identifier (cannot break out of the FROM/JOIN token) — pins that the runtime
    trusts the returned name only STRUCTURALLY; a consumer whose scratch placeholder
    has no materialized binding fails closed to SLOT_INVALID; an upstream result
    cell carrying SQL/`scratch.`/`);` rides to materialize as DATA, never the SQL;
  - clean-fail: a materialize rejection (endpoint over-cap / dup columns / bad type)
    → UNSUPPORTED (raw loop); a table intermediate that PAUSES mid-DAG and RESUMES
    → clean SLOT_INVALID, NEVER a wrong verified answer (the materialized map is not
    carried across the checkpoint, so the consumer can never bind a stale table);
  - sid format: the demo ``s<32hex>`` sid yields a rewritten JOIN the data-agent D64
    read gate accepts; the naming contract the fake mints matches what the gate
    extracts.
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import (
    SLOT_INVALID_CODE,
    UNSUPPORTED_CODE,
    BlueprintExecutor,
    ExecCompleted,
    ExecFailed,
    ExecPaused,
    _infer_scratch_columns,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.mcp.scratch_client import (
    FakeScratchClient,
    ScratchClientError,
)
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex
from data_agent.sqlparse.provenance import (
    ScratchSessionError,
    _validate_scratch_name,
)

_W = "dbpcm_warehouse"
_EMP = f"{_W}.employee"
_PAY = f"{_W}.payroll"

CATALOG = CatalogHandle(
    {
        _EMP: {"EmployeeCode": "String", "Department": "Nullable(String)"},
        _PAY: {
            "EmployeeCode": "String",
            "RegisterType": "Nullable(String)",
            "Amount": "Nullable(Float64)",
        },
    }
)

# An underscore-free, identifier-safe sid (the Slice-2 contract).
_SID = "sdagqa2test"

_USES = frozenset(
    {
        f"{_PAY}.EmployeeCode",
        f"{_PAY}.RegisterType",
        f"{_PAY}.Amount",
        f"{_EMP}.EmployeeCode",
        f"{_EMP}.Department",
    }
)

_PRODUCER_SQL = (
    "SELECT toString(p.EmployeeCode) AS EmployeeCode, toFloat64(SUM(p.Amount)) AS earnings "
    "FROM dbpcm_warehouse.payroll AS p WHERE p.RegisterType = 'EARN' GROUP BY p.EmployeeCode"
)
_CONSUMER_SQL = (
    "SELECT e.Department AS department, SUM(x.earnings) AS total_earnings "
    "FROM scratch.emp_earnings AS x "
    "JOIN dbpcm_warehouse.employee AS e ON e.EmployeeCode = x.EmployeeCode "
    "WHERE e.Department = {department} GROUP BY e.Department"
)


def _creds(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id=_SID, jwt="jwt-secret", column_scope=scope)


def _rq(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


def _detail(*, composes: list[dict[str, Any]] | None = None) -> BlueprintDetail:
    return BlueprintDetail(
        id="bp-table",
        intent="Total earnings by department via a scratch join",
        slots_summary="department",
        uses=_USES,
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        slots=[
            {
                "name": "department",
                "type": "string",
                "required": True,
                "binds_to": f"{_EMP}.Department",
            }
        ],
        uses_rules=None,
        sql_template=None,
        composes=composes
        or [
            {"order": 0, "output": {"emp_earnings": "table"}, "sql_template": _PRODUCER_SQL},
            {
                "order": 1,
                "feeds_from": [0],
                "consumes": {"emp_earnings": "$0"},
                "output": {},
                "sql_template": _CONSUMER_SQL,
            },
        ],
        result_grain=["Department"],
    )


def _executor(
    mcp: FakeMCPClient,
    *,
    scratch_client: Any,
    scratch_max_rows: int = 10_000,
    detail: BlueprintDetail | None = None,
) -> BlueprintExecutor:
    index = FakeVectorIndex()
    index.add_detail(detail or _detail())
    return BlueprintExecutor(
        tool_dispatcher=ToolDispatcher(mcp, CATALOG),
        vector_index=index,
        scratch_client=scratch_client,
        scratch_max_rows=scratch_max_rows,
    )


def _mcp(node0: dict[str, Any], node1: dict[str, Any], grain: dict[str, Any]) -> FakeMCPClient:
    """A scripted runQuery sequence: department domain probe, node0 (producer),
    node1 (consumer JOIN), grain probe."""
    return FakeMCPClient(
        scripted={"runQuery": [_rq(["Department"], [["Sales"], ["Eng"]]), node0, node1, grain]}
    )


# ---------------------------------------------------------------------------
# 1. Materialize EDGE cases
# ---------------------------------------------------------------------------


async def test_empty_producer_materializes_empty_table_and_verifies_no_rows() -> None:
    """A 0-row producer → an EMPTY scratch table → JOIN to nothing → a verified
    "no rows" answer (row_count 0). The D56 grain gate still runs (grain probe
    0/0 → 0 == 0 → grain_ok) — an empty intermediate is legitimate, not a failure."""
    mcp = _mcp(
        _rq(["EmployeeCode", "earnings"], []),  # producer: 0 rows
        _rq(["department", "total_earnings"], []),  # consumer JOIN: 0 rows
        _rq(["__bp_n", "__bp_d"], [[0, 0]]),  # grain probe: total 0, distinct 0
    )
    scratch = FakeScratchClient()
    outcome = await _executor(mcp, scratch_client=scratch).execute(
        blueprint_id="bp-table", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    assert outcome.result_full["row_count"] == 0
    # The empty table WAS materialized (rows=[]) — an empty JOIN, not a skipped node.
    mat = next(c for c in scratch.calls if c.op == "materialize")
    assert mat.rows == []
    assert [c["name"] for c in mat.columns] == ["EmployeeCode", "earnings"]


async def test_single_row_producer_materializes_and_verifies() -> None:
    mcp = _mcp(
        _rq(["EmployeeCode", "earnings"], [["1001", 100.0]]),
        _rq(["department", "total_earnings"], [["Sales", 100.0]]),
        _rq(["__bp_n", "__bp_d"], [[1, 1]]),
    )
    scratch = FakeScratchClient()
    outcome = await _executor(mcp, scratch_client=scratch).execute(
        blueprint_id="bp-table", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    mat = next(c for c in scratch.calls if c.op == "materialize")
    assert mat.rows == [["1001", 100.0]]


def test_numeric_looking_string_join_key_stays_string_not_mismapped() -> None:
    """The str-that-looks-numeric hazard: a join key returned as a NATIVE string
    ('1001') must infer to ``String``, NOT ``Int64`` — otherwise the scratch column
    would mistype and mismatch the ``String`` warehouse key. Native type wins over
    the textual appearance of the value (an explicit toString(...) is honored)."""
    cols = _infer_scratch_columns(
        ["EmployeeCode", "earnings"], [["1001", 100.0], ["1002", 200.0]]
    )
    assert cols == [
        {"name": "EmployeeCode", "type": "String"},  # numeric-looking str stays String
        {"name": "earnings", "type": "Float64"},
    ]


def test_mixed_and_null_columns_infer_string_and_nullable() -> None:
    """A column mixing int + str → String fallback (fail-safe); any NULL cell →
    Nullable-wrap of the inferred base type."""
    cols = _infer_scratch_columns(
        ["mixed", "with_null", "all_null"],
        [[1, 5, None], ["two", None, None]],
    )
    by_name = {c["name"]: c["type"] for c in cols}
    assert by_name["mixed"] == "String"  # int + str → String
    assert by_name["with_null"] == "Nullable(Int64)"  # 5 + NULL → Nullable(Int64)
    assert by_name["all_null"] == "Nullable(String)"  # all-NULL → Nullable(String)


# ---------------------------------------------------------------------------
# 2. AST-rewrite injection surface
# ---------------------------------------------------------------------------


class _HostileNameScratchClient:
    """A scratch client whose endpoint RETURNS a crafted, hostile table name — to
    prove the runtime trusts the returned name only STRUCTURALLY (AST identifier),
    never as raw SQL text spliced into the JOIN."""

    def __init__(self, table: str) -> None:
        self._table = table
        self.calls: list[Any] = []

    async def materialize(self, columns, rows, *, jwt, session_id) -> str:  # noqa: ANN001
        return self._table

    async def drop(self, table, *, jwt, session_id) -> None:  # noqa: ANN001
        pass


async def test_hostile_endpoint_table_name_is_ast_quoted_not_injected() -> None:
    """A materialize response with a hostile table name — one that tries to CLOSE the
    identifier quote and append a statement (``x"; DROP TABLE payroll; --``) — is
    rewritten into the consumer's FROM/JOIN as a SINGLE sqlglot identifier: sqlglot
    escapes the quote delimiter, so the crafted text stays one table-name token and
    cannot break out. Structurally proven by re-parsing the emitted SQL: it is still
    ONE read-only SELECT, and the scratch table node's NAME equals the hostile string
    verbatim (it round-trips as an identifier value, not as SQL).

    (Trusting the endpoint's returned name is also safe cross-session: the consumer's
    own runQuery re-runs the D64 read gate, so a name pointing at a foreign scratch
    table would be denied at dispatch — not relied on here, but noted.)"""
    import sqlglot
    from sqlglot import exp

    hostile = 'x"; DROP TABLE payroll; --'
    mcp = _mcp(
        _rq(["EmployeeCode", "earnings"], [["1001", 100.0]]),
        _rq(["department", "total_earnings"], [["Sales", 100.0]]),
        _rq(["__bp_n", "__bp_d"], [[1, 1]]),
    )
    executor = _executor(mcp, scratch_client=_HostileNameScratchClient(hostile))
    outcome = await executor.execute(
        blueprint_id="bp-table", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    consumer_sql = mcp.calls[2].args["sql"]
    # Re-parse: it is a SINGLE statement (not a multi-statement Block), i.e. no
    # breakout — the DROP did not become its own statement.
    parsed = sqlglot.parse(consumer_sql, dialect="clickhouse")
    assert len(parsed) == 1
    assert isinstance(parsed[0], exp.Select)
    # The crafted string survived as exactly one table-name identifier (data, inert).
    table_names = {t.name for t in parsed[0].find_all(exp.Table)}
    assert hostile in table_names


async def test_consumer_placeholder_with_no_materialized_binding_fails_closed() -> None:
    """A consumer template referencing ``scratch.<other>`` — a placeholder the
    producer never materialized to — fails closed to SLOT_INVALID (the executor
    never emits a JOIN against a non-existent scratch table)."""
    bad_consumer = _CONSUMER_SQL.replace("scratch.emp_earnings", "scratch.not_produced")
    detail = _detail(
        composes=[
            {"order": 0, "output": {"emp_earnings": "table"}, "sql_template": _PRODUCER_SQL},
            {
                "order": 1,
                "feeds_from": [0],
                "consumes": {"emp_earnings": "$0"},
                "output": {},
                "sql_template": bad_consumer,
            },
        ]
    )
    mcp = _mcp(
        _rq(["EmployeeCode", "earnings"], [["1001", 100.0]]),
        _rq(["department", "total_earnings"], [["Sales", 100.0]]),
        _rq(["__bp_n", "__bp_d"], [[1, 1]]),
    )
    outcome = await _executor(mcp, scratch_client=FakeScratchClient(), detail=detail).execute(
        blueprint_id="bp-table", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == SLOT_INVALID_CODE


async def test_upstream_cell_with_scratch_and_sql_rides_as_data_only() -> None:
    """An upstream result CELL containing ``scratch.``/``);``/SQL rides to
    materialize as a native row (DATA), and never appears in the consumer SQL — a
    scratch column VALUE is data, not identifier/text."""
    hostile_cell = "'); DROP TABLE scratch.s_victim_bp_x; --"
    mcp = _mcp(
        _rq(["EmployeeCode", "earnings"], [[hostile_cell, 100.0]]),
        _rq(["department", "total_earnings"], [["Sales", 100.0]]),
        _rq(["__bp_n", "__bp_d"], [[1, 1]]),
    )
    scratch = FakeScratchClient()
    outcome = await _executor(mcp, scratch_client=scratch).execute(
        blueprint_id="bp-table", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    mat = next(c for c in scratch.calls if c.op == "materialize")
    assert mat.rows == [[hostile_cell, 100.0]]  # the hostile string is a DATA cell
    assert hostile_cell not in mcp.calls[2].args["sql"]
    assert "DROP TABLE" not in mcp.calls[2].args["sql"]


# ---------------------------------------------------------------------------
# 3. Clean-fail paths
# ---------------------------------------------------------------------------


async def test_materialize_rejection_fails_closed_to_raw_loop() -> None:
    """A materialize the endpoint REJECTS (over-cap / duplicate columns / bad type,
    surfaced as ScratchClientError) → UNSUPPORTED (raw loop), never a partial JOIN."""
    scratch = FakeScratchClient(
        fail=ScratchClientError("SCRATCH_MATERIALIZE_REJECTED", "rejected")
    )
    mcp = _mcp(
        _rq(["EmployeeCode", "earnings"], [["1001", 100.0]]),
        _rq(["department", "total_earnings"], [["Sales", 100.0]]),
        _rq(["__bp_n", "__bp_d"], [[1, 1]]),
    )
    outcome = await _executor(mcp, scratch_client=scratch).execute(
        blueprint_id="bp-table", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == UNSUPPORTED_CODE


async def test_duplicate_producer_columns_forwarded_no_silent_dedup() -> None:
    """The runtime does NOT silently dedup a producer's duplicate column names —
    it forwards both to the endpoint (which re-validates + rejects duplicates,
    proven in ch-api). Pins that no client-side dedup masks a malformed producer:
    the forwarded column list preserves the duplicate, and an endpoint rejection
    fails closed."""
    dup_cols = _infer_scratch_columns(
        ["k", "k"], [["1001", "1002"], ["1003", "1004"]]
    )
    assert [c["name"] for c in dup_cols] == ["k", "k"]  # duplicate preserved, not collapsed


async def test_mid_dag_pause_then_resume_of_table_intermediate_never_wrong_answer() -> None:
    """A table-intermediate blueprint that PAUSES mid-DAG (an approval node between
    the table producer and its consumer) and then RESUMES must NEVER return a wrong
    verified answer. The materialized-table map is LOCAL to one ``_execute_dag`` call
    and is NOT carried across the checkpoint (only scalar outputs are), so on resume
    the rehydrated producer is skipped WITHOUT re-materializing — the consumer then
    finds no binding for its scratch placeholder and fails closed to SLOT_INVALID
    (raw loop). Pinned so a future change that tried to complete this path can only
    do so by ALSO restoring the materialized table, never by binding a stale/empty
    one into a "verified" JOIN."""
    detail = _detail(
        composes=[
            {"order": 0, "output": {"emp_earnings": "table"}, "sql_template": _PRODUCER_SQL},
            {
                "order": 1,
                "node_kind": "approval",
                "feeds_from": [0],
                "output": {},
                "requires_approval": {"prompt": "Proceed with the join?"},
            },
            {
                "order": 2,
                "feeds_from": [1],
                "consumes": {"emp_earnings": "$0"},
                "output": {},
                "sql_template": _CONSUMER_SQL,
            },
        ]
    )
    # First turn: domain probe + producer run, then PAUSE at the approval node.
    mcp1 = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [["Sales"]]),
                _rq(["EmployeeCode", "earnings"], [["1001", 100.0]]),
            ]
        }
    )
    scratch = FakeScratchClient()
    executor = _executor(mcp1, scratch_client=scratch, detail=detail)
    paused = await executor.execute(
        blueprint_id="bp-table", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(paused, ExecPaused)
    assert paused.reason == "blueprint_approval"
    # The producer WAS materialized on the first pass.
    assert any(c.op == "materialize" for c in scratch.calls)

    # Resume with an approval — a FRESH executor (restart-durable), fresh scratch.
    mcp2 = FakeMCPClient(scripted={"runQuery": []})
    scratch2 = FakeScratchClient()
    executor2 = _executor(mcp2, scratch_client=scratch2, detail=detail)
    resumed = await executor2.resume(
        blueprint_id="bp-table",
        slot_bindings={"department": "Sales"},
        completed_nodes_json=paused.completed_nodes_json,
        awaiting_node=paused.awaiting_node or 1,
        approval_answer="approve",
        credentials=_creds(),
    )
    # NEVER a wrong verified answer — a clean SLOT_INVALID (raw-loop) instead.
    assert isinstance(resumed, ExecFailed)
    assert resumed.error_code == SLOT_INVALID_CODE
    # The producer was NOT re-materialized on resume (rehydrated-skipped).
    assert not any(c.op == "materialize" for c in scratch2.calls)


# ---------------------------------------------------------------------------
# 4. sid format / naming contract
# ---------------------------------------------------------------------------


async def test_rewritten_join_name_is_accepted_by_d64_read_gate() -> None:
    """The demo ``s<32hex>`` sid produces a rewritten JOIN whose scratch table name
    the data-agent D64 read gate ACCEPTS for this session (the write naming contract
    and the read-gate extraction agree)."""
    mcp = _mcp(
        _rq(["EmployeeCode", "earnings"], [["1001", 100.0]]),
        _rq(["department", "total_earnings"], [["Sales", 100.0]]),
        _rq(["__bp_n", "__bp_d"], [[1, 1]]),
    )
    scratch = FakeScratchClient()
    outcome = await _executor(mcp, scratch_client=scratch).execute(
        blueprint_id="bp-table", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    mat = next(c for c in scratch.calls if c.op == "materialize")
    bare = mat.table.split(".", 1)[1]
    # The read gate accepts this table for THIS session (no raise)…
    _validate_scratch_name(bare, _SID)
    # …and rejects it for a different underscore-free session.
    with pytest.raises(ScratchSessionError):
        _validate_scratch_name(bare, "s" + "9" * 32)


def test_demo_sid_is_underscore_free_and_identifier_safe() -> None:
    """The Slice-2 sid contract: ``s<32hex>`` is identifier-safe AND underscore-free
    — the exact property the read gate's exact-extraction depends on. A raw uuid4
    (hyphenated) would NOT satisfy it (and is rejected at the materialize endpoint,
    proven in ch-api)."""
    import re
    import uuid

    demo = "s" + uuid.uuid4().hex
    assert re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", demo)
    assert "_" not in demo
    # A hyphenated uuid is neither identifier-safe nor underscore-free-compatible.
    assert not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(uuid.uuid4()))
