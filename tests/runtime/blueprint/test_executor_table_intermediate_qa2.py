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
    → UNSUPPORTED (raw loop);
  - mid-DAG pause/resume of a table intermediate: the producer's materialized
    ``scratch.…`` NAME and the ROW COUNT it wrote ride the checkpoint, so an approval-
    or ``when…ask``-paused blueprint COMPLETES on resume without re-materializing —
    but only after the resume re-counts the live table and finds it intact. A carried
    name that is foreign-session, wrong-database, malformed, missing, countless, or
    whose live count no longer matches (the ROW-level TTL expires rows while leaving
    the table standing) is refused and falls back to the pre-existing SLOT_INVALID,
    with that name never reaching a JOIN;
  - sid format: the demo ``s<32hex>`` sid yields a rewritten JOIN the data-agent D64
    read gate accepts; the naming contract the fake mints matches what the gate
    extracts.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import (
    SLOT_INVALID_CODE,
    UNSUPPORTED_CODE,
    VERIFY_FAILED_CODE,
    BlueprintExecutor,
    ExecCompleted,
    ExecFailed,
    ExecPaused,
    _dumps_completed,
    _infer_scratch_columns,
    _rehydrate_completed,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.client import MCPToolError
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


_APPROVAL_GATED_COMPOSES: list[dict[str, Any]] = [
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


async def _pause_after_producer(
    detail: BlueprintDetail, scratch: FakeScratchClient
) -> ExecPaused:
    """Run the first turn of the approval-gated table-intermediate blueprint: domain
    probe + producer run + materialize, then PAUSE at the approval node. Returns the
    pause (whose checkpoint the resume tests then feed back, tampered or not)."""
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [["Sales"]]),
                _rq(["EmployeeCode", "earnings"], [["1001", 100.0]]),
            ]
        }
    )
    paused = await _executor(mcp, scratch_client=scratch, detail=detail).execute(
        blueprint_id="bp-table", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(paused, ExecPaused)
    assert paused.reason == "blueprint_approval"
    assert any(c.op == "materialize" for c in scratch.calls)  # producer materialized
    return paused


_MISSING = object()  # sentinel: DELETE the `table` key rather than set it


def _retable(completed_nodes_json: str | None, order: int, table: Any) -> str:
    """Rewrite the stored `table` of one completed-node record — the tamper knob for
    the poisoned-checkpoint tests. `_MISSING` DELETES the key entirely."""
    payload = json.loads(completed_nodes_json or "[]")
    for entry in payload:
        if entry.get("order") == order:
            if table is _MISSING:
                entry.pop("table", None)
            else:
                entry["table"] = table
    return json.dumps(payload)


def _count(n: int | Any) -> dict[str, Any]:
    """A `SELECT COUNT(*)` result — the shape the resume's restore probe reads when
    it re-counts a carried scratch table against the row count the producer stored."""
    return _rq(["count()"], [[n]])


async def _resume_fresh(
    detail: BlueprintDetail,
    completed_nodes_json: str | None,
    *,
    counts: list[Any] | None = None,
    consumer_result: dict[str, Any] | None = None,
    grain: dict[str, Any] | None = None,
    credentials: RuntimeCredentials | None = None,
    awaiting_node: int = 1,
) -> tuple[Any, FakeMCPClient, FakeScratchClient]:
    """Resume on a FRESH executor + FRESH scratch client (restart durability: the
    only state crossing the gap is the checkpoint string). The scripted runQuery
    sequence is the domain probe, then ONE restore count-probe response per carried
    intermediate that survives the cheap gates (`counts`, in producer order), then
    the consumer JOIN + grain probe when the resume is expected to complete.

    `counts` is EXPLICIT rather than inferred, because "how many count probes did
    this resume dispatch" is exactly what several of these tests are pinning: a
    refusal at the ownership/row-count gates must cost no dispatch at all, so a test
    that expects a refusal there passes no counts and would notice a stray probe.

    *credentials* defaults to the session that ran the first pass; a test passes a
    DIFFERENT one to resume as another (or as no) session."""
    scripted: list[Any] = [_rq(["Department"], [["Sales"]])]
    scripted.extend(counts or [])
    if consumer_result is not None:
        scripted.append(consumer_result)
    if grain is not None:
        scripted.append(grain)
    mcp = FakeMCPClient(scripted={"runQuery": scripted})
    scratch = FakeScratchClient()
    outcome = await _executor(mcp, scratch_client=scratch, detail=detail).resume(
        blueprint_id="bp-table",
        slot_bindings={"department": "Sales"},
        completed_nodes_json=completed_nodes_json,
        awaiting_node=awaiting_node,
        approval_answer="approve",
        credentials=credentials if credentials is not None else _creds(),
    )
    return outcome, mcp, scratch


async def test_mid_dag_pause_then_resume_of_table_intermediate_completes_or_fails_closed() -> None:
    """A table-intermediate blueprint that PAUSES mid-DAG (an approval node between
    the table producer and its consumer) now RESUMES to a real verified answer: the
    producer's materialized ``scratch.…`` name AND the row count it wrote ride the
    checkpoint, the resume re-counts the table and finds it intact, and only then is
    the consumer's JOIN bound to it.

    The two invariants that made this path safe when it fail-closed still hold, and
    are what this test pins:
      (a) the producer is NOT re-materialized on resume — it is rehydrated-skipped
          (exactly-once, D45), so the answer is computed over the ORIGINAL rows, not
          a silently re-run producer;
      (b) the answer is produced ONLY via the correctly-restored table — the
          consumer's dispatched SQL carries the exact bare name the first pass's
          endpoint returned. A stale/expired/foreign binding never reaches a
          "verified" JOIN (the cross-session, expired and malformed cases below)."""
    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    scratch1 = FakeScratchClient()
    paused = await _pause_after_producer(detail, scratch1)

    resumed, mcp2, scratch2 = await _resume_fresh(
        detail,
        paused.completed_nodes_json,
        counts=[_count(1)],  # the intermediate still holds the 1 row it wrote
        consumer_result=_rq(["department", "total_earnings"], [["Sales", 100.0]]),
        grain=_rq(["__bp_n", "__bp_d"], [[1, 1]]),
    )
    assert isinstance(resumed, ExecCompleted)
    assert resumed.result_full["preview_rows"] == [["Sales", 100.0]]
    # (a) the producer was NOT re-materialized on resume (rehydrated-skipped).
    assert not any(c.op == "materialize" for c in scratch2.calls)
    # (b) the answer came from the RESTORED table, named exactly as the first pass's
    # endpoint returned it — not reconstructed, not a placeholder left standing.
    materialized = next(c for c in scratch1.calls if c.op == "materialize").table
    bare = (materialized or "").split(".", 1)[1]
    consumer_sql = _consumer_sql(mcp2)
    assert bare in consumer_sql
    assert "scratch.emp_earnings" not in consumer_sql  # placeholder was rewritten
    # …and the restore probe counted THAT table, not some other one.
    assert _count_probes(mcp2) == [f"SELECT COUNT(*) FROM scratch.{bare}"]


async def test_resume_binds_the_exact_table_name_the_endpoint_returned() -> None:
    """The restored binding is the endpoint's VERBATIM name, structurally: the
    consumer's dispatched SQL parses back to a single read-only SELECT whose scratch
    table node's name equals the bare ``s_<sid>_bp_<…>`` the first pass's
    FakeScratchClient handed out (asserted against the RECORDED name, never a name
    this test reconstructs from the sid)."""
    import sqlglot
    from sqlglot import exp

    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    scratch1 = FakeScratchClient()
    paused = await _pause_after_producer(detail, scratch1)
    returned = next(c for c in scratch1.calls if c.op == "materialize").table
    assert returned is not None

    resumed, mcp2, _ = await _resume_fresh(
        detail,
        paused.completed_nodes_json,
        counts=[_count(1)],
        consumer_result=_rq(["department", "total_earnings"], [["Sales", 100.0]]),
        grain=_rq(["__bp_n", "__bp_d"], [[1, 1]]),
    )
    assert isinstance(resumed, ExecCompleted)
    parsed = sqlglot.parse(_consumer_sql(mcp2), dialect="clickhouse")
    assert len(parsed) == 1
    scratch_tables = {
        t.name for t in parsed[0].find_all(exp.Table) if t.text("db") == "scratch"
    }
    assert scratch_tables == {returned.split(".", 1)[1]}


async def test_checkpoint_naming_a_foreign_session_table_is_not_seeded() -> None:
    """A poisoned/tampered checkpoint whose stored table belongs to a DIFFERENT
    session is refused at the seeding gate (D64) — the name never becomes a JOIN
    binding, the consumer finds nothing for its ``$0`` and the run fails closed to
    SLOT_INVALID (the pre-existing exit — no new error code). Pinned twice over: the
    outcome, and that NO runQuery carrying the foreign name was ever dispatched, so
    the fast path did not lean on the MCP's own D64 read gate to catch it."""
    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    paused = await _pause_after_producer(detail, FakeScratchClient())
    foreign = "scratch.s" + "9" * 32 + "_bp_" + "0" * 32
    tampered = _retable(paused.completed_nodes_json, 0, foreign)

    resumed, mcp2, _ = await _resume_fresh(detail, tampered)
    assert isinstance(resumed, ExecFailed)
    assert resumed.error_code == SLOT_INVALID_CODE
    bare_foreign = foreign.split(".", 1)[1]
    assert not any(bare_foreign in str(c.args.get("sql", "")) for c in mcp2.calls)


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(_MISSING, id="missing"),
        pytest.param(None, id="null"),
        pytest.param(12345, id="non-string"),
        pytest.param({"table": "s_x_bp_1"}, id="object"),
        pytest.param("", id="empty"),
        pytest.param("s_" + _SID + "_bp_1", id="unqualified-no-db"),
        pytest.param("scratch.not_a_session_table", id="malformed-name"),
        pytest.param("scratch.s_" + _SID, id="no-suffix"),
    ],
)
async def test_checkpoint_with_malformed_or_missing_table_fails_closed(stored: Any) -> None:
    """The ORIGINAL pin, preserved: a resume that cannot restore a TRUSTWORTHY table
    name for a table-consumed producer NEVER completes. Missing, null, non-string or
    structurally malformed — all land on the same SLOT_INVALID (raw loop), never a
    JOIN against a stale/empty/unowned scratch table dressed up as verified.

    The outcome is not the whole assertion. A string-valued tampered name must also
    appear in NO dispatched SQL — not the consumer JOIN and not the restore count
    probe, since a name refused at the ownership gate must cost no dispatch at all.
    Without that half this test would only catch a regression by accident (a seeded
    bad name happens to exhaust the fake's script and change the error code), which a
    future editor adding one more scripted response would silently switch off."""
    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    paused = await _pause_after_producer(detail, FakeScratchClient())
    tampered = _retable(paused.completed_nodes_json, 0, stored)

    resumed, mcp2, scratch2 = await _resume_fresh(detail, tampered)
    assert isinstance(resumed, ExecFailed)
    assert resumed.error_code == SLOT_INVALID_CODE
    # Still never re-materialized — the producer stays rehydrated-skipped (D45).
    assert not any(c.op == "materialize" for c in scratch2.calls)
    if isinstance(stored, str) and stored:
        bare = stored.split(".", 1)[1] if "." in stored else stored
        assert bare not in _dispatched_sql(mcp2)
    assert _count_probes(mcp2) == []  # refused before any probe, in every case


