"""Layer-2 integration — the `resolveValues` composite's MODEL-FACING tool path
(D77/D85) exercised END-TO-END through the live stack:

    ResolveValuesComposite.run(model_args) -> ToolResult
        -> ToolDispatcher.dispatch("runQuery", <DISTINCT-domain SQL>, …)
            -> RealMCPClient ↔ live clickhouse-api MCP ↔ real ClickHouse
        -> real HttpEmbeddingClient ranks the live DISTINCT domain by `concept`.

The D67 work proved `resolveValues.resolve()` (the typed, programmatic hook)
against real infra; this closes the remaining gap by driving the SAME
composite through its `run()` entry — the public method the agent loop's tool
registry calls — and asserting on the returned `ToolResult` (NOT `ResolveOutcome`).
Nothing below the composite is faked: real MCP, real ClickHouse, real embedder,
a real JWT minted by the token IdP.

Skip-guarded on MCP_TEST_URL + EMBEDDING_TEST_URL; `uv run pytest` with no live
stack stays fully green. Run with the stack up:

    MCP_TEST_URL=http://localhost:18090/mcp \
    EMBEDDING_TEST_URL=http://localhost:18003/embed \
        uv run pytest tests/integration/test_resolve_values_live.py -v

This module needs NEITHER neo4j NOR the corpus loader — the payroll DISTINCT
domain lives in ClickHouse (docker seed), the target set comes from the runtime
`CatalogHandle`, and no blueprint/vector-index path is touched.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.resolve_values import (
    TOOL_NAME,
    ResolveValuesComposite,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolResult
from data_agent.runtime.mcp.client import MCPToolSpec
from data_agent.runtime.mcp.real_client import RealMCPClient
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.provenance.catalog_handle import load_catalog_handle

from .conftest import Mint

pytestmark = pytest.mark.skipif(
    not (os.environ.get("MCP_TEST_URL") and os.environ.get("EMBEDDING_TEST_URL")),
    reason="Requires live clickhouse-api MCP + embedding API "
    "(set MCP_TEST_URL + EMBEDDING_TEST_URL).",
)

_MODEL = "all-mpnet-base-v2"
_P = "dbpcm_warehouse.payroll"
_COLUMN = "RegisterType"
# The D67-proven concept: the seed's DISTINCT RegisterType domain is the
# catalog-faithful set {EARN, EETAX, DDUCT, NETPAYDIST, EEBEN, ERTAX} (value-only
# ranking — no sibling description column), and "earnings" must rank EARN above a
# deduction/tax code such as DDUCT under the real embedder.
_EARN_CONCEPT = "earnings"


def _embedder() -> HttpEmbeddingClient:
    return HttpEmbeddingClient(
        url=os.environ["EMBEDDING_TEST_URL"],
        api_key=os.environ.get("EMBEDDING_TEST_API_KEY", ""),
        model=_MODEL,
        timeout_seconds=30.0,
    )


class _CapturingMCPClient:
    """Wraps `RealMCPClient`, recording every dispatched `(tool_name, args)` so
    the test can prove the concept NEVER reaches the backing SQL (D10). Pure
    passthrough otherwise — the real transport still runs unchanged."""

    def __init__(self, inner: RealMCPClient) -> None:
        self._inner = inner
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(
        self, tool_name: str, args: dict[str, Any], *, jwt: str, session_id: str
    ) -> dict[str, Any] | list[Any]:
        self.calls.append((tool_name, args))
        return await self._inner.call_tool(
            tool_name, args, jwt=jwt, session_id=session_id
        )

    async def list_tools(self, *, jwt: str, session_id: str) -> list[MCPToolSpec]:
        return await self._inner.list_tools(jwt=jwt, session_id=session_id)


def _composite() -> tuple[ResolveValuesComposite, _CapturingMCPClient]:
    catalog = load_catalog_handle()
    client = _CapturingMCPClient(RealMCPClient(os.environ["MCP_TEST_URL"]))
    dispatcher = ToolDispatcher(client, catalog)
    composite = ResolveValuesComposite(
        tool_dispatcher=dispatcher, catalog=catalog, embedding_client=_embedder()
    )
    return composite, client


async def test_run_earnings_ranks_over_real_mcp_and_embedder(mint: Mint) -> None:
    # The FULL model-facing tool path: run() -> ToolResult over the real
    # MCP↔ClickHouse (the backing DISTINCT runQuery, scope-enforced end-to-end)
    # + the real embedder ranking the live domain.
    jwt = await mint(session_id="sess-rv-live")  # allow-all scope (D80b), session-bound
    creds = RuntimeCredentials(
        session_id="sess-rv-live", jwt=jwt, column_scope=frozenset()
    )
    composite, client = _composite()

    result: ToolResult = await composite.run(
        {"table": _P, "column": _COLUMN, "concept": _EARN_CONCEPT}, creds
    )

    # -- ToolResult contract --------------------------------------------------
    assert result.status == "ok", result
    assert result.tool_name == TOOL_NAME
    assert isinstance(result.result_full, dict)
    rf = result.result_full

    # Real embedder ⇒ semantic+freq ranking, NOT degraded (design §3.2).
    assert rf["degraded"] is False, rf
    assert rf["ranking"] == "semantic+freq", rf

    values = rf["values"]
    assert isinstance(values, list) and values, values
    # Every item is the fixed {value, description, score, freq} shape.
    for item in values:
        assert set(item.keys()) == {"value", "description", "score", "freq"}, item
        assert isinstance(item["value"], str)
        assert isinstance(item["score"], (int, float))
        assert isinstance(item["freq"], int)

    ranked = [item["value"] for item in values]
    assert "EARN" in ranked, f"EARN missing from ranked domain {ranked!r}"
    assert "DDUCT" in ranked, f"DDUCT missing from ranked domain {ranked!r}"
    assert ranked.index("EARN") < ranked.index("DDUCT"), (
        f"EARN must rank above DDUCT for concept {_EARN_CONCEPT!r}; got "
        f"{ranked!r} (scores={[(i['value'], i['score']) for i in values]})"
    )

    # -- provenance carried from the inner runQuery (design §8) ---------------
    assert result.provenance is not None
    assert (_P, _COLUMN) in result.provenance, result.provenance

    # -- D10: the concept NEVER touches SQL -----------------------------------
    # Exactly one backing runQuery was dispatched; scan its actual SQL.
    run_queries = [args for (name, args) in client.calls if name == "runQuery"]
    assert len(run_queries) == 1, client.calls
    backing_sql = run_queries[0]["sql"]
    assert _COLUMN in backing_sql, backing_sql  # DISTINCT-domain probe on the column
    assert "freq" in backing_sql, backing_sql
    assert _EARN_CONCEPT not in backing_sql, backing_sql
    # No non-runQuery tool was dispatched, and no concept leak anywhere in calls.
    assert all(name == "runQuery" for name, _ in client.calls), client.calls
    assert _EARN_CONCEPT not in json.dumps(client.calls), client.calls
    # The ToolResult surfaced to the model carries no SQL/concept leak either.
    assert _EARN_CONCEPT not in json.dumps(rf), rf


async def test_run_denied_when_backing_query_out_of_scope(mint: Mint) -> None:
    # D57 enforcement on the resolveValues path against real ClickHouse: a JWT
    # scoped to ONLY payroll.Amount (excludes RegisterType). resolve_target does
    # NOT pre-check the value column (design §2.1 — only the description sibling),
    # so the backing runQuery selects RegisterType and the MCP denies it. The
    # denial passes through to the ToolResult verbatim (design §7).
    scope = f"{_P}.Amount"
    # narrow scope: RegisterType is NOT permitted; session-bound to the creds' id
    jwt = await mint([scope], session_id="sess-rv-live-scope")
    creds = RuntimeCredentials(
        session_id="sess-rv-live-scope", jwt=jwt, column_scope=frozenset({scope})
    )
    composite, client = _composite()

    result: ToolResult = await composite.run(
        {"table": _P, "column": _COLUMN, "concept": _EARN_CONCEPT}, creds
    )

    assert result.status == "denied", result
    assert result.error_code == "COLUMN_SCOPE_VIOLATION", result
    # No ranked values / full result leak on a denial.
    assert result.result_full is None
    # The backing runQuery WAS dispatched (denial came from the MCP, not a local
    # pre-check) and still carried no concept leak.
    assert any(name == "runQuery" for name, _ in client.calls), client.calls
    assert _EARN_CONCEPT not in json.dumps(client.calls), client.calls
