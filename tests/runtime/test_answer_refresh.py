from copy import deepcopy
from dataclasses import replace

import pytest

from data_agent.runtime.answer_access import resolve_table
from data_agent.runtime.session.answer_refresh import (
    RefreshRejectedError,
    RefreshStore,
    find_answer,
)
from data_agent.runtime.session.store import CASMismatchError
from data_agent.runtime.session_history import project_history
from tests.runtime.test_answer_document import final, section
from tests.runtime.test_harness_improvements import CREDS, Judge, build, discovery, query, run


async def saved():
    loop, store, *_ = build([discovery(), query(), final(section())], Judge())
    await run(loop)
    doc = await store.get_or_create_session(CREDS.session_id)
    return store, doc, doc.messages[-1]


async def test_history_exact_document_scope_filter_and_no_mutation():
    store, doc, target = await saved()
    target.answer_document["sources"]["q"]["data_as_of"] = "2026-01-01T00:00:00Z"
    history = project_history(doc.messages, doc.tool_trail, CREDS.column_scope, None)
    assert history["turns"][0]["answer_document"] == target.answer_document
    hidden = project_history(doc.messages, doc.tool_trail, frozenset({"other.table.column"}), None)
    assert "answer_document" not in hidden["turns"][0]
    no_freshness = project_history(
        doc.messages, doc.tool_trail, CREDS.column_scope, None, data_as_of_enabled=False
    )
    assert "data_as_of" not in no_freshness["turns"][0]["answer_document"]["sources"]["q"]
    assert target.answer_document["sources"]["q"]["data_as_of"]


async def test_refresh_stages_then_atomically_replaces_document_and_resolves_new_sources():
    store, doc, target = await saved()
    old = deepcopy(doc.to_doc())
    staging = RefreshStore(store, doc, target)
    loop, *_ = build(
        [discovery(), query(), final(section(), takeaway="Fresh counts.")], Judge(), store=staging
    )
    await run(loop)
    assert (await store.get_or_create_session(CREDS.session_id)).to_doc() == old
    message, trail = await staging.publication(
        CREDS.session_id, target.answer_document["answer_id"], 1
    )
    await store.publish_answer_refresh(
        CREDS.session_id, answer_id="answer-0", expected_revision=1, message=message, trail=trail
    )
    current = await store.get_or_create_session(CREDS.session_id)
    approved = find_answer(current, "answer-0", CREDS.column_scope)
    assert approved.answer_document["revision"] == 2
    assert approved.answer_document["takeaway"] == "Fresh counts."
    source = next(iter(approved.answer_document["sources"].values()))
    assert source["table_ref"] != target.answer_document["sources"]["q"]["table_ref"]
    assert await resolve_table(current, source["table_ref"], CREDS, store)
    with pytest.raises(PermissionError):
        await resolve_table(
            current, target.answer_document["sources"]["q"]["table_ref"], CREDS, store
        )
    winner = deepcopy(current.to_doc())
    with pytest.raises(CASMismatchError):
        await store.publish_answer_refresh(
            CREDS.session_id,
            answer_id="answer-0",
            expected_revision=1,
            message=message,
            trail=trail,
        )
    assert current.to_doc() == winner


async def test_unreviewed_refresh_cannot_publish_and_results_do_not_overwrite():
    store, doc, target = await saved()
    staging = RefreshStore(store, doc, target)
    ref = await store.write_full_result(CREDS.session_id, "q", {"value": "old"})
    fresh_ref = await staging.write_full_result(CREDS.session_id, "q", {"value": "new"})
    assert ref != fresh_ref
    assert await store.read_full_result(CREDS.session_id, ref) == {"value": "old"}
    await staging.append_message(CREDS.session_id, replace(target, review={"status": "disabled"}))
    with pytest.raises(RefreshRejectedError):
        await staging.publication(CREDS.session_id, "answer-0", 1)
    assert doc.messages[-1].answer_document["revision"] == 1


