"""Unit tests for composite/resolve_values.py (Layer 1 — FakeMCP + FakeEmbedding)."""

from __future__ import annotations

import json
from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.resolve_values import (
    TOOL_NAME,
    UNKNOWN_TARGET_CODE,
    Period,
    ResolveValuesComposite,
    parse_period,
)
from data_agent.runtime.composite.sql_builder import TargetValidationError
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.client import MCPToolError
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
SESSION_ID = "sess-resolve"


def _credentials(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=SECRET_JWT, column_scope=scope)


def _run_query_result(columns: list[str], rows: list[list]) -> dict:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


def _composite(
    *,
    scripted: dict | None = None,
    vectors: dict[str, list[float]] | None = None,
    embed_fail: bool = False,
    embedding: bool = True,
    observer: Any = None,
) -> tuple[ResolveValuesComposite, FakeMCPClient]:
    mcp = FakeMCPClient(scripted=scripted or {})
    # The SAME observer on both, exactly as `app.py` wires them: the composite
    # emits its own `resolveValues` progress events, the dispatcher would emit the
    # inner `runQuery` ones (gated — see the progress test at the end of the file).
    dispatcher = (
        ToolDispatcher(mcp, CATALOG, observer=observer)
        if observer is not None
        else ToolDispatcher(mcp, CATALOG)
    )
    embedding_client = (
        FakeEmbeddingClient(vectors, dim=2, fail=embed_fail) if embedding else None
    )
    kwargs: dict[str, Any] = {}
    if observer is not None:
        kwargs["observer"] = observer
    composite = ResolveValuesComposite(
        tool_dispatcher=dispatcher,
        catalog=CATALOG,
        embedding_client=embedding_client,
        query_limit=200,
        top_k=10,
        similarity_weight=0.7,
        **kwargs,
    )
    return composite, mcp


# --- happy path -------------------------------------------------------------


async def test_run_happy_path_ranked_top_k_with_description() -> None:
    result = _run_query_result(
        ["EarnCode", "EarnDescription", "freq"],
        [["PTO", "paid time off", 10], ["OT", "overtime", 1000]],
    )
    vectors = {
        "paid leave": [1.0, 0.0],
        "PTO: paid time off": [1.0, 0.0],
        "OT: overtime": [0.0, 1.0],
    }
    composite, mcp = _composite(scripted={"runQuery": [result]}, vectors=vectors)

    tool_result = await composite.run(
        {"table": "accrual_events", "column": "EarnCode", "concept": "paid leave"},
        _credentials(),
    )

    assert tool_result.status == "ok"
    assert tool_result.tool_name == TOOL_NAME
    assert tool_result.result_full["values"][0]["value"] == "PTO"  # semantic winner
    assert tool_result.result_full["values"][1]["value"] == "OT"
    assert set(tool_result.result_full["values"][0]) == {"value", "description", "score", "freq"}
    # Inner runQuery actually fired with a built SQL, once.
    assert [c.tool_name for c in mcp.calls] == ["runQuery"]
    assert "EarnDescription" in mcp.calls[0].args["sql"]


async def test_run_provenance_is_inner_run_query_provenance() -> None:
    result = _run_query_result(
        ["EarnCode", "EarnDescription", "freq"], [["PTO", "paid time off", 10]]
    )
    composite, _ = _composite(scripted={"runQuery": [result]})
    tool_result = await composite.run(
        {"table": _T, "column": "EarnCode", "concept": "leave"}, _credentials()
    )
    assert tool_result.provenance is not None
    assert (_T, "EarnCode") in tool_result.provenance
    assert (_T, "EarnDescription") in tool_result.provenance


async def test_run_value_only_fallback_no_description_column() -> None:
    result = _run_query_result(["Hours", "freq"], [["8", 40], ["4", 10]])
    composite, mcp = _composite(scripted={"runQuery": [result]})
    tool_result = await composite.run(
        {"table": _T, "column": "Hours", "concept": "full day"}, _credentials()
    )
    assert tool_result.status == "ok"
    assert all(row["description"] is None for row in tool_result.result_full["values"])
    assert "EarnDescription" not in mcp.calls[0].args["sql"]


# --- validation / fail-closed -----------------------------------------------


async def test_run_unknown_table_fails_closed() -> None:
    composite, mcp = _composite(scripted={"runQuery": []})
    tool_result = await composite.run(
        {"table": "no_such", "column": "EarnCode", "concept": "x"}, _credentials()
    )
    assert tool_result.status == "error"
    assert tool_result.error_code == UNKNOWN_TARGET_CODE
    assert tool_result.retryable is True
    assert mcp.calls == []  # never reached the MCP


