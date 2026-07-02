"""Adversarial Layer-1 tests for composite/resolve_values.py (D77).

Focuses on gaps the happy-path suite (`test_resolve_values.py`) does not cover:
malformed backing-`runQuery` result shapes, non-string / empty / case-mismatch
args, hostile-fragment-never-in-SQL + dispatch-never-called assertions, the
degrade/top_margin surfaces, and inner transport-error pass-through.

`test_non_int_freq_does_not_crash_the_turn` and
`test_short_row_does_not_crash_the_turn` were the BUG-1/BUG-2 xfail repros (an
unguarded `int()` and an unguarded list index in `_extract_rows`); both are now
FIXED — `_extract_rows` skips/defaults every malformed row, so they pass as
regular tests.

`result_full` is a metadata wrapper `{degraded, ranking, top_margin, values}`
(H3) — assertions read `r.result_full["values"]`.
"""

from __future__ import annotations

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.resolve_values import (
    TOOL_NAME,
    UNKNOWN_TARGET_CODE,
    ResolveValuesComposite,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle

_T = "dbpcm_warehouse.accrual_events"
CATALOG = CatalogHandle(
    {
        _T: {
            "EarnCode": "Nullable(String)",
            "EarnDescription": "Nullable(String)",
            "RequestDate": "Nullable(DateTime64(6))",
            "Hours": "Nullable(Decimal(18, 6))",
        }
    }
)

SECRET_JWT = "eyJhbGciOi.super-secret-jwt-body.sig"
SESSION_ID = "sess-resolve-adv"


def _credentials(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=SECRET_JWT, column_scope=scope)


def _rq(columns: list[str], rows: list[list]) -> dict:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


def _composite(
    *,
    scripted: dict | None = None,
    vectors: dict[str, list[float]] | None = None,
    embed_fail: bool = False,
    embedding: bool = True,
) -> tuple[ResolveValuesComposite, FakeMCPClient]:
    mcp = FakeMCPClient(scripted=scripted or {})
    dispatcher = ToolDispatcher(mcp, CATALOG)
    embedding_client = (
        FakeEmbeddingClient(vectors, dim=2, fail=embed_fail) if embedding else None
    )
    composite = ResolveValuesComposite(
        tool_dispatcher=dispatcher,
        catalog=CATALOG,
        embedding_client=embedding_client,
    )
    return composite, mcp


# ---------------------------------------------------------------------------
# Malformed backing-runQuery result shapes
# ---------------------------------------------------------------------------


async def test_missing_columns_key_is_ok_empty() -> None:
    # No 'columns' key at all -> nothing resolvable -> ok + empty, not a crash.
    composite, _ = _composite(scripted={"runQuery": [{"rows": [["PTO", "x", 1]]}]})
    r = await composite.run({"table": _T, "column": "EarnCode", "concept": "x"}, _credentials())
    assert r.status == "ok"
    assert r.result_full["values"] == []


async def test_missing_rows_key_is_ok_empty() -> None:
    composite, _ = _composite(
        scripted={"runQuery": [{"columns": ["EarnCode", "EarnDescription", "freq"]}]}
    )
    r = await composite.run({"table": _T, "column": "EarnCode", "concept": "x"}, _credentials())
    assert r.status == "ok"
    assert r.result_full["values"] == []


async def test_non_dict_result_is_ok_empty() -> None:
    # A bare list (wrong shape entirely) must not crash — degrade to empty.
    composite, _ = _composite(scripted={"runQuery": [["not", "a", "dict"]]})
    r = await composite.run({"table": _T, "column": "EarnCode", "concept": "x"}, _credentials())
    assert r.status == "ok"
    assert r.result_full["values"] == []


async def test_value_column_absent_from_result_is_ok_empty() -> None:
    # The built SQL SELECTs EarnCode, but a malformed result omits it -> empty.
    composite, _ = _composite(scripted={"runQuery": [_rq(["freq"], [[5]])]})
    r = await composite.run({"table": _T, "column": "EarnCode", "concept": "x"}, _credentials())
    assert r.status == "ok"
    assert r.result_full["values"] == []


async def test_column_order_is_resolved_by_name_not_position() -> None:
    # freq / desc / value in a scrambled order — extraction keys off names.
    result = _rq(
        ["freq", "EarnDescription", "EarnCode"],
        [[10, "paid time off", "PTO"], [3, "overtime", "OT"]],
    )
    composite, _ = _composite(scripted={"runQuery": [result]})
    r = await composite.run({"table": _T, "column": "EarnCode", "concept": "x"}, _credentials())
    values = {row["value"]: row for row in r.result_full["values"]}
    assert set(values) == {"PTO", "OT"}
    assert values["PTO"]["description"] == "paid time off"
    assert values["PTO"]["freq"] == 10


async def test_null_value_cell_is_skipped() -> None:
    result = _rq(
        ["EarnCode", "EarnDescription", "freq"],
        [[None, "orphan desc", 99], ["PTO", "paid time off", 10]],
    )
    composite, _ = _composite(scripted={"runQuery": [result]})
    r = await composite.run({"table": _T, "column": "EarnCode", "concept": "x"}, _credentials())
    assert [row["value"] for row in r.result_full["values"]] == ["PTO"]


async def test_description_none_cell_preserved_as_none() -> None:
    result = _rq(["EarnCode", "EarnDescription", "freq"], [["PTO", None, 10]])
    composite, _ = _composite(scripted={"runQuery": [result]})
    r = await composite.run({"table": _T, "column": "EarnCode", "concept": "x"}, _credentials())
    assert r.result_full["values"][0]["description"] is None


async def test_none_freq_cell_defaults_to_zero() -> None:
    result = _rq(["EarnCode", "EarnDescription", "freq"], [["PTO", "x", None]])
    composite, _ = _composite(scripted={"runQuery": [result]})
    r = await composite.run({"table": _T, "column": "EarnCode", "concept": "x"}, _credentials())
    assert r.result_full["values"][0]["freq"] == 0


async def test_float_freq_is_truncated_to_int() -> None:
    result = _rq(["EarnCode", "EarnDescription", "freq"], [["PTO", "x", 3.9]])
    composite, _ = _composite(scripted={"runQuery": [result]})
    r = await composite.run({"table": _T, "column": "EarnCode", "concept": "x"}, _credentials())
    assert r.result_full["values"][0]["freq"] == 3


async def test_duplicate_values_are_not_deduplicated() -> None:
    # The GROUP BY guarantees distinctness in real results; if a malformed
    # result carries duplicate values, they are ranked independently (documents
    # current behavior — the composite does NOT re-dedupe).
    result = _rq(
        ["EarnCode", "EarnDescription", "freq"],
        [["PTO", "a", 10], ["PTO", "b", 5]],
    )
    composite, _ = _composite(scripted={"runQuery": [result]})
    r = await composite.run({"table": _T, "column": "EarnCode", "concept": "x"}, _credentials())
    assert [row["value"] for row in r.result_full["values"]] == ["PTO", "PTO"]


async def test_non_int_freq_does_not_crash_the_turn() -> None:
    result = _rq(["EarnCode", "EarnDescription", "freq"], [["PTO", "x", "not-a-number"]])
    composite, _ = _composite(scripted={"runQuery": [result]})
    r = await composite.run({"table": _T, "column": "EarnCode", "concept": "x"}, _credentials())
    assert r.status == "ok"


async def test_short_row_does_not_crash_the_turn() -> None:
    # Value column resolves to index 2, but the row has a single cell.
    result = _rq(["EarnDescription", "freq", "EarnCode"], [["x"]])
    composite, _ = _composite(scripted={"runQuery": [result]})
    r = await composite.run({"table": _T, "column": "EarnCode", "concept": "x"}, _credentials())
    assert r.status == "ok"


# ---------------------------------------------------------------------------
# Non-string / empty / case-mismatch args (all fail closed, no dispatch)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        {"table": 123, "column": "EarnCode", "concept": "x"},
        {"table": _T, "column": {"nested": 1}, "concept": "x"},
        {"table": _T, "column": "EarnCode", "concept": 5},
        {"table": ["list"], "column": "EarnCode", "concept": "x"},
        {"table": _T, "column": "EarnCode", "concept": None},
        {"column": "EarnCode", "concept": "x"},  # table missing
        {"table": _T, "concept": "x"},  # column missing
        {"table": _T, "column": "EarnCode"},  # concept missing
    ],
)
async def test_non_string_or_missing_args_fail_closed_no_dispatch(args: dict) -> None:
    composite, mcp = _composite(scripted={"runQuery": []})
    r = await composite.run(args, _credentials())
    assert r.status == "error"
    assert r.error_code == UNKNOWN_TARGET_CODE
    assert r.retryable is True
    assert mcp.calls == []


