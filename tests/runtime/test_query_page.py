"""Tests for runtime/query_page.py + the `POST /query/page` endpoint.

The endpoint executes the model-designated `answer_sql` so the UI can page the
answer table (what replaced the fixed ~20-row `result_table` preview). It must add
NO authority over the agent's own path, so the tests below assert on WHAT REACHES
THE DISPATCHER, not just on status codes.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from data_agent.runtime import app as app_module
from data_agent.runtime.app import create_app
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.mcp.client import MCPToolSpec
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.query_page import (
    MAX_PAGE_SIZE,
    QueryPageError,
    build_page_sql,
    clamp_page_params,
)
from data_agent.runtime.session.memory_store import InMemorySessionStore

SESSION_ID = "sess-query-page"
HEADERS = {"Authorization": "Bearer test-jwt", "X-Session-Id": SESSION_ID}


# --- build_page_sql (pure) -------------------------------------------------


def test_build_page_sql_wraps_as_a_subquery() -> None:
    out = build_page_sql("SELECT a, count() AS n FROM db.t GROUP BY a", limit=50, offset=100)
    assert out == (
        "SELECT * FROM (SELECT a, count() AS n FROM db.t GROUP BY a) AS page_src "
        "LIMIT 50 OFFSET 100"
    )


def test_build_page_sql_wraps_rather_than_rewrites_an_inner_limit() -> None:
    """An inner LIMIT is left alone — it bounds the inner result, and the OUTER
    limit is ours, so a page can never exceed what we asked for regardless."""
    out = build_page_sql("SELECT * FROM db.t LIMIT 10", limit=1000, offset=0)
    assert out.startswith("SELECT * FROM (SELECT * FROM db.t LIMIT 10) AS page_src")
    assert out.endswith("LIMIT 1000 OFFSET 0")


@pytest.mark.parametrize(
    "bad",
    [
        "INSERT INTO db.t VALUES (1)",
        "DROP TABLE db.t",
        "ALTER TABLE db.t ADD COLUMN x String",
        # Multi-statement: the classic "…; DROP …" shape must die at parse time,
        # not rely on the MCP's read-only mode as the only guard.
        "SELECT 1; DROP TABLE db.t",
        "",
        "   ",
        "not sql at all (((",
    ],
)
def test_build_page_sql_rejects_non_select_and_unparseable(bad: str) -> None:
    with pytest.raises(QueryPageError):
        build_page_sql(bad, limit=10, offset=0)


def test_build_page_sql_error_never_echoes_the_offending_sql() -> None:
    """The message reaches the client, so it must not quote the statement back —
    a sqlglot parse error otherwise includes a fragment of it."""
    secret = "SELECT tOp_SeCrEt_MaRkEr FROM (((("
    with pytest.raises(QueryPageError) as exc:
        build_page_sql(secret, limit=10, offset=0)
    assert "tOp_SeCrEt_MaRkEr" not in str(exc.value)


def test_clamp_page_params_is_total_and_bounded() -> None:
    # Garbage never raises — a bad paging param is UI plumbing, not a 4xx.
    assert clamp_page_params(None, None) == (100, 0)
    assert clamp_page_params("abc", "xyz") == (100, 0)
    assert clamp_page_params(-5, -99) == (1, 0)
    assert clamp_page_params(10**9, 20) == (MAX_PAGE_SIZE, 20)


# --- the endpoint ----------------------------------------------------------


def _client(monkeypatch, *, scripted: dict | None = None) -> tuple[TestClient, FakeMCPClient]:
    monkeypatch.setattr(app_module, "verify_jwt", lambda *a, **k: frozenset())
    mcp = FakeMCPClient(
        tools=[MCPToolSpec(name="runQuery", description="", input_schema={"type": "object"})],
        scripted=scripted
        or {
            "runQuery": [
                {
                    "columns": ["department", "n"],
                    "rows": [["Sales", 3], ["Eng", 5]],
                    "row_count": 2,
                    "truncated": False,
                }
            ]
        },
    )
    app = create_app(
        settings=RuntimeSettings(_env_file=None, discovery_emulation_enabled=False),
        session_store=InMemorySessionStore(),
        mcp_client=mcp,
        model_client=ScriptedModelClient([ModelTurnResult(assistant_text="hi")]),
        catalog=CatalogHandle({}),
    )
    return TestClient(app), mcp


def test_endpoint_returns_a_page_of_rows(monkeypatch) -> None:
    client, _ = _client(monkeypatch)
    resp = client.post(
        "/query/page",
        json={"sql": "SELECT department, count() AS n FROM db.t GROUP BY department",
              "limit": 2, "offset": 0},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["columns"] == ["department", "n"]
    assert body["rows"] == [["Sales", 3], ["Eng", 5]]
    assert body["limit"] == 2
    assert body["offset"] == 0
    # A full page implies there may be more — a hint, not a count.
    assert body["has_more"] is True


def test_endpoint_dispatches_the_wrapped_sql_not_the_raw_sql(monkeypatch) -> None:
    """THE POINT: paging bounds must be OURS. Assert on what actually reached the
    dispatcher — a green status code would pass even if the raw SQL were run."""
    client, mcp = _client(monkeypatch)
    client.post(
        "/query/page",
        json={"sql": "SELECT a FROM db.t", "limit": 25, "offset": 75},
        headers=HEADERS,
    )
    assert len(mcp.calls) == 1
    dispatched = mcp.calls[0].args["sql"]
    assert dispatched == "SELECT * FROM (SELECT a FROM db.t) AS page_src LIMIT 25 OFFSET 75"


def test_endpoint_clamps_an_absurd_page_size_before_dispatch(monkeypatch) -> None:
    client, mcp = _client(monkeypatch)
    resp = client.post(
        "/query/page", json={"sql": "SELECT a FROM db.t", "limit": 10**9}, headers=HEADERS
    )
    assert resp.json()["limit"] == MAX_PAGE_SIZE
    assert f"LIMIT {MAX_PAGE_SIZE}" in mcp.calls[0].args["sql"]


def test_endpoint_rejects_a_write_without_dispatching(monkeypatch) -> None:
    """A write must never reach the MCP at all — the endpoint is the earlier, more
    specific rejection, not a reliance on read-only mode downstream."""
    client, mcp = _client(monkeypatch)
    resp = client.post("/query/page", json={"sql": "DROP TABLE db.t"}, headers=HEADERS)
    assert resp.status_code == 400
    assert mcp.calls == []
    assert "read-only" in resp.json()["error"].lower()


def test_endpoint_rejects_a_multi_statement_payload_without_dispatching(monkeypatch) -> None:
    client, mcp = _client(monkeypatch)
    resp = client.post(
        "/query/page", json={"sql": "SELECT 1; DROP TABLE db.t"}, headers=HEADERS
    )
    assert resp.status_code == 400
    assert mcp.calls == []


def test_endpoint_error_body_never_leaks_the_sql(monkeypatch) -> None:
    client, _ = _client(monkeypatch)
    resp = client.post(
        "/query/page", json={"sql": "SELECT tOp_SeCrEt_MaRkEr FROM (((("}, headers=HEADERS
    )
    assert resp.status_code == 400
    assert "tOp_SeCrEt_MaRkEr" not in json.dumps(resp.json())


def test_endpoint_requires_auth_like_every_other_route(monkeypatch) -> None:
    client, _ = _client(monkeypatch)
    assert client.post("/query/page", json={"sql": "SELECT 1"}).status_code == 401
    assert (
        client.post(
            "/query/page",
            json={"sql": "SELECT 1"},
            headers={"Authorization": "Bearer t"},
        ).status_code
        == 400  # missing X-Session-Id
    )