async def test_run_unknown_column_fails_closed() -> None:
    composite, mcp = _composite(scripted={"runQuery": []})
    tool_result = await composite.run(
        {"table": _T, "column": "Nope", "concept": "x"}, _credentials()
    )
    assert tool_result.status == "error"
    assert tool_result.error_code == UNKNOWN_TARGET_CODE
    assert mcp.calls == []


async def test_run_injection_column_never_reaches_mcp() -> None:
    composite, mcp = _composite(scripted={"runQuery": []})
    tool_result = await composite.run(
        {"table": _T, "column": "c) FROM x --", "concept": "x"}, _credentials()
    )
    assert tool_result.status == "error"
    assert tool_result.error_code == UNKNOWN_TARGET_CODE
    assert mcp.calls == []


async def test_run_injection_table_never_reaches_mcp() -> None:
    composite, mcp = _composite(scripted={"runQuery": []})
    tool_result = await composite.run(
        {"table": "Emp; DROP", "column": "EarnCode", "concept": "x"}, _credentials()
    )
    assert tool_result.status == "error"
    assert mcp.calls == []


async def test_run_bad_period_shape_fails_closed() -> None:
    composite, mcp = _composite(scripted={"runQuery": []})
    tool_result = await composite.run(
        {"table": _T, "column": "EarnCode", "concept": "x", "period": "2026"},
        _credentials(),
    )
    assert tool_result.status == "error"
    assert tool_result.error_code == UNKNOWN_TARGET_CODE
    assert mcp.calls == []


async def test_run_bad_period_column_fails_closed() -> None:
    composite, mcp = _composite(scripted={"runQuery": []})
    tool_result = await composite.run(
        {
            "table": _T,
            "column": "EarnCode",
            "concept": "x",
            "period": {"column": "NopeDate", "start": "2026-01-01"},
        },
        _credentials(),
    )
    assert tool_result.status == "error"
    assert mcp.calls == []


async def test_run_good_period_builds_where_and_reaches_mcp() -> None:
    result = _run_query_result(["EarnCode", "EarnDescription", "freq"], [["PTO", "x", 3]])
    composite, mcp = _composite(scripted={"runQuery": [result]})
    tool_result = await composite.run(
        {
            "table": _T,
            "column": "EarnCode",
            "concept": "leave",
            "period": {"column": "RequestDate", "start": "2026-01-01", "end": "2026-03-31"},
        },
        _credentials(),
    )
    assert tool_result.status == "ok"
    sql = mcp.calls[0].args["sql"]
    assert "WHERE RequestDate >= '2026-01-01' AND RequestDate <= '2026-03-31'" in sql


async def test_concept_with_sql_metacharacters_never_in_sql() -> None:
    result = _run_query_result(["EarnCode", "EarnDescription", "freq"], [["PTO", "x", 3]])
    composite, mcp = _composite(scripted={"runQuery": [result]})
    nasty = "'; DROP TABLE accrual_events; --"
    await composite.run(
        {"table": _T, "column": "EarnCode", "concept": nasty}, _credentials()
    )
    assert "DROP TABLE" not in mcp.calls[0].args["sql"]


# --- denial / empty / degrade -----------------------------------------------


async def test_run_inner_denial_passes_through() -> None:
    denial = MCPToolError("COLUMN_SCOPE_VIOLATION", "[COLUMN_SCOPE_VIOLATION] nope")
    composite, _ = _composite(scripted={"runQuery": [denial]})
    tool_result = await composite.run(
        {"table": _T, "column": "EarnCode", "concept": "leave"}, _credentials()
    )
    assert tool_result.status == "denied"
    assert tool_result.tool_name == TOOL_NAME
    assert tool_result.error_code == "COLUMN_SCOPE_VIOLATION"
    assert tool_result.retryable is False
    assert tool_result.provenance is None


async def test_run_empty_result_is_ok_empty() -> None:
    result = _run_query_result(["EarnCode", "EarnDescription", "freq"], [])
    composite, _ = _composite(scripted={"runQuery": [result]})
    tool_result = await composite.run(
        {"table": _T, "column": "EarnCode", "concept": "leave"}, _credentials()
    )
    assert tool_result.status == "ok"
    assert tool_result.result_full["values"] == []


async def test_run_embedding_failure_degrades_to_freq_only() -> None:
    result = _run_query_result(
        ["EarnCode", "EarnDescription", "freq"],
        [["rare", "a", 1], ["common", "b", 100]],
    )
    composite, _ = _composite(scripted={"runQuery": [result]}, embed_fail=True)
    tool_result = await composite.run(
        {"table": _T, "column": "EarnCode", "concept": "leave"}, _credentials()
    )
    assert tool_result.status == "ok"
    # Freq-only ordering: common (100) before rare (1).
    assert [r["value"] for r in tool_result.result_full["values"]] == ["common", "rare"]


