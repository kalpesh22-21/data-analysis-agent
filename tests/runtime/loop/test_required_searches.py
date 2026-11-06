from dataclasses import replace

import pytest

from data_agent.runtime.context.scope_filter import compute_scope_hash
from data_agent.runtime.dispatch.tool_dispatcher import ToolResult
from data_agent.runtime.loop.required_searches import LIMITATION, SEARCHES, RequiredSearchGate
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.session.models import ResultPreview, TrailEntry
from tests.runtime.loop.test_answer_judge_exits import (
    SESSION_ID,
    _answer_table,
    _build,
    _credentials,
    _query,
    _rows_mcp,
    _ScriptedJudge,
)

pytestmark = pytest.mark.usefixtures("answer_tools", "blueprint_consulted")


class Search:
    def __init__(self, name, fail_once=False):
        self.name, self.fail_once, self.calls = name, fail_once, 0

    async def run(self, args, credentials, **kwargs):
        self.calls += 1
        failed = self.fail_once and self.calls == 1
        return ToolResult(
            status="error" if failed else "ok",
            tool_name=self.name,
            error_code="RETRIEVAL_TOOL_UNAVAILABLE" if failed else None,
            retryable=False if failed else None,
            user_message=None,
            provenance=frozenset(),
            result_preview=None
            if failed
            else ResultPreview(
                columns=[], row_count=1, truncated=False, preview_rows=[[{"matches": []}]]
            ),
            result_full=None if failed else {"matches": []},
        )


def call(name, ident=None):
    return ModelTurnResult(
        tool_calls=[ToolCallRequest(id=ident or name, name=name, arguments={"query": "time off"})]
    )


def build(script, required=SEARCHES, fail_once=False):
    loop, store, events, model = _build(script, _ScriptedJudge(), mcp=_rows_mcp(2))
    loop._required_search_tools = required
    provider = loop._tools_provider

    async def schemas(credentials):
        return [
            *await provider(credentials),
            *[{"name": n, "type": "function", "parameters": {"type": "object"}} for n in required],
        ]

    loop._tools_provider = schemas
    loop._runtime_tools = {
        **loop._runtime_tools,
        **{n: Search(n, fail_once and n == "searchBlueprints") for n in required},
    }
    return loop, store, events, model


@pytest.mark.parametrize("capabilities", [False, True])
async def test_local_searches_unlock_tools_and_finalizer(capabilities):
    required = SEARCHES + (("searchCapabilityTools",) if capabilities else ())
    script = [
        *(call(n) for n in required),
        ModelTurnResult(tool_calls=[_query("q1")]),
        ModelTurnResult(tool_calls=[_answer_table("a", "Counts by department.")]),
    ]
    loop, store, events, model = build(script, required)
    result = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Counts by department"
    )
    assert result.status == "done"
    assert result.review["status"] == "approved"
    for i, _name in enumerate(required):
        assert {t["name"] for t in model.calls[i].tools} == set(required[i:])
    assert "runQuery" in {t["name"] for t in model.calls[len(required)].tools}
    assert sum(e == "loop_required_search_gate" and p["status"] == "ready" for e, p in events) == 1


async def test_failed_search_stays_pending_and_can_retry():
    loop, store, events, model = build(
        [
            call("searchBlueprints", "bad"),
            call("searchKnowledge"),
            call("searchBlueprints", "retry"),
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(tool_calls=[_answer_table("a", "Counts by department.")]),
        ],
        fail_once=True,
    )
    result = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Counts by department"
    )
    assert result.review["status"] == "approved"
    assert {t["name"] for t in model.calls[1].tools} == set(SEARCHES)
    assert {t["name"] for t in model.calls[2].tools} == {"searchBlueprints"}


async def test_mixed_batch_keeps_search_but_drops_early_finalizer():
    early = ModelTurnResult(
        tool_calls=[
            _answer_table("early", "Wrong"),
            ToolCallRequest(id="s", name="searchBlueprints", arguments={"query": "time off"}),
        ]
    )
    loop, store, events, model = build(
        [
            early,
            call("searchKnowledge"),
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(tool_calls=[_answer_table("a", "Counts by department.")]),
        ]
    )
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Counts by department"
    )
    assert outcome.review["status"] == "approved"
    doc = await store.get_or_create_session(SESSION_ID)
    assert not any(e.tool_call_id == "early" for e in doc.tool_trail)
    assert any(e.tool_call_id == "s" for e in doc.tool_trail)
    assert any(p["status"] == "blocked" for e, p in events if e == "loop_required_search_gate")