async def test_checkpoint_table_in_the_wrong_database_is_not_seeded() -> None:
    """A stored name that IS session-owned by its bare form but lives in another
    database (``dbpcm_warehouse.s_<sid>_bp_x``) is refused: a producer only ever
    writes to ``scratch``, so anything else is a tampered checkpoint trying to point
    the rewritten JOIN at a warehouse table. SLOT_INVALID, and that name never
    reaches a dispatched query."""
    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    paused = await _pause_after_producer(detail, FakeScratchClient())
    wrong_db = f"{_W}.s_{_SID}_bp_x"
    tampered = _retable(paused.completed_nodes_json, 0, wrong_db)

    resumed, mcp2, _ = await _resume_fresh(detail, tampered)
    assert isinstance(resumed, ExecFailed)
    assert resumed.error_code == SLOT_INVALID_CODE
    assert not any(f"s_{_SID}_bp_x" in str(c.args.get("sql", "")) for c in mcp2.calls)


async def test_when_ask_pause_also_restores_the_table_intermediate() -> None:
    """The OTHER mid-DAG pause flavor — a ``when…on_violation: ask`` between the
    producer and its consumer — resumes through the identical checkpoint path. Pinned
    so the fix is not quietly approval-only (both flavors call the same `_pause`)."""
    detail = _detail(
        composes=[
            {"order": 0, "output": {"emp_earnings": "table"}, "sql_template": _PRODUCER_SQL},
            {
                "order": 1,
                "feeds_from": [0],
                "consumes": {"emp_earnings": "$0"},
                "output": {},
                "sql_template": _CONSUMER_SQL,
                # A table producer contributes an EMPTY scalar env (`count($0)` is 0),
                # so this precondition is violated on the first pass → the `ask` pause.
                "when": {"expr": "count($0) > 0", "on_violation": "ask"},
            },
        ]
    )
    mcp1 = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [["Sales"]]),
                _rq(["EmployeeCode", "earnings"], [["1001", 100.0]]),
            ]
        }
    )
    scratch1 = FakeScratchClient()
    paused = await _executor(mcp1, scratch_client=scratch1, detail=detail).execute(
        blueprint_id="bp-table", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(paused, ExecPaused)
    assert paused.reason == "blueprint_when_ask"

    mcp2 = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [["Sales"]]),
                _count(1),  # the restore probe: the intermediate is intact
                _rq(["department", "total_earnings"], [["Sales", 100.0]]),
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),
            ]
        }
    )
    scratch2 = FakeScratchClient()
    resumed = await _executor(mcp2, scratch_client=scratch2, detail=detail).resume(
        blueprint_id="bp-table",
        slot_bindings={"department": "Sales"},
        completed_nodes_json=paused.completed_nodes_json,
        awaiting_node=paused.awaiting_node or 1,
        approval_answer="yes",
        credentials=_creds(),
    )
    assert isinstance(resumed, ExecCompleted)
    assert not any(c.op == "materialize" for c in scratch2.calls)
    bare = (next(c for c in scratch1.calls if c.op == "materialize").table or "").split(".", 1)[1]
    assert bare in _consumer_sql(mcp2)


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