async def test_run_no_embedding_client_degrades_to_freq_only() -> None:
    result = _run_query_result(
        ["EarnCode", "EarnDescription", "freq"],
        [["rare", "a", 1], ["common", "b", 100]],
    )
    composite, _ = _composite(scripted={"runQuery": [result]}, embedding=False)
    tool_result = await composite.run(
        {"table": _T, "column": "EarnCode", "concept": "leave"}, _credentials()
    )
    assert [r["value"] for r in tool_result.result_full["values"]] == ["common", "rare"]


# --- limits / credential non-leak / programmatic ----------------------------


async def test_run_query_limit_applied() -> None:
    result = _run_query_result(["EarnCode", "EarnDescription", "freq"], [["PTO", "x", 1]])
    mcp = FakeMCPClient(scripted={"runQuery": [result]})
    composite = ResolveValuesComposite(
        tool_dispatcher=ToolDispatcher(mcp, CATALOG),
        catalog=CATALOG,
        embedding_client=FakeEmbeddingClient(dim=2),
        query_limit=42,
        top_k=1,
    )
    await composite.run({"table": _T, "column": "EarnCode", "concept": "x"}, _credentials())
    assert "LIMIT 42" in mcp.calls[0].args["sql"]


async def test_run_top_k_respected() -> None:
    rows = [[f"c{i}", f"d{i}", i + 1] for i in range(15)]
    result = _run_query_result(["EarnCode", "EarnDescription", "freq"], rows)
    mcp = FakeMCPClient(scripted={"runQuery": [result]})
    composite = ResolveValuesComposite(
        tool_dispatcher=ToolDispatcher(mcp, CATALOG),
        catalog=CATALOG,
        embedding_client=FakeEmbeddingClient(dim=2),
        query_limit=200,
        top_k=3,
    )
    tool_result = await composite.run(
        {"table": _T, "column": "EarnCode", "concept": "x"}, _credentials()
    )
    assert len(tool_result.result_full["values"]) == 3


async def test_tool_result_never_leaks_credentials() -> None:
    result = _run_query_result(["EarnCode", "EarnDescription", "freq"], [["PTO", "x", 1]])
    composite, _ = _composite(scripted={"runQuery": [result]})
    tool_result = await composite.run(
        {"table": _T, "column": "EarnCode", "concept": "leave"}, _credentials()
    )
    blob = json.dumps(
        {
            "status": tool_result.status,
            "error_code": tool_result.error_code,
            "user_message": tool_result.user_message,
            "provenance": sorted(tool_result.provenance or []),
            "result_full": tool_result.result_full,
            "preview": tool_result.result_preview.to_doc() if tool_result.result_preview else None,
        },
        default=str,
    )
    assert SECRET_JWT not in blob
    assert SESSION_ID not in blob


async def test_resolve_programmatic_returns_typed_outcome() -> None:
    result = _run_query_result(
        ["EarnCode", "EarnDescription", "freq"], [["PTO", "paid time off", 10]]
    )
    composite, _ = _composite(scripted={"runQuery": [result]})
    outcome = await composite.resolve(
        table=_T, column="EarnCode", concept="leave", period=None, credentials=_credentials()
    )
    # No ToolResult wrapping — the D67 programmatic surface.
    assert outcome.status == "ok"
    assert outcome.values[0].value == "PTO"
    assert outcome.provenance is not None
    assert outcome.degraded is False


# --- parse_period unit ------------------------------------------------------


def test_parse_period_none() -> None:
    assert parse_period(None) is None


def test_parse_period_valid() -> None:
    period = parse_period({"column": "RequestDate", "start": "2026-01-01"})
    assert period == Period(column="RequestDate", start="2026-01-01", end=None)


def test_parse_period_missing_column_raises() -> None:
    try:
        parse_period({"start": "2026-01-01"})
        raise AssertionError("expected TargetValidationError")
    except TargetValidationError:
        pass


# --- H3: degraded/ranking metadata surfaced to the model --------------------


async def test_degraded_flag_and_ranking_mode_surface_in_tool_result() -> None:
    result = _run_query_result(
        ["EarnCode", "EarnDescription", "freq"], [["rare", "a", 1], ["common", "b", 100]]
    )
    composite, _ = _composite(scripted={"runQuery": [result]}, embed_fail=True)
    tool_result = await composite.run(
        {"table": _T, "column": "EarnCode", "concept": "leave"}, _credentials()
    )
    assert tool_result.status == "ok"
    # result_full is the metadata wrapper (H3), not a bare list.
    assert tool_result.result_full["degraded"] is True
    assert tool_result.result_full["ranking"] == "freq_only"
    # The model sees only result_preview — the degraded flag must reach it.
    cell = tool_result.result_preview.preview_rows[0][0]
    assert cell["degraded"] is True
    assert cell["ranking"] == "freq_only"