async def test_unfinished_gate_cannot_use_judge_or_salvage():
    loop, store, events, model = build([ModelTurnResult(), ModelTurnResult()])
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Counts by department"
    )
    assert outcome.assistant_text == LIMITATION
    assert not loop._answer_judge.briefs
    assert not any(e == "loop_partial_answer_recovered" for e, p in events)


def test_resume_receipts_are_turn_and_scope_bound():
    scope = compute_scope_hash(frozenset())
    entry = TrailEntry(
        turn_index=2,
        tool_call_id="s",
        tool_name="searchBlueprints",
        args={},
        status="ok",
        error_code=None,
        provenance=frozenset(),
        result_full_ref=None,
        result_preview=ResultPreview(columns=[], row_count=1, truncated=False, preview_rows=[[{}]]),
        model_response={"scope_hash": scope},
        ts="2026-11-05T00:00:00Z",
    )
    assert RequiredSearchGate.restore(SEARCHES, [entry], 2, scope).pending == ("searchKnowledge",)
    assert RequiredSearchGate.restore(SEARCHES, [entry], 3, scope).pending == SEARCHES
    assert RequiredSearchGate.restore(SEARCHES, [entry], 2, "new").pending == SEARCHES
    assert (
        RequiredSearchGate.restore(
            SEARCHES, [replace(entry, error_code="ALREADY_SERVED")], 2, scope
        ).pending
        == SEARCHES
    )


@pytest.mark.parametrize("hints", [False, True])
@pytest.mark.parametrize("capabilities", [False, True])
def test_app_wires_flag_and_capability_condition(monkeypatch, hints, capabilities):
    from fastapi.testclient import TestClient

    from data_agent.runtime import app as app_module
    from data_agent.runtime.app import create_app
    from data_agent.runtime.config import RuntimeSettings
    from data_agent.runtime.mcp.fake_client import FakeMCPClient
    from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
    from data_agent.runtime.model.scripted_client import ScriptedModelClient
    from data_agent.runtime.provenance.catalog_handle import CatalogHandle
    from data_agent.runtime.session.memory_store import InMemorySessionStore
    from tests.runtime.final_answer import final_answer
    from tests.runtime.retrieval.test_config_qa import _recording_pipeline

    class Capabilities:
        async def search(self, *args, **kwargs):
            return []

    monkeypatch.setattr(app_module, "verify_jwt", lambda *a, **kw: frozenset())
    required = SEARCHES + (("searchCapabilityTools",) if capabilities else ())
    script = ([] if hints else [call(n) for n in required]) + [
        final_answer(assistant_text="I can help analyze HR data.")
    ]
    model = ScriptedModelClient(script)
    embedder = FakeEmbeddingClient()
    app = create_app(
        settings=RuntimeSettings(
            _env_file=None,
            retrieval_mode="hints" if hints else "required_search",
            capability_tools_enabled=capabilities,
            answer_judge_enabled=False,
            progress_summary_enabled=False,
        ),
        model_client=model,
        session_store=InMemorySessionStore(),
        mcp_client=FakeMCPClient(),
        catalog=CatalogHandle({}),
        retrieval=_recording_pipeline(embedder),
        capability_client=Capabilities(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/turn",
            json={"message": "What can you help with?"},
            headers={"Authorization": "Bearer test", "X-Session-Id": "gate-settings"},
        )
    assert response.status_code == 200
    assert model.calls_made == len(script)
    assert not any("prefetchContext" in str(m) for c in model.calls for m in c.messages)
    has_hint = any(
        "candidate blueprints/knowledge" in str(m.get("content", ""))
        for m in model.calls[0].messages
    )
    assert has_hint == hints
    if not hints:
        assert {t["name"] for t in model.calls[0].tools} == set(required)
        assert "finalizeAnswer" in {t["name"] for t in model.calls[-1].tools}


@pytest.mark.parametrize("hints", [False, True])
@pytest.mark.parametrize("capabilities", [False, True])
def test_prompt_explains_selected_discovery_mode(hints, capabilities):
    from data_agent.runtime.config import RuntimeSettings
    from data_agent.runtime.prompts import RETRIEVAL_HINTS_GUIDANCE, required_search_guidance

    settings = RuntimeSettings(
        _env_file=None, retrieval_mode="hints" if hints else "required_search", capability_tools_enabled=capabilities
    )
    prompt = settings.effective_agent_system_prompt()
    guidance = RETRIEVAL_HINTS_GUIDANCE if hints else required_search_guidance(capabilities)
    assert prompt.startswith(guidance + "\n\n")
    assert ("searchCapabilityTools" in guidance) == (capabilities and not hints)
    opposite = "Required-search mode:" if hints else "Retrieval hints mode:"
    assert opposite not in prompt
    assert (
        settings.model_copy(
            update={"agent_system_prompt_enabled": False}
        ).effective_agent_system_prompt()
        is None
    )


async def test_missing_search_schema_fails_closed_without_model_or_judge():
    loop, store, events, model = build([])

    async def no_searches(credentials):
        return [{"name": "runQuery", "parameters": {}}]

    loop._tools_provider = no_searches
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Counts by department"
    )
    assert outcome.assistant_text == LIMITATION
    assert model.calls_made == 0
    assert not loop._answer_judge.briefs