async def test_refresh_endpoint_stream_history_conflict_and_trace(monkeypatch):
    import json

    import httpx
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from data_agent.runtime import app as app_module
    from data_agent.runtime.config import RuntimeSettings
    from data_agent.runtime.loop.agent_loop import AgentLoop
    from data_agent.runtime.mcp.fake_client import FakeMCPClient
    from data_agent.runtime.provenance.catalog_handle import CatalogHandle

    def events(body):
        return [
            {
                "event": frame.splitlines()[0].removeprefix("event: "),
                "data": json.loads(
                    next(line[6:] for line in frame.splitlines() if line.startswith("data: "))
                ),
            }
            for frame in body.split("\n\n")
            if frame.startswith("event: ")
        ]

    store, original, target = await saved()
    sink = InMemorySpanExporter()
    monkeypatch.setattr(app_module, "verify_jwt", lambda *a, **k: CREDS.column_scope)
    normal_run = AgentLoop.run

    async def refreshed_run(self, **kwargs):
        # Exercise the production route's isolated store with the real loop and judge.
        loop, *_ = build(
            [discovery(), query(), final(section(), takeaway="q")],
            Judge(),
            store=self._session_store,
        )
        return await normal_run(loop, **kwargs)

    monkeypatch.setattr(AgentLoop, "run", refreshed_run)
    app = app_module.create_app(
        settings=RuntimeSettings(_env_file=None, otlp_endpoint="", openai_api_key="test-only"),
        session_store=store,
        mcp_client=FakeMCPClient(),
        catalog=CatalogHandle({}),
        span_exporter=sink,
    )
    headers = {"Authorization": "Bearer fake", "X-Session-Id": CREDS.session_id}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/answer/refresh",
            headers=headers,
            json={"answer_id": "answer-0", "expected_revision": 1},
        )
        frames = events(response.text)
        result = next(f["data"] for f in frames if f["event"] == "result")
        assert result["answer_document"]["revision"] == 2
        assert result["answer_document"]["takeaway"] == "q"  # not rewritten as a result ID
        history = (await client.get("/session/history", headers=headers)).json()
        assert history["turns"][0]["answer_document"] == result["answer_document"]
        conflict = await client.post(
            "/answer/refresh",
            headers=headers,
            json={"answer_id": "answer-0", "expected_revision": 1},
        )
        assert conflict.status_code == 409
        unknown = await client.post(
            "/answer/refresh", headers=headers, json={"answer_id": "other", "expected_revision": 1}
        )
        assert unknown.status_code == 404
    spans = sink.get_finished_spans()
    root = next(s for s in spans if s.name == "agent.turn")
    emitted = next(s for s in spans if s.name == "loop_response_emitted")
    assert emitted.context.trace_id == root.context.trace_id
    assert emitted.parent.span_id == root.context.span_id


async def test_couchbase_refresh_cas_retry_rechecks_revision():
    from unittest.mock import AsyncMock

    from couchbase.exceptions import CasMismatchException

    from tests.runtime.session.test_couchbase_store_cas_retry import _build_store, _FakeGetResult

    _, original, target = await saved()
    winner = deepcopy(original)
    winner.messages[-1].answer_document["revision"] = 2
    replacement = deepcopy(winner.messages[-1])
    store, collection = _build_store(sleep=AsyncMock())
    collection.get = AsyncMock(
        side_effect=[_FakeGetResult(original.to_doc(), 1), _FakeGetResult(winner.to_doc(), 2)]
    )
    collection.replace = AsyncMock(side_effect=CasMismatchException("concurrent refresh won"))
    with pytest.raises(CASMismatchError):
        await store.publish_answer_refresh(
            CREDS.session_id,
            answer_id="answer-0",
            expected_revision=1,
            message=replacement,
            trail=[],
        )
    assert collection.replace.await_count == 1