# ---------------------------------------------------------------------------
# 5. QA sweep of the checkpoint-carried table name — the cases the fix's own
#    tests did not reach. Everything here drives the SAME seam (a persisted
#    checkpoint re-entering `_execute_dag`) from the outside: a resuming SESSION
#    that is not the producing one, a checkpoint written by OLDER code, a name
#    filed against the WRONG node, TWO intermediates of which one is poisoned, an
#    EMPTY intermediate, a scratch table that DIED across the pause, and the D56 /
#    provenance teeth on the completed resumed run.
# ---------------------------------------------------------------------------


def _records(completed_nodes_json: str | None) -> list[dict[str, Any]]:
    return list(json.loads(completed_nodes_json or "[]"))


def _repatch(completed_nodes_json: str | None, order: int, **fields: Any) -> str:
    """Rewrite arbitrary fields of one completed-node record (the general form of
    `_retable` — used for the `provenance` poison and for filing a record under a
    different node order)."""
    payload = _records(completed_nodes_json)
    for entry in payload:
        if entry.get("order") == order:
            entry.update(fields)
    return json.dumps(payload)


def _materialized_name(scratch: FakeScratchClient, nth: int = 0) -> str:
    table = [c for c in scratch.calls if c.op == "materialize"][nth].table
    assert table is not None
    return table


def _dispatched_sql(mcp: FakeMCPClient) -> str:
    return "\n".join(str(c.args.get("sql", "")) for c in mcp.calls)


_COUNT_PROBE_PREFIX = "SELECT COUNT(*) FROM scratch."


def _count_probes(mcp: FakeMCPClient) -> list[str]:
    """Every dispatched restore count-probe (`SELECT COUNT(*) FROM scratch.…`)."""
    return [
        sql
        for c in mcp.calls
        if (sql := str(c.args.get("sql", ""))).startswith(_COUNT_PROBE_PREFIX)
    ]


def _consumer_sql(mcp: FakeMCPClient) -> str:
    """The dispatched CONSUMER JOIN, located by CONTENT: the one query that reads a
    `scratch.` table and is neither a restore count-probe nor the D56 grain probe (which
    wraps the consumer as a subquery and aliases `__bp_n`).

    By content and not by call index on purpose: a resume dispatches a VARIABLE number
    of restore count-probes ahead of the consumer (one per carried intermediate that
    reaches the probe), so a positional `calls[1]` would quietly start asserting against
    a `SELECT COUNT(*)` the moment the restore gate changes."""
    matches = [
        sql
        for c in mcp.calls
        if "scratch." in (sql := str(c.args.get("sql", "")))
        and "__bp_n" not in sql
        and not sql.startswith(_COUNT_PROBE_PREFIX)
    ]
    assert len(matches) == 1, f"expected exactly one consumer JOIN, got {len(matches)}"
    return matches[0]


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(12345, id="int"),
        pytest.param(True, id="bool"),
        pytest.param({"table": "s_x_bp_1"}, id="object"),
        pytest.param(["scratch.s_x_bp_1"], id="list"),
        pytest.param(None, id="null"),
    ],
)
def test_rehydrate_normalizes_a_non_string_table_to_none(stored: Any) -> None:
    """The rehydrator's documented contract, pinned at the unit: a `table` that is not a
    `str` becomes `None` — TYPE only, ownership is re-checked later at the seeding site.
    This is deliberately REDUNDANT with the `isinstance` at that seeding site, and
    redundant guards are exactly the ones a refactor deletes because "nothing failed".
    It stops being redundant the moment the ownership check moves, so it is pinned where
    the promise is made rather than only where it currently happens to be enforced."""
    payload = json.dumps(
        [
            {
                "order": 0,
                "output": {},
                "provenance": [],
                "sql": "SELECT 1",
                "table": stored,
                "row_count": 1,
            }
        ]
    )
    assert _rehydrate_completed(payload)[0]["table"] is None


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param("1", id="numeric-string"),
        pytest.param(1.0, id="float"),
        pytest.param(True, id="bool"),
        pytest.param(-1, id="negative"),
        pytest.param(None, id="null"),
        pytest.param({"n": 1}, id="object"),
    ],
)
def test_rehydrate_normalizes_an_unusable_row_count_to_none(stored: Any) -> None:
    """The count's half of the same contract. It is compared for EXACT equality against
    a live `COUNT(*)`, so anything that is not a non-negative `int` is not a count:
    `True` would compare equal to 1, `1.0` would compare equal to 1, `"1"` would compare
    equal to nothing, and a negative is not a row count at all. All become `None`, which
    the restore gate then refuses — the table's contents cannot be verified without a
    number to verify them against."""
    payload = json.dumps(
        [
            {
                "order": 0,
                "output": {},
                "provenance": [],
                "sql": "SELECT 1",
                "table": f"scratch.s_{_SID}_bp_1",
                "row_count": stored,
            }
        ]
    )
    assert _rehydrate_completed(payload)[0]["row_count"] is None


def test_a_completed_node_record_round_trips_through_the_checkpoint() -> None:
    """Dump → load is the identity on all four carried fields, including a table name
    with the endpoint's real shape. The pause and the resume are two different processes;
    if this is not an identity, everything above is testing a coincidence."""
    table = f"scratch.s_{_SID}_bp_{'a' * 32}"
    running = {
        0: {
            "output": {},
            "provenance": frozenset({(_PAY, "Amount")}),
            "sql": "SELECT 1",
            "table": table,
            "row_count": 812,
        },
        1: {
            "output": {"n": 7},
            "provenance": frozenset(),
            "sql": "SELECT 2",
            "table": None,
            "row_count": None,
        },
    }
    back = _rehydrate_completed(_dumps_completed(running))
    assert back[0]["table"] == table
    assert back[0]["row_count"] == 812
    assert back[0]["provenance"] == frozenset({(_PAY, "Amount")})
    assert back[1]["table"] is None
    assert back[1]["row_count"] is None
    assert back[1]["output"] == {"n": 7}