@pytest.mark.parametrize(
    "args",
    [
        {"table": "", "column": "EarnCode", "concept": "x"},
        {"table": _T, "column": "", "concept": "x"},
        {"table": _T, "column": "EarnCode", "concept": ""},
    ],
)
async def test_empty_string_args_fail_closed_no_dispatch(args: dict) -> None:
    composite, mcp = _composite(scripted={"runQuery": []})
    r = await composite.run(args, _credentials())
    assert r.status == "error"
    assert mcp.calls == []


@pytest.mark.parametrize(
    ("table", "column"),
    [
        (_T, "earncode"),  # column case mismatch (D70 case-sensitive exact)
        (_T, "EARNCODE"),
        ("dbpcm_warehouse.ACCRUAL_EVENTS", "EarnCode"),  # table case mismatch
        ("Accrual_Events", "EarnCode"),  # bare-name case mismatch
    ],
)
async def test_case_mismatch_identifiers_fail_closed(table: str, column: str) -> None:
    composite, mcp = _composite(scripted={"runQuery": []})
    r = await composite.run({"table": table, "column": column, "concept": "x"}, _credentials())
    assert r.status == "error"
    assert mcp.calls == []


@pytest.mark.parametrize(
    ("table", "column"),
    [
        (" accrual_events", "EarnCode"),  # leading whitespace
        ("accrual_events ", "EarnCode"),  # trailing whitespace
        (_T, " EarnCode"),
        (_T, "EarnCode "),
        (_T, '"EarnCode"'),  # quoted identifier
        ("`dbpcm_warehouse`.`accrual_events`", "EarnCode"),  # backtick-quoted
        ("wrongdb.accrual_events", "EarnCode"),  # qualified, wrong database
    ],
)
async def test_whitespace_quoted_or_wrong_db_fail_closed(table: str, column: str) -> None:
    composite, mcp = _composite(scripted={"runQuery": []})
    r = await composite.run({"table": table, "column": column, "concept": "x"}, _credentials())
    assert r.status == "error"
    assert mcp.calls == []