async def test_non_degraded_ranking_mode_is_semantic_freq() -> None:
    result = _run_query_result(
        ["EarnCode", "EarnDescription", "freq"], [["PTO", "paid time off", 10]]
    )
    composite, _ = _composite(scripted={"runQuery": [result]})
    tool_result = await composite.run(
        {"table": _T, "column": "EarnCode", "concept": "leave"}, _credentials()
    )
    assert tool_result.result_full["degraded"] is False
    assert tool_result.result_full["ranking"] == "semantic+freq"


# --- M1: description column out of scope -> value-only, no denial -----------


async def test_description_col_out_of_scope_falls_back_to_value_only() -> None:
    # Scope grants the value column but NOT its sibling description column.
    scope = frozenset({f"{_T}.EarnCode"})
    result = _run_query_result(["EarnCode", "freq"], [["PTO", 10]])
    composite, mcp = _composite(scripted={"runQuery": [result]})
    tool_result = await composite.run(
        {"table": _T, "column": "EarnCode", "concept": "leave"}, _credentials(scope)
    )
    assert tool_result.status == "ok"
    sql = mcp.calls[0].args["sql"]
    # Availability pre-check dropped EarnDescription -> value-only query built,
    # so the MCP never denies on the out-of-scope sibling.
    assert "EarnDescription" not in sql


async def test_description_col_in_scope_is_used() -> None:
    scope = frozenset({f"{_T}.EarnCode", f"{_T}.EarnDescription"})
    result = _run_query_result(["EarnCode", "EarnDescription", "freq"], [["PTO", "x", 10]])
    composite, mcp = _composite(scripted={"runQuery": [result]})
    await composite.run(
        {"table": _T, "column": "EarnCode", "concept": "leave"}, _credentials(scope)
    )
    assert "EarnDescription" in mcp.calls[0].args["sql"]


# --- M2: mismatched-length embedding vectors -> degrade, never crash --------


async def test_mismatched_length_vectors_degrade_to_freq_only() -> None:
    result = _run_query_result(
        ["EarnCode", "EarnDescription", "freq"], [["rare", "a", 1], ["common", "b", 100]]
    )
    # concept vector length 1, row vectors length 2 -> shape mismatch.
    vectors = {
        "leave": [1.0],
        "rare: a": [1.0, 0.0],
        "common: b": [0.0, 1.0],
    }
    composite, _ = _composite(scripted={"runQuery": [result]}, vectors=vectors)
    tool_result = await composite.run(
        {"table": _T, "column": "EarnCode", "concept": "leave"}, _credentials()
    )
    assert tool_result.status == "ok"
    assert tool_result.result_full["degraded"] is True
    assert [r["value"] for r in tool_result.result_full["values"]] == ["common", "rare"]


# --- UI progress: the inner runQuery is an implementation detail -------------


class _RecordingObserver:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event: str, payload: dict) -> None:
        self.events.append((event, dict(payload)))


async def test_the_inner_run_query_emits_no_ui_progress_of_its_own() -> None:
    """`resolveValues` is ONE step to the user. Its inner `runQuery` runs through
    the shared dispatcher, whose `tool_dispatch_*` events are UI progress labels —
    left ungated the UI painted "running runQuery…" inside "running
    resolveValues…", advertising that the lookup is SQL over an internal table.

    The composite's OWN start/ok events are unaffected: the step the model asked
    for is still narrated.
    """
    observer = _RecordingObserver()
    result = _run_query_result(["EarnCode", "EarnDescription", "freq"], [["PTO", "x", 10]])
    composite, mcp = _composite(scripted={"runQuery": [result]}, observer=observer)

    tool_result = await composite.run(
        {"table": _T, "column": "EarnCode", "concept": "leave"}, _credentials()
    )

    assert tool_result.status == "ok"
    assert [c.tool_name for c in mcp.calls] == ["runQuery"]  # it really did run
    dispatch_events = [
        (event, payload) for event, payload in observer.events if event.startswith("tool_dispatch_")
    ]
    assert dispatch_events, "the composite's own progress events must still fire"
    assert {payload["tool_name"] for _event, payload in dispatch_events} == {TOOL_NAME}


async def test_an_inner_denial_still_reports_under_the_composite_tool_name() -> None:
    """Control flow unchanged: the inner denial is passed through and narrated as
    `resolveValues`, never as a silent step and never as `runQuery`."""
    observer = _RecordingObserver()
    composite, _mcp = _composite(
        scripted={"runQuery": [MCPToolError("COLUMN_SCOPE_VIOLATION", "denied")]},
        observer=observer,
    )

    tool_result = await composite.run(
        {"table": _T, "column": "EarnCode", "concept": "leave"}, _credentials()
    )

    assert tool_result.status == "denied"
    denials = [
        payload for event, payload in observer.events if event == "tool_dispatch_denied"
    ]
    assert [payload["tool_name"] for payload in denials] == [TOOL_NAME]