async def test_the_pause_checkpoint_carries_the_table_name_in_its_wire_format() -> None:
    """The checkpoint is a STRING handed to session persistence and handed back on a
    later turn, possibly to a different process. Pin its wire shape directly, not just
    its round-trip behaviour: the producer's record carries `table` as the endpoint's
    full `scratch.…` name AND `row_count` as the number of rows it wrote there,
    alongside `output`/`provenance`/`sql`, and the whole payload is JSON — because a
    resume that cannot re-read this string is a resume that binds nothing, and one
    that reads the name without the count is a resume that cannot tell an intact
    intermediate from a row-TTL-expired one."""
    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    scratch1 = FakeScratchClient()
    paused = await _pause_after_producer(detail, scratch1)

    records = _records(paused.completed_nodes_json)
    assert [r["order"] for r in records] == [0]  # only the producer has completed
    assert set(records[0]) == {"order", "output", "provenance", "sql", "table", "row_count"}
    assert records[0]["table"] == _materialized_name(scratch1)
    assert records[0]["table"].startswith("scratch.")
    # The count is what the endpoint was actually handed, not what the node returned.
    assert records[0]["row_count"] == len(
        next(c for c in scratch1.calls if c.op == "materialize").rows or []
    )


async def test_resume_under_a_different_session_never_restores_the_first_sessions_table() -> None:
    """The checkpoint is not a bearer token. A resume whose `credentials.session_id`
    DIFFERS from the session that ran the first pass (a hijacked/mis-routed session
    document, or a checkpoint replayed into someone else's turn) must not restore the
    producing session's scratch table: the name is refused at the seeding gate, the
    run fails closed to SLOT_INVALID, and the foreign name never reaches a dispatched
    query — the D64 read gate at the MCP is the SECOND line here, not the first."""
    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    scratch1 = FakeScratchClient()
    paused = await _pause_after_producer(detail, scratch1)
    victim_table = _materialized_name(scratch1)

    other = RuntimeCredentials(
        session_id="s" + "9" * 32, jwt="jwt-secret", column_scope=frozenset()
    )
    resumed, mcp2, scratch2 = await _resume_fresh(
        detail, paused.completed_nodes_json, credentials=other
    )
    assert isinstance(resumed, ExecFailed)
    assert resumed.error_code == SLOT_INVALID_CODE
    assert victim_table.split(".", 1)[1] not in _dispatched_sql(mcp2)
    assert not any(c.op == "materialize" for c in scratch2.calls)


async def test_a_resume_whose_sid_only_prefix_matches_the_stored_table_is_refused() -> None:
    """The hole the D64 rule was TIGHTENED to close, driven through the executor seam
    rather than the predicate's own unit test — the one an innocent
    ``startswith(f"scratch.s_{session_id}_")`` "simplification" of the executor's guard
    would reopen, and which nothing else in the suite catches.

    The first pass (session `sdagqa2test`) materializes `scratch.s_sdagqa2test_bp_<hex>`.
    A session calling itself `sdagqa2test_bp` is a strict prefix-extension of that
    name's owner: a loose prefix test says "mine", exact extraction says the owner is
    `sdagqa2test` and refuses. The refusal is what makes it safe to bind a persisted
    name into a JOIN at all."""
    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    scratch1 = FakeScratchClient()
    paused = await _pause_after_producer(detail, scratch1)
    victim_table = _materialized_name(scratch1)
    bare = victim_table.split(".", 1)[1]

    prefix_claimant = RuntimeCredentials(
        session_id=f"{_SID}_bp", jwt="jwt-secret", column_scope=frozenset()
    )
    # The premise: a loose prefix check WOULD have accepted this pairing.
    assert bare.startswith(f"s_{prefix_claimant.session_id}_")

    resumed, mcp2, _ = await _resume_fresh(
        detail, paused.completed_nodes_json, credentials=prefix_claimant
    )
    assert isinstance(resumed, ExecFailed)
    assert resumed.error_code == SLOT_INVALID_CODE
    assert bare not in _dispatched_sql(mcp2)


async def test_a_sessionless_resume_never_restores_a_carried_table() -> None:
    """The omit-the-header bypass, at the resume seam: a turn whose `session_id` is
    empty cannot PROVE it owns anything, so a carried `scratch.…` name — even a
    perfectly well-formed one — is not restored. Fail-closed on a falsy sid is the
    property that makes "the name is only usable by its owner" true rather than
    "usable by anyone who can drop a header"."""
    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    scratch1 = FakeScratchClient()
    paused = await _pause_after_producer(detail, scratch1)
    bare = _materialized_name(scratch1).split(".", 1)[1]

    sessionless = RuntimeCredentials(session_id="", jwt="jwt-secret", column_scope=frozenset())
    resumed, mcp2, _ = await _resume_fresh(
        detail, paused.completed_nodes_json, credentials=sessionless
    )
    assert isinstance(resumed, ExecFailed)
    assert resumed.error_code == SLOT_INVALID_CODE
    assert bare not in _dispatched_sql(mcp2)


async def test_a_legacy_checkpoint_written_before_the_fix_fails_closed_not_crashes() -> None:
    """FORWARD COMPATIBILITY, the direction a live deploy actually takes: a checkpoint
    JSON written by the PREVIOUS code — no `table` key on ANY record — is handed to the
    new resume path. It must rehydrate cleanly (the missing key is a `None`, not a
    KeyError), skip the seeding, and land on the pre-existing SLOT_INVALID, i.e. exactly
    the behaviour that release shipped. A crash here would turn an in-flight approval
    into a 500 the moment the fix deploys."""
    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    paused = await _pause_after_producer(detail, FakeScratchClient())
    legacy = json.dumps(
        [
            {k: v for k, v in r.items() if k != "table"}
            for r in _records(paused.completed_nodes_json)
        ]
    )
    assert all("table" not in r for r in json.loads(legacy))

    resumed, _mcp2, scratch2 = await _resume_fresh(detail, legacy)
    assert isinstance(resumed, ExecFailed)
    assert resumed.error_code == SLOT_INVALID_CODE
    assert not any(c.op == "materialize" for c in scratch2.calls)


async def test_a_table_filed_against_the_wrong_node_is_never_borrowed() -> None:
    """`materialized` is keyed by PRODUCER ORDER and the consumer looks up exactly the
    order its `$N` names — it never falls back to "whatever table is lying around". A
    tampered checkpoint that moves a perfectly VALID own-session name off the producer
    (order 0) and onto the approval node (order 1) therefore restores nothing usable:
    SLOT_INVALID, and the misfiled name never reaches a query."""
    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    scratch1 = FakeScratchClient()
    paused = await _pause_after_producer(detail, scratch1)
    real = _materialized_name(scratch1)
    payload = _records(paused.completed_nodes_json)
    payload[0]["table"] = None
    payload.append({"order": 1, "output": {}, "provenance": [], "sql": None, "table": real})

    resumed, mcp2, _ = await _resume_fresh(detail, json.dumps(payload))
    assert isinstance(resumed, ExecFailed)
    assert resumed.error_code == SLOT_INVALID_CODE
    assert real.split(".", 1)[1] not in _dispatched_sql(mcp2)