# ---------------------------------------------------------------------------
# Hostile fragment never in generated SQL; dispatch skipped on validation fail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile_column",
    [
        "EarnCode; DROP TABLE accrual_events",
        "EarnCode) UNION SELECT jwt FROM secrets --",
        "*",
        "1=1",
        "EarnCode/**/OR/**/1",
    ],
)
async def test_hostile_column_never_dispatched(hostile_column: str) -> None:
    composite, mcp = _composite(scripted={"runQuery": []})
    r = await composite.run(
        {"table": _T, "column": hostile_column, "concept": "x"}, _credentials()
    )
    assert r.status == "error"
    assert mcp.calls == []  # hostile fragment never reached the MCP at all


async def test_hostile_concept_present_but_absent_from_sql() -> None:
    result = _rq(["EarnCode", "EarnDescription", "freq"], [["PTO", "x", 3]])
    composite, mcp = _composite(scripted={"runQuery": [result]})
    nasty = "'; DROP TABLE accrual_events; -- union select password"
    await composite.run({"table": _T, "column": "EarnCode", "concept": nasty}, _credentials())
    sql = mcp.calls[0].args["sql"]
    assert "DROP TABLE" not in sql
    assert "password" not in sql
    assert "union" not in sql.lower()


async def test_hostile_period_bounds_escaped_never_raw_in_sql() -> None:
    result = _rq(["EarnCode", "EarnDescription", "freq"], [["PTO", "x", 3]])
    composite, mcp = _composite(scripted={"runQuery": [result]})
    await composite.run(
        {
            "table": _T,
            "column": "EarnCode",
            "concept": "leave",
            "period": {"column": "RequestDate", "start": "2026'); DROP TABLE x; --"},
        },
        _credentials(),
    )
    sql = mcp.calls[0].args["sql"]
    # The raw (unescaped) injection string must not appear; only a properly
    # doubled-quote SQL literal.
    assert "2026'); DROP TABLE x; --'" not in sql.replace("''", "\x00")
    assert "'2026''); DROP TABLE x; --'" in sql


# ---------------------------------------------------------------------------
# period edge cases through run()
# ---------------------------------------------------------------------------


