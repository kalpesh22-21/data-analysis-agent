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
    assert "serves_intents" in get_schema["parameters"]["properties"]


def test_disabled_prompt_says_product_questions_have_no_source() -> None:
    """With Help Center off the base prompt still routed how-to questions to it; a live
    run then searched the knowledge base five times and closed the how-to deliverable
    with an unrelated schema read. Off, the one routing line says there is no source."""
    from data_agent.runtime.prompts import HELP_CENTER_ROUTE, HELP_CENTER_UNAVAILABLE_ROUTE

    disabled = RuntimeSettings(help_center_enabled=False)
    enabled = RuntimeSettings(help_center_enabled=True)

    assert HELP_CENTER_ROUTE in disabled.agent_system_prompt  # the line the swap replaces
    off = disabled.effective_agent_system_prompt()
    assert disabled.agent_system_prompt.replace(HELP_CENTER_ROUTE, HELP_CENTER_UNAVAILABLE_ROUTE) in off
    assert HELP_CENTER_ROUTE not in off and "searchHelpCenter" not in off
    on = enabled.effective_agent_system_prompt()
    assert HELP_CENTER_ROUTE in on and HELP_CENTER_UNAVAILABLE_ROUTE not in on
    assert "searchHelpCenter" in on


def test_product_guidance_navigation_instruction_requires_both_features() -> None:
    help_only = RuntimeSettings(help_center_enabled=True, capability_tools_enabled=False)
    capability_only = RuntimeSettings(help_center_enabled=False, capability_tools_enabled=True)
    both = RuntimeSettings(help_center_enabled=True, capability_tools_enabled=True)

    marker = "Product guidance with navigation"
    assert marker not in help_only.effective_agent_system_prompt()
    assert marker not in capability_only.effective_agent_system_prompt()
    combined = both.effective_agent_system_prompt()
    assert marker in combined
    assert "use the Help Center to answer the question in text" in combined
    assert "If no relevant navigation or action option is available" in combined


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


@pytest.mark.parametrize("reason,request_text", [
    ("product_documentation_unavailable", "explain expense submission"),
    ("action_not_supported", "open Expense Management"),
])
async def test_off_mixed_intents_finalize_through_normal_judge_without_salvage(reason, request_text):
    from tests.runtime.final_answer import final_answer
    from tests.runtime.test_harness_improvements import (
        CREDS,
        Judge,
        batch,
        build,
        call,
        discovery,
        query,
        run,
    )

    from data_agent.runtime.composite.scope_refusal import DeclineOutOfScopeTool
    from data_agent.runtime.session.models import live_analysis_state

    judge = Judge()
    steps = [
        batch(call("updateAnalysisState", "declare", intents=[
            {"description": "Count employees by department"}, {"description": request_text},
        ])),
        discovery(), query(),
        batch(call("declineOutOfScope", "disabled", reason=reason, request=request_text, serves_intents=["i2"])),
        batch(call("updateAnalysisState", "close", intents=[
            {"intent_id": "i1", "status": "completed", "result_id": "q"},
            {"intent_id": "i2", "status": "blocked", "result_id": "disabled"},
        ])),
        final_answer(assistant_text="Sales has 120 employees. The requested feature is not supported here.",
                     evidence=["q"], tables=[{"result_id": "q", "caption": "Employees"}]),
    ]
    loop, store, _, _, events = build(steps, judge, extra={
        "declineOutOfScope": DeclineOutOfScopeTool(actions_available=False, product_documentation_available=False),
    })
    out = await run(loop, "Count employees by department and " + request_text + ".")
    assert out.review["status"] == "approved" and len(judge.briefs) == 1
    assert not any("salvage" in name or name == "loop_finalization_refused" for name, _ in events)
    doc = await store.get_or_create_session(CREDS.session_id)
    state = live_analysis_state(doc, 0)
    assert [i.status for i in state.intents] == ["completed", "blocked"]
    assert state.intents[1].reason_code == "NOT_SUPPORTED_HERE"
    assert not any(e.error_code == "ANALYSIS_STATE_INVALID" for e in doc.tool_trail)
    assert "disabled" not in out.answer_document["sources"]


@pytest.mark.parametrize("actions", [False, True])
@pytest.mark.parametrize("documentation", [False, True])
async def test_decline_reasons_follow_both_settings(actions, documentation):
    schemas = await ToolSchemaCache(FakeMCP(), actions_available=actions,
                                   product_documentation_available=documentation).get_schemas(jwt="jwt", session_id="s")
    assert "checkAvailability" not in {s["name"] for s in schemas}
    schema = next(s for s in schemas if s["name"] == "declineOutOfScope")
    props = schema["parameters"]["properties"]
    assert "  " not in schema["description"]
    assert ("deployment limitation" in schema["description"]) == (not (actions and documentation))
    assert ("NOT_SUPPORTED_HERE" in schema["description"]) == (not (actions and documentation))
    if actions and documentation:
        assert "reason" not in props
    else:
        reasons = props["reason"]["enum"]
        assert ("action_not_supported" in reasons) == (not actions)
        assert ("product_documentation_unavailable" in reasons) == (not documentation)