async def test_an_extraneous_table_on_a_non_consumed_node_is_inert() -> None:
    """The benign twin of the misfiled case: a checkpoint carrying an EXTRA valid-looking
    name on a node nobody table-consumes (here the approval node) neither breaks the
    resume nor leaks into it. The run completes on the producer's own name, and the
    extraneous one appears in no dispatched SQL — an unused `materialized` entry is dead
    weight, not an alternative binding."""
    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    scratch1 = FakeScratchClient()
    paused = await _pause_after_producer(detail, scratch1)
    real = _materialized_name(scratch1)
    extraneous = f"scratch.s_{_SID}_bp_{'f' * 32}"
    payload = _records(paused.completed_nodes_json)
    payload.append(
        {
            "order": 1,
            "output": {},
            "provenance": [],
            "sql": None,
            "table": extraneous,
            "row_count": 1,
        }
    )

    resumed, mcp2, _ = await _resume_fresh(
        detail,
        json.dumps(payload),
        counts=[_count(1)],
        consumer_result=_rq(["department", "total_earnings"], [["Sales", 100.0]]),
        grain=_rq(["__bp_n", "__bp_d"], [[1, 1]]),
    )
    assert isinstance(resumed, ExecCompleted)
    sql = _dispatched_sql(mcp2)
    assert real.split(".", 1)[1] in sql
    assert extraneous.split(".", 1)[1] not in sql
    # Not even COUNTED: the restore only considers orders a consumer actually names,
    # so a name filed against a node nobody table-consumes costs no dispatch either.
    assert _count_probes(mcp2) == [f"SELECT COUNT(*) FROM scratch.{real.split('.', 1)[1]}"]


_PRODUCER_B_SQL = (
    "SELECT toString(p.EmployeeCode) AS EmployeeCode, toFloat64(SUM(p.Amount)) AS bonus "
    "FROM dbpcm_warehouse.payroll AS p WHERE p.RegisterType = 'BONUS' GROUP BY p.EmployeeCode"
)
_CONSUMER_TWO_SQL = (
    "SELECT e.Department AS department, SUM(x.earnings) AS total_earnings, "
    "SUM(y.bonus) AS total_bonus "
    "FROM scratch.emp_earnings AS x "
    "JOIN scratch.emp_bonus AS y ON y.EmployeeCode = x.EmployeeCode "
    "JOIN dbpcm_warehouse.employee AS e ON e.EmployeeCode = x.EmployeeCode "
    "WHERE e.Department = {department} GROUP BY e.Department"
)
_TWO_PRODUCER_COMPOSES: list[dict[str, Any]] = [
    {"order": 0, "output": {"emp_earnings": "table"}, "sql_template": _PRODUCER_SQL},
    {"order": 1, "output": {"emp_bonus": "table"}, "sql_template": _PRODUCER_B_SQL},
    {
        "order": 2,
        "node_kind": "approval",
        "feeds_from": [0, 1],
        "output": {},
        "requires_approval": {"prompt": "Proceed with the join?"},
    },
    {
        "order": 3,
        "feeds_from": [2],
        "consumes": {"emp_earnings": "$0", "emp_bonus": "$1"},
        "output": {},
        "sql_template": _CONSUMER_TWO_SQL,
    },
]