async def test_gate_violation_uses_search_guidance_not_finalization_repair():
    loop, store, events, model = build([
        ModelTurnResult(tool_calls=[_query("premature")]),
        call("searchBlueprints"), call("searchKnowledge"),
        ModelTurnResult(tool_calls=[_query("q1")]),
        ModelTurnResult(tool_calls=[_answer_table("a", "Counts by department.")]),
    ])
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Counts by department"
    )
    assert outcome.review["status"] == "approved"
    assert any("Only pending searches" in str(m) for m in model.calls[1].messages)
    # The request builder adds the instruction; the violation path must not add it again.
    assert str(model.calls[1].messages).count("Before other work on this turn") == 1
    assert not any("empty_answer" in e for e, _ in events)
    assert not any(e.tool_call_id == "premature" for e in (await store.get_or_create_session(SESSION_ID)).tool_trail)


def test_default_and_legacy_retrieval_modes():
    from data_agent.runtime.config import RuntimeSettings

    assert RuntimeSettings(_env_file=None).retrieval_mode == "required_search"
    # Neither deprecated flag value overrides the required-search default.
    for flag in (True, False):
        assert RuntimeSettings(_env_file=None, retrieval_prefetch_tool_enabled=flag).retrieval_mode == "required_search"
    assert RuntimeSettings(_env_file=None, retrieval_mode="required_search",
                           retrieval_prefetch_tool_enabled=True).retrieval_mode == "required_search"


@pytest.mark.parametrize("enabled", [False, True])
def test_required_search_rejects_missing_retrieval_at_startup(enabled):
    from data_agent.runtime.app import create_app
    from data_agent.runtime.config import RuntimeSettings
    from data_agent.runtime.mcp.fake_client import FakeMCPClient
    from data_agent.runtime.model.scripted_client import ScriptedModelClient
    from data_agent.runtime.provenance.catalog_handle import CatalogHandle
    from data_agent.runtime.session.memory_store import InMemorySessionStore

    with pytest.raises(ValueError, match="RETRIEVAL_MODE=required_search requires"):
        create_app(
            settings=RuntimeSettings(_env_file=None,
                                     retrieval_enabled=enabled, neo4j_url=""),
            model_client=ScriptedModelClient([]), mcp_client=FakeMCPClient(),
            catalog=CatalogHandle({}), session_store=InMemorySessionStore(),
        )


def test_explicit_hints_with_deprecated_flag_warns(caplog):
    from data_agent.runtime.app import create_app
    from data_agent.runtime.config import RuntimeSettings
    from data_agent.runtime.mcp.fake_client import FakeMCPClient
    from data_agent.runtime.model.scripted_client import ScriptedModelClient
    from data_agent.runtime.provenance.catalog_handle import CatalogHandle
    from data_agent.runtime.session.memory_store import InMemorySessionStore

    # Explicit hints mode remains available without retrieval.
    with caplog.at_level("WARNING", logger="data_agent.runtime.app"):
        create_app(
            settings=RuntimeSettings(_env_file=None, retrieval_mode="hints", retrieval_prefetch_tool_enabled=False,
                                     retrieval_enabled=False, neo4j_url=""),
            model_client=ScriptedModelClient([]), mcp_client=FakeMCPClient(),
            catalog=CatalogHandle({}), session_store=InMemorySessionStore(),
        )
    assert "RETRIEVAL_PREFETCH_TOOL_ENABLED is deprecated and ignored" in caplog.text


