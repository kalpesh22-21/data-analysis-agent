from __future__ import annotations

import httpx
import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.help_center.client import (
    HelpCenterDocument,
    HelpCenterSearchHit,
    HttpHelpCenterClient,
)
from data_agent.runtime.help_center.tools import GetHelpCenterDocumentTool, SearchHelpCenterTool
from data_agent.runtime.mcp.client import MCPToolSpec
from data_agent.runtime.mcp.tool_schema import (
    GET_HELP_CENTER_DOCUMENT_TOOL_SCHEMA,
    SEARCH_HELP_CENTER_TOOL_SCHEMA,
    ToolSchemaCache,
)
from data_agent.runtime.model.reranker_client import FakeRerankerClient


class FakeHelpCenter:
    def __init__(self) -> None:
        self.jwts: list[str] = []

    async def search(self, query: str, limit: int, *, jwt: str) -> list[HelpCenterSearchHit]:
        self.jwts.append(jwt)
        return [HelpCenterSearchHit(str(i), float(10 - i), f"snippet {i}") for i in range(7)]

    async def get_document(self, article_id: str, *, jwt: str) -> HelpCenterDocument | None:
        self.jwts.append(jwt)
        return HelpCenterDocument(article_id, "complete")


class FakeMCP:
    async def list_tools(self, *, jwt: str, session_id: str) -> list[MCPToolSpec]:
        return []


@pytest.mark.asyncio
async def test_search_reranks_candidates_and_returns_five() -> None:
    reranker = FakeRerankerClient({f"snippet {i}": float(i) for i in range(7)})
    client = FakeHelpCenter()
    tool = SearchHelpCenterTool(client=client, reranker=reranker)
    credentials = RuntimeCredentials(jwt="jwt", session_id="session", column_scope=frozenset())

    result = await tool.run({"query": "positions"}, credentials)

    assert result.status == "ok"
    assert [item["id"] for item in result.result_full["documents"]] == ["6", "5", "4", "3", "2"]
    assert reranker.calls == [("positions", [f"snippet {i}" for i in range(7)])]
    assert client.jwts == ["jwt"]


@pytest.mark.asyncio
async def test_get_document_forwards_request_jwt() -> None:
    client = FakeHelpCenter()
    tool = GetHelpCenterDocumentTool(client=client)
    credentials = RuntimeCredentials(
        jwt="ui.jwt.token", session_id="session", column_scope=frozenset()
    )

    result = await tool.run({"id": "article-1"}, credentials)

    assert result.status == "ok"
    assert client.jwts == ["ui.jwt.token"]


@pytest.mark.asyncio
async def test_schema_is_absent_unless_explicitly_enabled() -> None:
    disabled = await ToolSchemaCache(FakeMCP()).get_schemas(jwt="jwt", session_id="session")
    enabled = await ToolSchemaCache(
        FakeMCP(),
        additional_local_schemas=(
            SEARCH_HELP_CENTER_TOOL_SCHEMA,
            GET_HELP_CENTER_DOCUMENT_TOOL_SCHEMA,
        ),
    ).get_schemas(jwt="jwt", session_id="session")

    assert "searchHelpCenter" not in {schema["name"] for schema in disabled}
    assert "searchHelpCenter" in {schema["name"] for schema in enabled}
    get_schema = next(schema for schema in enabled if schema["name"] == "getHelpCenterDocument")
    assert "serves_intent" in get_schema["parameters"]["properties"]


def test_prompt_is_byte_unchanged_when_disabled() -> None:
    disabled = RuntimeSettings(help_center_enabled=False)
    enabled = RuntimeSettings(help_center_enabled=True)

    assert disabled.effective_agent_system_prompt() == disabled.agent_system_prompt
    assert "searchHelpCenter" not in disabled.effective_agent_system_prompt()
    assert "searchHelpCenter" in enabled.effective_agent_system_prompt()


@pytest.mark.asyncio
async def test_http_contract() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer ui.jwt.token"
        if request.method == "POST":
            return httpx.Response(
                200, json={"documents": [{"id": "a/1", "score": 0.9, "snippet": "text"}]}
            )
        assert request.url.raw_path.endswith(b"/a%2F1")
        return httpx.Response(200, json={"id": "a/1", "content": "complete article"})

    client = HttpHelpCenterClient(
        search_url="https://help.test/v1/search",
        documents_url="https://help.test/v1/documents",
        transport=httpx.MockTransport(handler),
    )
    hits = await client.search("question", 25, jwt="ui.jwt.token")
    document = await client.get_document(hits[0].id, jwt="ui.jwt.token")

    assert hits[0].snippet == "text"
    assert document == HelpCenterDocument("a/1", "complete article")