async def _pause_after_two_producers(
    scratch: FakeScratchClient,
) -> tuple[BlueprintDetail, ExecPaused]:
    """First turn of a TWO-intermediate blueprint: both producers run and materialize,
    then the approval node pauses — so the checkpoint carries two independent names."""
    detail = _detail(composes=_TWO_PRODUCER_COMPOSES)
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [["Sales"]]),
                _rq(["EmployeeCode", "earnings"], [["1001", 100.0]]),
                _rq(["EmployeeCode", "bonus"], [["1001", 10.0]]),
            ]
        }
    )
    paused = await _executor(mcp, scratch_client=scratch, detail=detail).execute(
        blueprint_id="bp-table", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(paused, ExecPaused)
    assert len([c for c in scratch.calls if c.op == "materialize"]) == 2
    return detail, paused


async def test_two_table_intermediates_are_both_restored_across_the_pause() -> None:
    """The map is per-node, and so is the restore: a DAG with TWO table producers
    feeding ONE consumer resumes with BOTH names bound to their own placeholders. Pinned
    because a carry that happened to work for a single intermediate (say, by remembering
    "the last table") would produce a self-join here and a silently wrong "verified"
    answer."""
    scratch1 = FakeScratchClient()
    detail, paused = await _pause_after_two_producers(scratch1)
    first, second = _materialized_name(scratch1, 0), _materialized_name(scratch1, 1)
    assert first != second

    resumed, mcp2, scratch2 = await _resume_fresh(
        detail,
        paused.completed_nodes_json,
        counts=[_count(1), _count(1)],  # BOTH intermediates re-counted, in order
        consumer_result=_rq(
            ["department", "total_earnings", "total_bonus"], [["Sales", 100.0, 10.0]]
        ),
        grain=_rq(["__bp_n", "__bp_d"], [[1, 1]]),
        awaiting_node=2,
    )
    assert isinstance(resumed, ExecCompleted)
    assert not any(c.op == "materialize" for c in scratch2.calls)
    consumer_sql = _consumer_sql(mcp2)
    assert first.split(".", 1)[1] in consumer_sql
    assert second.split(".", 1)[1] in consumer_sql
    assert "scratch.emp_earnings" not in consumer_sql
    assert "scratch.emp_bonus" not in consumer_sql
    # Each carried intermediate is verified on its OWN name — not one probe standing
    # in for both, which would let a second, expired table ride in on the first.
    assert _count_probes(mcp2) == [
        f"SELECT COUNT(*) FROM scratch.{first.split('.', 1)[1]}",
        f"SELECT COUNT(*) FROM scratch.{second.split('.', 1)[1]}",
    ]


async def test_two_intermediates_with_one_poisoned_binds_neither() -> None:
    """PARTIAL rehydration is not a partial run. With one of the two carried names
    poisoned (a foreign session's table on order 1), the consumer's binding map is built
    ALL-or-nothing: the run fails closed to SLOT_INVALID, so the still-valid name for
    order 0 is never joined into a half-built query.

    The good name IS re-counted on the way (the restore verifies each carried
    intermediate independently, and order 0's is genuine), so the assertion is about
    the QUERY that matters: no dispatched statement both names the good table and
    touches the warehouse, i.e. no half-bound JOIN was ever emitted. The foreign name
    is absent from every dispatch, count probe included — it never passed the
    ownership gate."""
    scratch1 = FakeScratchClient()
    detail, paused = await _pause_after_two_producers(scratch1)
    good = _materialized_name(scratch1, 0)
    foreign = "scratch.s" + "9" * 32 + "_bp_" + "0" * 32
    tampered = _retable(paused.completed_nodes_json, 1, foreign)

    resumed, mcp2, _ = await _resume_fresh(
        detail, tampered, counts=[_count(1)], awaiting_node=2
    )
    assert isinstance(resumed, ExecFailed)
    assert resumed.error_code == SLOT_INVALID_CODE
    assert foreign.split(".", 1)[1] not in _dispatched_sql(mcp2)
    bare_good = good.split(".", 1)[1]
    assert not any(
        bare_good in (sql := str(c.args.get("sql", ""))) and _W in sql for c in mcp2.calls
    )
    assert _count_probes(mcp2) == [f"SELECT COUNT(*) FROM scratch.{bare_good}"]


async def test_an_empty_table_intermediate_survives_the_pause_and_verifies_no_rows() -> None:
    """An EMPTY intermediate is a legitimate answer, and it has to survive the pause on
    the same terms as a populated one — the pre-fix behaviour (bind nothing) and an
    empty table are indistinguishable in the SQL but very distinguishable in the answer.
    Here the producer materializes 0 rows, the run pauses, and the resume JOINs the real
    (empty) table to a verified `row_count == 0` — with the empty table's own name in the
    query, never a skipped/unbound consumer.

    ALSO the anti-over-correction pin for the TTL gate. That gate refuses an intermediate
    whose live count no longer matches what was written, and the tempting cheap version —
    "refuse if the table is now empty" — would break exactly this case. Stored 0, probes
    0, matches: a legitimately empty intermediate is not an expired one."""
    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    scratch1 = FakeScratchClient()
    mcp1 = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [["Sales"]]),
                _rq(["EmployeeCode", "earnings"], []),  # producer: 0 rows
            ]
        }
    )
    paused = await _executor(mcp1, scratch_client=scratch1, detail=detail).execute(
        blueprint_id="bp-table", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(paused, ExecPaused)
    assert next(c for c in scratch1.calls if c.op == "materialize").rows == []

    resumed, mcp2, _ = await _resume_fresh(
        detail,
        paused.completed_nodes_json,
        counts=[_count(0)],  # stored 0, probes 0 — intact, not expired
        consumer_result=_rq(["department", "total_earnings"], []),
        grain=_rq(["__bp_n", "__bp_d"], [[0, 0]]),
    )
    assert isinstance(resumed, ExecCompleted)
    assert resumed.result_full["row_count"] == 0
    assert _materialized_name(scratch1).split(".", 1)[1] in _consumer_sql(mcp2)


@pytest.mark.parametrize(
    "code",
    [
        pytest.param("CLICKHOUSE_QUERY_ERROR", id="clickhouse-query-error"),
        pytest.param("TABLE_NOT_FOUND", id="table-not-found"),
    ],
)
async def test_a_scratch_table_that_died_across_the_pause_is_a_clean_failure(code: str) -> None:
    """THE assumption the fix rests on, tested from the other side: the carried name is
    restored on the belief that the scratch table is still there and still holds what it
    held. When the TABLE ITSELF is unresolvable (someone dropped it, the database was
    cleared), that belief now fails at the RESTORE COUNT PROBE — the first thing a resume
    does with a carried name — so the run refuses the seed and lands on SLOT_INVALID
    without ever dispatching the JOIN.

    This CHANGED with the TTL gate, and the change is the safe direction: previously the
    restore was unconditional and the death surfaced only when the consumer's own
    runQuery failed, passing the inner denial through. Now nothing is bound at all. The
    outcome is the pre-existing SLOT_INVALID (no invented code), and the restored name
    appears ONLY in the probe that discovered the problem — never in a JOIN."""
    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    scratch1 = FakeScratchClient()
    paused = await _pause_after_producer(detail, scratch1)

    mcp2 = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [["Sales"]]),
                MCPToolError(code, "Table scratch.s_… doesn't exist"),
            ]
        }
    )
    resumed = await _executor(mcp2, scratch_client=FakeScratchClient(), detail=detail).resume(
        blueprint_id="bp-table",
        slot_bindings={"department": "Sales"},
        completed_nodes_json=paused.completed_nodes_json,
        awaiting_node=1,
        approval_answer="approve",
        credentials=_creds(),
    )
    assert isinstance(resumed, ExecFailed)
    assert resumed.error_code == SLOT_INVALID_CODE
    bare = _materialized_name(scratch1).split(".", 1)[1]
    assert _count_probes(mcp2) == [f"SELECT COUNT(*) FROM scratch.{bare}"]
    # No consumer JOIN was ever dispatched — the run stopped at the probe.
    assert not any(_W in str(c.args.get("sql", "")) and bare in str(c.args.get("sql", "")) for c in mcp2.calls)


async def test_a_consumer_denial_after_a_verified_restore_passes_through_verbatim() -> None:
    """The other half of the death case, preserved from the pre-TTL-gate version of the
    test above: when the restore VERIFIES (the table is there and counts right) and the
    consumer's own runQuery is nonetheless denied — a scope change, a permission revoked
    mid-pause, a backend blip — the executor passes that inner denial through VERBATIM
    rather than inventing a runBlueprint code for it.

    What must never happen in either half is the third outcome, a `verified`
    ExecCompleted: the restored name only ever buys a query ATTEMPT."""
    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    scratch1 = FakeScratchClient()
    paused = await _pause_after_producer(detail, scratch1)

    mcp2 = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [["Sales"]]),
                _count(1),  # the restore probe passes — the intermediate is intact
                MCPToolError("COLUMN_SCOPE_VIOLATION", "denied"),
            ]
        }
    )
    resumed = await _executor(mcp2, scratch_client=FakeScratchClient(), detail=detail).resume(
        blueprint_id="bp-table",
        slot_bindings={"department": "Sales"},
        completed_nodes_json=paused.completed_nodes_json,
        awaiting_node=1,
        approval_answer="approve",
        credentials=_creds(),
    )
    assert isinstance(resumed, ExecFailed)
    assert resumed.error_code == "COLUMN_SCOPE_VIOLATION"  # verbatim, no invented code
    # The attempt DID carry the restored name (this is the denial path, not the
    # unbound-placeholder path it must not be confused with).
    assert _materialized_name(scratch1).split(".", 1)[1] in _consumer_sql(mcp2)