def test_helm_values_select_retrieval_mode_not_the_deprecated_flag():
    from pathlib import Path

    import yaml

    for name in ("values.yaml", "values-example.yaml"):
        text = (Path(__file__).parents[3] / "deploy/helm/data-agent" / name).read_text()
        assert "RETRIEVAL_PREFETCH_TOOL_ENABLED" not in text
        values = yaml.safe_load(text)
        modes = [v["RETRIEVAL_MODE"] for v in values.values() if isinstance(v, dict) and "RETRIEVAL_MODE" in v]
        assert modes == ["required_search"], name


async def test_finish_uses_window_credentials_without_delivery_context():
    from data_agent.runtime.loop.delivery import CURRENT_DELIVERY

    loop, store, events, model = build([
        call("searchBlueprints"), call("searchKnowledge"),
        ModelTurnResult(tool_calls=[_query("q1")]),
        ModelTurnResult(tool_calls=[_answer_table("a", "Counts by department.")]),
    ])
    original_finish = loop._finish

    async def finish_without_delivery(**kwargs):
        token = CURRENT_DELIVERY.set(None)
        try:
            return await original_finish(**kwargs)
        finally:
            CURRENT_DELIVERY.reset(token)

    loop._finish = finish_without_delivery
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Counts by department"
    )
    assert outcome.assistant_text != LIMITATION
    assert not any(e == "loop_required_search_gate" and p["status"] == "incomplete" for e, p in events)


def test_explicit_hints_without_retrieval_answers_chat(monkeypatch):
    from fastapi.testclient import TestClient

    from data_agent.runtime import app as app_module
    from data_agent.runtime.config import RuntimeSettings
    from data_agent.runtime.mcp.fake_client import FakeMCPClient
    from data_agent.runtime.model.scripted_client import ScriptedModelClient
    from data_agent.runtime.provenance.catalog_handle import CatalogHandle
    from data_agent.runtime.session.memory_store import InMemorySessionStore
    from tests.runtime.final_answer import final_answer

    monkeypatch.setattr(app_module, "verify_jwt", lambda *a, **kw: frozenset())
    judge = _ScriptedJudge()
    monkeypatch.setattr(app_module, "AnswerJudge", lambda *a, **kw: judge)
    model = ScriptedModelClient([final_answer(assistant_text="Hello! How can I help?")])
    monkeypatch.setattr(app_module, "build_openai_model_client", lambda **kw: model)
    app = app_module.create_app(
        settings=RuntimeSettings(_env_file=None, retrieval_mode="hints", neo4j_url="", answer_judge_enabled=True, openai_api_key="test-only",
                                 progress_summary_enabled=False),
        model_client=model, mcp_client=FakeMCPClient(), catalog=CatalogHandle({}),
        session_store=InMemorySessionStore(),
    )
    with TestClient(app) as client:
        response = client.post('/turn', json={"message": "Hi"}, headers={
            "Authorization": "Bearer test", "X-Session-Id": "no-retrieval-default",
        })
    assert response.status_code == 200
    assert "Hello! How can I help?" in response.text, model.calls[-1].messages[-3:]
    assert LIMITATION not in response.text
    assert model.calls_made == 1
    assert judge.calls_made == 1


@pytest.mark.parametrize("capabilities", [False, True])
async def test_search_batch_unlocks_next_response(capabilities):
    required = SEARCHES + (("searchCapabilityTools",) if capabilities else ())
    loop, store, events, model = build([
        ModelTurnResult(tool_calls=[call(name).tool_calls[0] for name in required]),
        ModelTurnResult(tool_calls=[_query("q1")]),
        ModelTurnResult(tool_calls=[_answer_table("a", "Counts by department.")]),
    ], required)
    result = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Counts by department"
    )
    assert result.status == "done"
    assert result.review["status"] == "approved"
    assert {t["name"] for t in model.calls[0].tools} == set(required)
    assert "runQuery" in {t["name"] for t in model.calls[1].tools}
    assert all(loop._runtime_tools[name].calls == 1 for name in required)


def test_search_batch_guidance_in_both_modes_and_gate():
    from data_agent.runtime.prompts import (
        RETRIEVAL_HINTS_GUIDANCE,
        SEARCH_BATCH_GUIDANCE,
        required_search_guidance,
    )
    for guidance in (
        RETRIEVAL_HINTS_GUIDANCE, required_search_guidance(False),
        required_search_guidance(True),
    ):
        assert guidance.count(SEARCH_BATCH_GUIDANCE) == 1
    assert SEARCH_BATCH_GUIDANCE not in RequiredSearchGate(SEARCHES).instruction()