async def test_period_column_equal_to_value_column_is_allowed() -> None:
    result = _rq(["EarnCode", "EarnDescription", "freq"], [["PTO", "x", 3]])
    composite, mcp = _composite(scripted={"runQuery": [result]})
    r = await composite.run(
        {
            "table": _T,
            "column": "EarnCode",
            "concept": "leave",
            "period": {"column": "EarnCode", "start": "AAA", "end": "ZZZ"},
        },
        _credentials(),
    )
    assert r.status == "ok"
    sql = mcp.calls[0].args["sql"]
    assert "WHERE EarnCode >= 'AAA' AND EarnCode <= 'ZZZ'" in sql


async def test_period_start_after_end_still_builds_valid_sql() -> None:
    # No runtime validation of start<=end (design §2.4 — bounds are literals);
    # the query simply returns no rows at execution time.
    result = _rq(["EarnCode", "EarnDescription", "freq"], [])
    composite, mcp = _composite(scripted={"runQuery": [result]})
    r = await composite.run(
        {
            "table": _T,
            "column": "EarnCode",
            "concept": "leave",
            "period": {"column": "RequestDate", "start": "2026-12-31", "end": "2026-01-01"},
        },
        _credentials(),
    )
    assert r.status == "ok"
    assert "WHERE RequestDate >= '2026-12-31' AND RequestDate <= '2026-01-01'" in (
        mcp.calls[0].args["sql"]
    )


async def test_period_unicode_bounds_are_literal_bound() -> None:
    result = _rq(["EarnCode", "EarnDescription", "freq"], [["PTO", "x", 3]])
    composite, mcp = _composite(scripted={"runQuery": [result]})
    await composite.run(
        {
            "table": _T,
            "column": "EarnCode",
            "concept": "leave",
            "period": {"column": "RequestDate", "start": "2026-01-01ünïçödé"},
        },
        _credentials(),
    )
    assert "'2026-01-01ünïçödé'" in mcp.calls[0].args["sql"]


# ---------------------------------------------------------------------------
# Degrade / top_margin / inner error surfaces (via resolve())
# ---------------------------------------------------------------------------


async def test_resolve_degraded_flag_true_on_embedding_failure() -> None:
    result = _rq(["EarnCode", "EarnDescription", "freq"], [["a", "d", 1], ["b", "e", 2]])
    composite, _ = _composite(scripted={"runQuery": [result]}, embed_fail=True)
    outcome = await composite.resolve(
        table=_T, column="EarnCode", concept="x", period=None, credentials=_credentials()
    )
    assert outcome.status == "ok"
    assert outcome.degraded is True


async def test_resolve_top_margin_computed_for_multi_row() -> None:
    result = _rq(
        ["EarnCode", "EarnDescription", "freq"], [["a", "d", 100], ["b", "e", 1]]
    )
    composite, _ = _composite(scripted={"runQuery": [result]}, embedding=False)
    outcome = await composite.resolve(
        table=_T, column="EarnCode", concept="x", period=None, credentials=_credentials()
    )
    assert outcome.top_margin is not None
    assert outcome.top_margin == round(outcome.values[0].score - outcome.values[1].score, 4)


async def test_resolve_top_margin_none_for_single_row() -> None:
    result = _rq(["EarnCode", "EarnDescription", "freq"], [["a", "d", 100]])
    composite, _ = _composite(scripted={"runQuery": [result]}, embedding=False)
    outcome = await composite.resolve(
        table=_T, column="EarnCode", concept="x", period=None, credentials=_credentials()
    )
    assert outcome.top_margin is None


async def test_inner_transport_error_passes_through_as_error() -> None:
    # A raw (non-MCPToolError) transport exception -> dispatcher returns
    # status="error"/INTERNAL_TRANSPORT_ERROR; the composite must surface it as
    # error (not ok, not degraded), tagged resolveValues, with no provenance.
    boom = ConnectionError("connection reset by peer")
    composite, _ = _composite(scripted={"runQuery": [boom]})
    r = await composite.run(
        {"table": _T, "column": "EarnCode", "concept": "leave"}, _credentials()
    )
    assert r.status == "error"
    assert r.tool_name == TOOL_NAME
    assert r.error_code == "INTERNAL_TRANSPORT_ERROR"
    assert r.provenance is None
    # Raw exception text never surfaces to the model.
    assert "connection reset by peer" not in (r.user_message or "")