# ---------------------------------------------------------------------------
# 5b. THE ROW-TTL GATE. Formerly a known gap (a `verified` under-counted answer);
#     now the reason a carried table name is never trusted on age.
#
#     ch-api creates the intermediate as
#     ``ENGINE = MergeTree … TTL <ttl_col> + INTERVAL <scratch_ttl_seconds> SECOND``
#     (`app/scratch_ingest.py::build_scratch_create_sql`) — a ROW-level TTL, not a
#     table TTL. ClickHouse expires ROWS on background merges and never drops the
#     table, so past the TTL the name still RESOLVES (no dispatch error to pass
#     through, unlike a dropped table) while the JOIN sees zero rows, or an arbitrary
#     partially-merged subset. Nothing bounds the window either: `scratch_ttl_seconds`
#     defaults to 3600 while the session doc carrying the checkpoint lives
#     `session_ttl_seconds` = 604_800 (7 days), and no layer stamps or checks a pause
#     age. The D56 grain gate is a SHAPE check on the terminal result and validates the
#     smaller result exactly as happily — so an approval answered the next morning
#     would return a silently under-counted aggregate as `status: "verified"`.
#
#     The gate: the producer's row count rides the checkpoint beside the name, and the
#     resume re-counts the live table and refuses to seed unless the two match exactly.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "live"),
    [
        pytest.param(1, 0, id="fully-expired"),
        pytest.param(812, 0, id="fully-expired-large"),
        pytest.param(812, 300, id="partially-merged"),
        pytest.param(812, 813, id="grew"),
    ],
)
async def test_a_row_expired_intermediate_is_refused_not_answered_verified(
    written: int, live: int
) -> None:
    """THE BLOCKER'S REGRESSION TEST. The producer materialized *written* rows; by resume
    time the live table holds *live* of them. That must never become an answer.

    Four arms, one rule — EXACT equality, both directions:
      * `1 → 0` and `812 → 0` — fully expired. The table resolves, the JOIN matches
        nothing, and the D56 grain gate reads `0 == 0` and passes. Without this gate that
        is a "verified" empty answer to a question whose true answer had rows;
      * `812 → 300` — mid-merge. An ARBITRARY surviving subset, which is the same wrong
        answer with a plausible-looking number on it;
      * `812 → 813` — more rows than were written. Not a TTL symptom, but the check is an
        equality and not a floor, and a table that GREW is no longer the one this run
        produced either.

    The large counts are stamped onto the checkpoint rather than genuinely materialized —
    the executor compares the STORED number to the probed one, and materializing 812 fake
    rows would test the fake, not the gate.

    In every arm: no seed, no JOIN dispatched, the pre-existing SLOT_INVALID, raw loop.
    """
    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    scratch1 = FakeScratchClient()
    paused = await _pause_after_producer(detail, scratch1)
    # What the intermediate HELD is carried on the record — the thing the old
    # checkpoint lacked and the reason this was unfixable at the resume.
    assert _records(paused.completed_nodes_json)[0]["row_count"] == 1
    stored = _repatch(paused.completed_nodes_json, 0, row_count=written)

    resumed, mcp2, _ = await _resume_fresh(
        detail,
        stored,
        counts=[_count(live)],
        # Scripted but never reached: a consumer JOIN over a half-expired intermediate
        # returning a plausible, wrong, verifiable number.
        consumer_result=_rq(["department", "total_earnings"], [["Sales", 50.0]]),
        grain=_rq(["__bp_n", "__bp_d"], [[1, 1]]),
    )
    assert isinstance(resumed, ExecFailed)
    assert resumed.error_code == SLOT_INVALID_CODE
    bare = _materialized_name(scratch1).split(".", 1)[1]
    assert _count_probes(mcp2) == [f"SELECT COUNT(*) FROM scratch.{bare}"]
    # The JOIN was never dispatched — nothing bound the expired table.
    assert not any(
        bare in (sql := str(c.args.get("sql", ""))) and _W in sql for c in mcp2.calls
    )


@pytest.mark.parametrize(
    "probe",
    [
        pytest.param(MCPToolError("TABLE_NOT_FOUND", "gone"), id="table-gone"),
        pytest.param(MCPToolError("CLICKHOUSE_QUERY_ERROR", "boom"), id="query-error"),
        pytest.param(RuntimeError("transport"), id="transport-blip"),
        pytest.param(_rq(["count()"], []), id="no-rows"),
        pytest.param(_rq(["count()"], [[1], [1]]), id="two-rows"),
        pytest.param(_rq(["count()", "x"], [[1, 2]]), id="two-columns"),
        pytest.param(_rq(["count()"], [["not-a-number"]]), id="non-numeric"),
        pytest.param(_rq(["count()"], [[None]]), id="null"),
        pytest.param(_rq(["count()"], [[True]]), id="bool"),
        pytest.param("not-a-result", id="not-a-dict"),
    ],
)
async def test_a_restore_probe_that_cannot_answer_refuses_the_seed(probe: Any) -> None:
    """The count gate can only license a bind when it actually COUNTED something. A
    denial, a transport blip, a vanished table, or any result it cannot read as a single
    integer cell is NOT a pass — it is an unknown, and an unknown fails closed to
    SLOT_INVALID.

    `True` is in here on purpose: `int(True)` is 1, so a bool cell would silently
    "match" a 1-row intermediate if the unpack leaned on `int()` alone. A numeric STRING
    is deliberately NOT here — it is accepted, because a live ClickHouse may hand a
    UInt64 back as a decimal string over JSON and refusing that would fail every real
    resume (see the sibling test below)."""
    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    scratch1 = FakeScratchClient()
    paused = await _pause_after_producer(detail, scratch1)

    resumed, mcp2, _ = await _resume_fresh(detail, paused.completed_nodes_json, counts=[probe])
    assert isinstance(resumed, ExecFailed)
    assert resumed.error_code == SLOT_INVALID_CODE
    bare = _materialized_name(scratch1).split(".", 1)[1]
    assert not any(
        bare in (sql := str(c.args.get("sql", ""))) and _W in sql for c in mcp2.calls
    )


async def test_a_stringified_count_from_clickhouse_still_verifies() -> None:
    """ClickHouse's `count()` is a UInt64 and can arrive as a decimal STRING over the
    JSON transport. The gate coerces with `int(...)` — the same tolerance
    `unpack_grain_probe` already applies to the D56 probe's totals — so a live resume is
    not failed by a wire-format detail. Pinned so a future "tighten it to isinstance(int)"
    has to notice it would break every real resume."""
    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    paused = await _pause_after_producer(detail, FakeScratchClient())

    resumed, _mcp2, _ = await _resume_fresh(
        detail,
        paused.completed_nodes_json,
        counts=[_count("1")],
        consumer_result=_rq(["department", "total_earnings"], [["Sales", 100.0]]),
        grain=_rq(["__bp_n", "__bp_d"], [[1, 1]]),
    )
    assert isinstance(resumed, ExecCompleted)


async def test_a_checkpoint_with_a_table_but_no_row_count_is_refused() -> None:
    """A LEGACY checkpoint — one written by code that carried the table name but not the
    count (the shape that shipped between the resume fix and the TTL gate) — cannot have
    its table verified, so it is refused rather than trusted.

    Fail-closed on the deploy boundary, and cheaply: the missing count is caught before
    any probe is dispatched, so an in-flight approval from the previous build degrades to
    the raw loop instead of paying for a probe it cannot use."""
    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    scratch1 = FakeScratchClient()
    paused = await _pause_after_producer(detail, scratch1)
    legacy = json.dumps(
        [
            {k: v for k, v in r.items() if k != "row_count"}
            for r in _records(paused.completed_nodes_json)
        ]
    )
    assert all("table" in r and "row_count" not in r for r in json.loads(legacy))

    resumed, mcp2, scratch2 = await _resume_fresh(detail, legacy)
    assert isinstance(resumed, ExecFailed)
    assert resumed.error_code == SLOT_INVALID_CODE
    assert _count_probes(mcp2) == []  # refused before the probe
    assert not any(c.op == "materialize" for c in scratch2.calls)