@pytest.mark.parametrize("enabled,quote,expected", [
    (False, "explain expense submission", "NOT_SUPPORTED_HERE"),
    (True, "explain expense submission", "INVALID_TOOL_ARGUMENTS"),
    (False, "invented request", "INVALID_TOOL_ARGUMENTS"),
])
async def test_documentation_decline_requires_configuration_and_user_quote(enabled, quote, expected):
    from dataclasses import replace

    from tests.runtime.composite.test_previous_result_reuse import CREDS, original

    from data_agent.runtime.composite.analysis_state import classify_block_evidence
    from data_agent.runtime.composite.scope_refusal import DeclineOutOfScopeTool
    from data_agent.runtime.loop.agent_loop import TurnContext

    args = {"request": quote, "reason": "product_documentation_unavailable"}
    result = await DeclineOutOfScopeTool(product_documentation_available=enabled).run(
        args, CREDS, TurnContext(0, "Count employees and explain expense submission."))
    assert result.error_code == expected
    entry = replace(original(), tool_name="declineOutOfScope", args=args,
                    status=result.status, error_code=result.error_code, result_preview=None)
    assert classify_block_evidence(entry.tool_call_id, [entry], 0) == (
        "NOT_SUPPORTED_HERE" if expected == "NOT_SUPPORTED_HERE" else None)
    if expected == "NOT_SUPPORTED_HERE":
        assert result.retryable is False and result.result_preview is None
        assert "Product documentation is not available in this deployment" in result.user_message


async def test_default_decline_configuration_matches_runtime_settings():
    from tests.runtime.composite.test_previous_result_reuse import CREDS

    from data_agent.runtime.composite.scope_refusal import DeclineOutOfScopeTool
    from data_agent.runtime.loop.agent_loop import TurnContext
    from data_agent.runtime.mcp.tool_schema import (
        DECLINE_OUT_OF_SCOPE_SCHEMA,
        decline_out_of_scope_schema,
        fetch_function_schemas,
    )

    settings = RuntimeSettings(_env_file=None)
    assert settings.help_center_enabled is False and settings.capability_tools_enabled is False
    expected = decline_out_of_scope_schema(
        actions_available=settings.capability_tools_enabled,
        product_documentation_available=settings.help_center_enabled,
    )
    assert DECLINE_OUT_OF_SCOPE_SCHEMA == expected
    assert decline_out_of_scope_schema(actions_available=False) == expected
    for schemas in [await fetch_function_schemas(FakeMCP(), jwt="jwt", session_id="s"),
                    await ToolSchemaCache(FakeMCP()).get_schemas(jwt="jwt", session_id="s")]:
        assert next(s for s in schemas if s["name"] == "declineOutOfScope") == expected
    result = await DeclineOutOfScopeTool().run(
        {"request": "explain expense submission", "reason": "product_documentation_unavailable"},
        CREDS, TurnContext(0, "explain expense submission"),
    )
    assert result.error_code == "NOT_SUPPORTED_HERE"


class _HelpDown:
    async def run(self, *args, **kwargs):
        from data_agent.runtime.dispatch.tool_dispatcher import ToolResult

        return ToolResult("error", "searchHelpCenter", "HELP_UNAVAILABLE", True, "down", None, None, None)


async def test_a_rejected_help_decline_leaves_the_repair_its_recorded_assumptions():
    """The decline is bound to approval only once the judge approves it. Applied before
    review, it cleared the turn's assumptions, so a repaired data answer shipped without
    them while resume and history still had them."""
    from tests.runtime.test_answer_document import section
    from tests.runtime.test_harness_improvements import (
        CREDS,
        Judge,
        batch,
        build,
        call,
        discovery,
        query,
        run,
    )

    from data_agent.runtime.loop.answer_judge import JudgeVerdict
    from data_agent.runtime.session_history import project_history

    recorded = ["Department is the employee's home department."]
    judge = Judge([JudgeVerdict(False, "contradicts_result", "Answer the employee count with data.", True,
                                repair_type="analysis")])
    loop, store, *_ = build(
        [discovery(),
         batch(call("searchHelpCenter", "h", query="clock in")),
         batch(call("recordAssumptions", "a", assumptions=recorded)),
         batch(call("finalizeAnswer", "f1", takeaway="Open Time Clock and select Start.", sections=[], evidence=[])),
         query(),
         batch(call("finalizeAnswer", "f2", takeaway="Employee counts by department.",
                    sections=[section()], evidence=["q"]))],
        judge, extra={"searchHelpCenter": _HelpDown()},
    )
    out = await run(loop)
    assert out.review["status"] == "approved" and out.assumptions == recorded
    doc = await store.get_or_create_session(CREDS.session_id)
    assert project_history(doc.messages, doc.tool_trail, CREDS.column_scope, None)["turns"][0]["assumptions"] == recorded
    assert await loop._compute_turn_assumptions(CREDS.session_id, 0) == recorded


async def test_a_help_decline_ships_no_supporting_evidence():
    """The canned decline cites nothing, so no evidence-only receipt ships with it."""
    from tests.runtime.test_harness_improvements import CREDS, Judge, Tool, batch, build, call, run

    loop, store, *_ = build(
        [batch(call("searchHelpCenter", "h", query="clock in")),
         batch(call("listTables", "lt", database="hr")),
         batch(call("finalizeAnswer", "f1", takeaway="Open Time Clock and select Start.", sections=[],
                    evidence=["lt"]))],
        Judge(), extra={"searchHelpCenter": _HelpDown(), "listTables": Tool("listTables", {"tables": ["employee"]})},
    )
    out = await run(loop, "How do I clock in?")
    doc = await store.get_or_create_session(CREDS.session_id)
    assert out.review["status"] == "approved" and not out.extra_evidence
    assert not [m for m in doc.messages if m.role == "assistant"][-1].extra_evidence