async def test_a_fresh_run_never_dispatches_a_restore_probe() -> None:
    """The gate is resume-only. A first-call `runBlueprint` materializes its own
    intermediate in-process and holds the name in memory, so there is nothing to restore
    and nothing to re-count — the extra probe must not appear on the hot path and cost
    every table-intermediate blueprint an extra round trip."""
    mcp = _mcp(
        _rq(["EmployeeCode", "earnings"], [["1001", 100.0]]),
        _rq(["department", "total_earnings"], [["Sales", 100.0]]),
        _rq(["__bp_n", "__bp_d"], [[1, 1]]),
    )
    outcome = await _executor(mcp, scratch_client=FakeScratchClient()).execute(
        blueprint_id="bp-table", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    assert _count_probes(mcp) == []


async def test_a_resumed_run_unions_provenance_across_the_whole_dag() -> None:
    """D44/D56 footprint honesty on a resumed run. The producer never re-runs, so the
    ONLY record of the warehouse columns it read is the checkpoint — if the carry were
    dropped, the completed answer would claim a footprint of just the consumer's
    columns and under-report what it depended on. The union must span BOTH nodes, and
    scratch pairs stay excluded (session-gated, not scope-gated)."""
    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    paused = await _pause_after_producer(detail, FakeScratchClient())

    resumed, _mcp2, _ = await _resume_fresh(
        detail,
        paused.completed_nodes_json,
        counts=[_count(1)],
        consumer_result=_rq(["department", "total_earnings"], [["Sales", 100.0]]),
        grain=_rq(["__bp_n", "__bp_d"], [[1, 1]]),
    )
    assert isinstance(resumed, ExecCompleted)
    assert resumed.provenance is not None
    assert (_PAY, "Amount") in resumed.provenance  # the PRE-pause producer's read
    assert (_EMP, "Department") in resumed.provenance  # the POST-pause consumer's read
    assert not any(table.startswith("scratch.") for table, _col in resumed.provenance)
    # …and the per-node SQL transparency list spans the whole DAG too.
    assert len(resumed.result_full["sql"]) == 2


async def test_a_null_provenance_on_the_rehydrated_producer_still_poisons_the_resume() -> None:
    """The union's fail-closed rule is not weakened by the table carry riding the same
    record: a completed-node record whose `provenance` is `null` (an undetermined
    pre-pause read) poisons the union to `None` even though its `table` restored fine and
    the run completes. The answer then drops from D44 replay — carrying the table name
    must not have made the footprint look determined."""
    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    paused = await _pause_after_producer(detail, FakeScratchClient())
    poisoned = _repatch(paused.completed_nodes_json, 0, provenance=None)

    resumed, _mcp2, _ = await _resume_fresh(
        detail,
        poisoned,
        counts=[_count(1)],
        consumer_result=_rq(["department", "total_earnings"], [["Sales", 100.0]]),
        grain=_rq(["__bp_n", "__bp_d"], [[1, 1]]),
    )
    assert isinstance(resumed, ExecCompleted)
    assert resumed.provenance is None  # fail-closed union, unchanged by the carry


async def test_the_d56_grain_gate_still_bites_on_a_resumed_run() -> None:
    """The restored JOIN buys no exemption from verification. A resumed consumer whose
    grain probe reports MORE rows than distinct grain values (a duplicated
    `Department` — the fan-out D56 exists to catch) fails the gate and returns
    VERIFY_FAILED, not a verified answer with a restored-table alibi."""
    detail = _detail(composes=_APPROVAL_GATED_COMPOSES)
    paused = await _pause_after_producer(detail, FakeScratchClient())

    resumed, _mcp2, _ = await _resume_fresh(
        detail,
        paused.completed_nodes_json,
        counts=[_count(1)],
        consumer_result=_rq(["department", "total_earnings"], [["Sales", 100.0], ["Sales", 5.0]]),
        grain=_rq(["__bp_n", "__bp_d"], [[2, 1]]),  # 2 rows, 1 distinct Department
    )
    assert isinstance(resumed, ExecFailed)
    assert resumed.error_code == VERIFY_FAILED_CODE


# ---------------------------------------------------------------------------
# 6. KNOWN GAPS of the checkpoint-carried table name.
#
#    The test below asserts the CURRENT behaviour and says why it is wrong, so
#    closing the gap makes the test FAIL loudly and forces a deliberate update
#    (the `known_gaps` convention already used elsewhere in this suite).
#
#    Its sibling — a row-TTL-expired intermediate answering `verified` — WAS here
#    and is now closed: the checkpoint carries the producer's row count and the
#    resume re-counts the live table (§5b). What remains is the other axis: the
#    checkpoint pins no VERSION of the definition that produced the table.
# ---------------------------------------------------------------------------


async def test_a_blueprint_redefined_during_the_pause_binds_the_old_table_is_a_known_gap() -> None:
    """The checkpoint names a blueprint ID and a node ORDER; it pins no version of the
    definition. `resume` re-fetches the blueprint from the index, so a definition
    promoted/edited during the pause (a 7-day window on a corpus that is promoted into)
    is walked with the OLD run's rehydrated records.

    For a scalar carry that was already true and bounded — a stale NUMBER re-enters the
    env. Carrying the table name WIDENS it: `$0` now restores a whole TABLE produced by a
    query that the current definition does not contain, and the new consumer JOINs it as
    if it were its own upstream.

    Pinned here with node 0's producer changed from EARN to BONUS between the two turns.
    The resume completes, verified, over the EARN table — and the transparency list even
    reports the EARN SQL, because it too is carried, so the result is internally
    consistent and externally wrong. Closing this (a definition fingerprint on the
    checkpoint, refused on mismatch) makes this test fail."""
    before = _detail(composes=_APPROVAL_GATED_COMPOSES)
    scratch1 = FakeScratchClient()
    paused = await _pause_after_producer(before, scratch1)

    redefined_producer = _PRODUCER_SQL.replace("'EARN'", "'BONUS'")
    after = _detail(
        composes=[
            {**_APPROVAL_GATED_COMPOSES[0], "sql_template": redefined_producer},
            _APPROVAL_GATED_COMPOSES[1],
            _APPROVAL_GATED_COMPOSES[2],
        ]
    )
    resumed, mcp2, _ = await _resume_fresh(
        after,
        paused.completed_nodes_json,
        counts=[_count(1)],
        consumer_result=_rq(["department", "total_earnings"], [["Sales", 100.0]]),
        grain=_rq(["__bp_n", "__bp_d"], [[1, 1]]),
    )
    # KNOWN GAP: the run completes over the PREVIOUS definition's intermediate.
    assert isinstance(resumed, ExecCompleted)
    assert _materialized_name(scratch1).split(".", 1)[1] in _consumer_sql(mcp2)
    assert "'EARN'" in resumed.result_full["sql"][0]  # the superseded producer's SQL
    assert redefined_producer not in resumed.result_full["sql"]
