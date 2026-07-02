"""QA-3 adversarial Layer-1 tests for the read tools (retrieval/tools.py).

ADDITIVE to test_read_tools.py — never modifies it. Attacks the arg matrix,
the B4 crash guard, output non-reflection, the getBlueprint non-oracle
(byte-identical fields), degrade observability, config-driven k clamps through
the tool layer, and JSON-schema/credential validity of the three schemas.

All fakes, no infra. Every case must resolve to a clean `ToolResult` (ok/clamp
or a canned INVALID_ARGS/INTERNAL_ERROR) — never a raised exception, and never
the raw model-supplied value echoed into `user_message`/`result_full`/preview.
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
from data_agent.runtime.model.reranker_client import FakeRerankerClient
from data_agent.runtime.retrieval.models import BlueprintDetail, Candidate
from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
from data_agent.runtime.retrieval.tools import (
    GetBlueprintTool,
    SearchBlueprintsTool,
    SearchKnowledgeTool,
)
from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

_Q = "how much overtime did the sales team work"
_QVEC = [1.0, 0.0]
_A = "dbpcm_warehouse.payroll.Amount"
_B = "dbpcm_warehouse.employee.Department"

# A right-to-left-override + a plausible PII fragment: the exact class of free
# text a redaction/reflection bug would leak. Must never surface in output.
_BIDI = "‮abc‬ salary of Jane Doe SSN 123-45-6789"
_HUGE = "A" * 100_000


def _creds(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s1", jwt="jwt-secret", column_scope=scope)


def _bp(id: str, intent: str, uses: set[str], vec: list[float]) -> tuple[Candidate, list[float]]:
    return (
        Candidate(
            id=id,
            kind="blueprint",
            text=intent,
            uses=frozenset(uses),
            payload={"intent": intent, "slots_summary": f"slots-of-{id}"},
        ),
        vec,
    )


def _kn(id: str, chunk: str, vec: list[float]) -> tuple[Candidate, list[float]]:
    return (Candidate(id=id, kind="knowledge", text=chunk, uses=None, payload={"title": None}), vec)


def _detail(id: str = "bp-x", uses: frozenset[str] | None = frozenset({_A, _B})) -> BlueprintDetail:
    return BlueprintDetail(
        id=id,
        intent="Total overtime pay by department",
        slots_summary="department, pay_period",
        uses=uses,
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="sha123",
    )


_DEFAULT_EMBEDDER = object()


def _pipeline(
    *,
    index: FakeVectorIndex,
    embedder: Any = _DEFAULT_EMBEDDER,
    reranker: FakeRerankerClient | None = None,
    recall_k: int = 30,
    top_k_blueprints: int = 3,
    top_k_knowledge: int = 3,
) -> RetrievalPipeline:
    return RetrievalPipeline(
        embedding_client=(
            FakeEmbeddingClient({_Q: _QVEC}) if embedder is _DEFAULT_EMBEDDER else embedder
        ),
        reranker=reranker,
        vector_index=index,
        user_memory=NullUserMemoryProvider(),
        recall_k=recall_k,
        top_k_blueprints=top_k_blueprints,
        top_k_knowledge=top_k_knowledge,
    )


def _search_tool() -> SearchBlueprintsTool:
    idx = FakeVectorIndex([_bp("in", "overtime rollup", {_A}, [1.0, 0.0])])
    return SearchBlueprintsTool(
        pipeline=_pipeline(index=idx, reranker=FakeRerankerClient({"overtime rollup": 0.9})),
        default_k=5,
        max_k=20,
    )


def _knowledge_tool() -> SearchKnowledgeTool:
    idx = FakeVectorIndex([_kn("kn-1", "overtime is 1.5x", [1.0, 0.0])])
    return SearchKnowledgeTool(pipeline=_pipeline(index=idx), knowledge_k=5)


def _get_tool() -> GetBlueprintTool:
    idx = FakeVectorIndex(details={"bp-x": _detail("bp-x")})
    return GetBlueprintTool(vector_index=idx)


def _output_blob(result: Any) -> str:
    """Every model/user-facing surface of a ToolResult, concatenated — the
    reflection search space (user_message + result_full + result_preview)."""
    preview = result.result_preview
    return "".join(
        [
            repr(result.user_message),
            repr(result.result_full),
            repr(preview.to_doc() if preview is not None else None),
        ]
    )


# ---------------------------------------------------------------------------
# query arg matrix — every value → clean error or ok, never a crash/reflection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_query",
    [None, 123, 1.5, True, {"a": 1}, ["x"], "", "   ", "\t\n"],
)
async def test_query_arg_matrix_malformed_is_invalid_args(bad_query: Any) -> None:
    for tool in (_search_tool(), _knowledge_tool()):
        r = await tool.run({"query": bad_query}, _creds())
        assert r.status == "error"
        assert r.error_code == "RETRIEVAL_TOOL_INVALID_ARGS"
        # The bad VALUE is never echoed into the canned message.
        assert str(bad_query) not in (r.user_message or "") or bad_query in ("", "   ", "\t\n")
        assert r.provenance == frozenset()


@pytest.mark.parametrize("query", [_HUGE, _BIDI, "unicode ☃ é ü", "SELECT * FROM x"])
async def test_query_valid_free_text_never_reflected_into_output(query: str) -> None:
    # A 100KB / bidi / unicode / SQL-shaped query is accepted (free text) and
    # must NEVER appear in any model/user-facing output field (the pipeline
    # returns cards/knowledge only, never the query).
    for tool in (_search_tool(), _knowledge_tool()):
        r = await tool.run({"query": query}, _creds(frozenset({_A})))
        assert r.status == "ok"
        assert query not in _output_blob(r)


# ---------------------------------------------------------------------------
# k arg matrix — string/float/bool/<1 → INVALID_ARGS; huge → clamp; None → default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_k", ["5", 1.5, True, -1, 0, -(10**9)])
async def test_k_arg_matrix_malformed_is_invalid_args(bad_k: Any) -> None:
    r = await _search_tool().run({"query": _Q, "k": bad_k}, _creds())
    assert r.status == "error"
    assert r.error_code == "RETRIEVAL_TOOL_INVALID_ARGS"


async def test_k_billion_is_clamped_not_rejected() -> None:
    idx = FakeVectorIndex([_bp(f"b{i}", f"i{i}", {_A}, [1.0, i / 100]) for i in range(25)])
    tool = SearchBlueprintsTool(
        pipeline=_pipeline(index=idx, reranker=None, recall_k=30), default_k=5, max_k=20
    )
    r = await tool.run({"query": _Q, "k": 10**9}, _creds())
    assert r.status == "ok"
    assert r.result_full["count"] == 20  # clamped to max_k


async def test_k_absent_uses_default() -> None:
    idx = FakeVectorIndex([_bp(f"b{i}", f"i{i}", {_A}, [1.0, i / 100]) for i in range(25)])
    tool = SearchBlueprintsTool(
        pipeline=_pipeline(index=idx, reranker=None, recall_k=30), default_k=5, max_k=20
    )
    r = await tool.run({"query": _Q}, _creds())  # no k
    assert r.result_full["count"] == 5  # default_k


# ---------------------------------------------------------------------------
# id arg matrix — metachars/newlines/null-bytes/non-str → found:false | INVALID_ARGS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "malicious_id",
    [
        "' OR 1=1 //",
        "bp-x') DETACH DELETE (n) //",
        "a\nb\r",
        "x\x00y",
        "‮evil",
        "A" * 100_000,
    ],
)
async def test_id_metachars_never_crash_and_never_reflected(malicious_id: str) -> None:
    # Fake store does a plain dict lookup; a real store parameterizes. Either
    # way a hostile id is a clean miss, never a crash, never reflected.
    r = await _get_tool().run({"id": malicious_id}, _creds(frozenset({_A, _B})))
    assert r.status == "ok"
    assert r.result_full == {"found": False}
    assert malicious_id not in _output_blob(r)


@pytest.mark.parametrize("bad_id", [None, 123, 1.5, True, {"a": 1}, ["x"], "", "  "])
async def test_id_malformed_is_invalid_args(bad_id: Any) -> None:
    r = await _get_tool().run({"id": bad_id}, _creds())
    assert r.status == "error"
    assert r.error_code == "RETRIEVAL_TOOL_INVALID_ARGS"


# ---------------------------------------------------------------------------
# extra / missing args
# ---------------------------------------------------------------------------


async def test_unexpected_extra_args_are_ignored() -> None:
    # A model over-specifying (session_id, jwt, scope, k on a tool with no k)
    # must not crash and must not smuggle a credential into behaviour.
    r = await _knowledge_tool().run(
        {"query": _Q, "k": 99, "session_id": "s", "jwt": "leak", "scope": ["x"]}, _creds()
    )
    assert r.status == "ok"
    r2 = await _get_tool().run({"id": "bp-x", "unexpected": object()}, _creds())
    assert r2.status == "ok"


async def test_missing_required_arg_is_invalid_args() -> None:
    assert (await _search_tool().run({}, _creds())).error_code == "RETRIEVAL_TOOL_INVALID_ARGS"
    assert (await _knowledge_tool().run({}, _creds())).error_code == "RETRIEVAL_TOOL_INVALID_ARGS"
    assert (await _get_tool().run({}, _creds())).error_code == "RETRIEVAL_TOOL_INVALID_ARGS"


# ---------------------------------------------------------------------------
# B4 crash guard — a backing dependency that RAISES → clean INTERNAL_ERROR
# ---------------------------------------------------------------------------


class _RaisingPipeline:
    async def search_blueprints(self, *, question: str, column_scope: frozenset[str], k: int) -> Any:
        raise RuntimeError("pipeline boom str(exc) MUST NOT LEAK")

    async def search_knowledge(self, *, question: str, k: int) -> Any:
        raise RuntimeError("pipeline boom str(exc) MUST NOT LEAK")


class _RaisingIndex:
    async def get_blueprint(self, blueprint_id: str) -> Any:
        raise RuntimeError("store boom str(exc) MUST NOT LEAK")

    async def recall(self, *, query_vector: list[float], kind: str, k: int) -> Any:  # pragma: no cover
        raise RuntimeError("unused")


async def test_crash_guard_all_three_tools_internal_error_never_leaks() -> None:
    sb = SearchBlueprintsTool(pipeline=_RaisingPipeline(), default_k=5, max_k=20)  # type: ignore[arg-type]
    sk = SearchKnowledgeTool(pipeline=_RaisingPipeline(), knowledge_k=5)  # type: ignore[arg-type]
    gb = GetBlueprintTool(vector_index=_RaisingIndex())  # type: ignore[arg-type]
    for tool in (sb, sk):
        r = await tool.run({"query": _Q}, _creds())
        assert r.status == "error"
        assert r.error_code == "RETRIEVAL_TOOL_INTERNAL_ERROR"
        assert r.retryable is False
        assert r.provenance == frozenset()  # still safe-empty on the crash path
        assert "boom" not in (r.user_message or "")  # never str(exc)
    rg = await gb.run({"id": "bp-x"}, _creds())
    assert rg.error_code == "RETRIEVAL_TOOL_INTERNAL_ERROR"
    assert "boom" not in (rg.user_message or "")


# ---------------------------------------------------------------------------
# getBlueprint non-oracle — out-of-scope and absent are BYTE-identical (all fields)
# ---------------------------------------------------------------------------


async def test_get_blueprint_out_of_scope_vs_absent_all_fields_identical() -> None:
    index = FakeVectorIndex(details={"secret": _detail("secret", frozenset({_A}))})
    tool = GetBlueprintTool(vector_index=index)
    scope = frozenset({_B})  # does not cover the blueprint's uses ({_A})

    oos = await tool.run({"id": "secret"}, _creds(scope))
    absent = await tool.run({"id": "totally-unknown-id"}, _creds(scope))

    # Every model-visible and trail-visible field must match — a caller cannot
    # tell an out-of-scope blueprint from one that does not exist (anti-oracle).
    assert oos.status == absent.status == "ok"
    assert oos.error_code == absent.error_code
    assert oos.retryable == absent.retryable
    assert oos.user_message == absent.user_message
    assert oos.provenance == absent.provenance == frozenset()
    assert oos.result_full == absent.result_full == {"found": False}
    assert (
        (oos.result_preview.to_doc() if oos.result_preview else None)
        == (absent.result_preview.to_doc() if absent.result_preview else None)
    )


async def test_get_blueprint_returned_uses_never_exceeds_scope() -> None:
    # DESIGN PIN (§3): under a NARROWED (non-empty) scope, getBlueprint only
    # returns when uses ⊆ scope — so the exposed `uses` list can never contain a
    # column identifier outside the caller's scope. (Under allow-all/empty scope
    # the full uses is returned verbatim — everything is in scope by definition.)
    index = FakeVectorIndex(details={"bp-x": _detail("bp-x", frozenset({_A, _B}))})
    tool = GetBlueprintTool(vector_index=index)

    narrowed = await tool.run({"id": "bp-x"}, _creds(frozenset({_A, _B})))
    assert set(narrowed.result_full["uses"]) <= {_A, _B}

    allow_all = await tool.run({"id": "bp-x"}, _creds(frozenset()))
    assert allow_all.result_full["uses"] == sorted([_A, _B])


# ---------------------------------------------------------------------------
# degrade observability — no embedder → ok+empty+degraded AND start/ok events
# ---------------------------------------------------------------------------


async def test_degrade_no_embedder_emits_start_and_ok_events() -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    def observer(name: str, payload: dict[str, Any]) -> None:
        events.append((name, payload))

    idx = FakeVectorIndex([_bp("a", "x", {_A}, [1.0, 0.0])])
    tool = SearchBlueprintsTool(
        pipeline=_pipeline(index=idx, embedder=None), default_k=5, max_k=20, observer=observer
    )
    r = await tool.run({"query": _Q}, _creds())
    assert r.status == "ok"
    assert r.result_full["count"] == 0
    assert r.result_full["degraded"] is True
    names = [n for n, _ in events]
    assert names == ["tool_dispatch_start", "tool_dispatch_ok"]
    # Shape-only: the query text never rides in a progress payload (D25/D61).
    assert all(_Q not in repr(p) for _, p in events)


async def test_degrade_emits_error_event_on_invalid_args() -> None:
    events: list[str] = []
    tool = SearchBlueprintsTool(
        pipeline=_pipeline(index=FakeVectorIndex([])),
        default_k=5,
        max_k=20,
        observer=lambda n, _p: events.append(n),
    )
    await tool.run({}, _creds())  # missing query
    assert events == ["tool_dispatch_start", "tool_dispatch_error"]


# ---------------------------------------------------------------------------
# config k through the TOOL layer (not just settings validation)
# ---------------------------------------------------------------------------


async def test_config_defaults_flow_through_tool_layer() -> None:
    settings = RuntimeSettings()
    assert (settings.retrieval_search_default_k, settings.retrieval_search_max_k) == (5, 20)
    idx = FakeVectorIndex([_bp(f"b{i}", f"i{i}", {_A}, [1.0, i / 100]) for i in range(25)])
    sb = SearchBlueprintsTool(
        pipeline=_pipeline(index=idx, reranker=None, recall_k=30),
        default_k=settings.retrieval_search_default_k,
        max_k=settings.retrieval_search_max_k,
    )
    assert (await sb.run({"query": _Q}, _creds())).result_full["count"] == 5  # default
    assert (await sb.run({"query": _Q, "k": 999}, _creds())).result_full["count"] == 20  # clamp

    kidx = FakeVectorIndex([_kn(f"k{i}", f"c{i}", [1.0, i / 100]) for i in range(20)])
    sk = SearchKnowledgeTool(
        pipeline=_pipeline(index=kidx, reranker=None),
        knowledge_k=settings.retrieval_search_knowledge_k,
    )
    assert (await sk.run({"query": _Q}, _creds())).result_full["count"] == 5  # knowledge_k cut


# ---------------------------------------------------------------------------
# schema validity + no credential-shaped params + non-empty descriptions
# ---------------------------------------------------------------------------

_CREDENTIAL_DENYLIST = frozenset(
    {
        "jwt",
        "session_id",
        "sessionid",
        "scope",
        "column_scope",
        "columnscope",
        "client",
        "client_id",
        "tenant",
        "tenant_id",
        "credentials",
        "api_key",
        "apikey",
        "token",
        "password",
        "secret",
    }
)


def _all_runtime_schemas() -> list[dict[str, Any]]:
    from data_agent.runtime.mcp.tool_schema import (
        ASK_USER_TOOL_SCHEMA,
        GET_BLUEPRINT_TOOL_SCHEMA,
        RESOLVE_VALUES_TOOL_SCHEMA,
        SEARCH_BLUEPRINTS_TOOL_SCHEMA,
        SEARCH_KNOWLEDGE_TOOL_SCHEMA,
    )

    return [
        ASK_USER_TOOL_SCHEMA,
        RESOLVE_VALUES_TOOL_SCHEMA,
        SEARCH_BLUEPRINTS_TOOL_SCHEMA,
        GET_BLUEPRINT_TOOL_SCHEMA,
        SEARCH_KNOWLEDGE_TOOL_SCHEMA,
    ]


def test_read_tool_schemas_are_valid_jsonschema() -> None:
    from data_agent.runtime.mcp.tool_schema import (
        GET_BLUEPRINT_TOOL_SCHEMA,
        SEARCH_BLUEPRINTS_TOOL_SCHEMA,
        SEARCH_KNOWLEDGE_TOOL_SCHEMA,
    )

    try:
        import jsonschema

        validator_cls = jsonschema.Draft202012Validator
    except ImportError:  # pragma: no cover - structural fallback
        validator_cls = None

    for schema in (
        SEARCH_BLUEPRINTS_TOOL_SCHEMA,
        GET_BLUEPRINT_TOOL_SCHEMA,
        SEARCH_KNOWLEDGE_TOOL_SCHEMA,
    ):
        params = schema["parameters"]
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict)
        assert set(params.get("required", [])) <= set(params["properties"])
        if validator_cls is not None:
            validator_cls.check_schema(params)  # raises SchemaError if invalid


def test_no_runtime_schema_declares_a_credential_shaped_param() -> None:
    for schema in _all_runtime_schemas():
        for name in schema["parameters"].get("properties", {}):
            lowered = name.lower()
            assert lowered not in _CREDENTIAL_DENYLIST, f"{schema['name']}.{name}"
            assert not any(tok in lowered for tok in ("jwt", "secret", "password")), name


def test_every_runtime_schema_param_has_a_nonempty_description() -> None:
    for schema in _all_runtime_schemas():
        assert schema["description"].strip(), schema["name"]
        for name, spec in schema["parameters"].get("properties", {}).items():
            # nested objects (resolveValues.period) also carry described children
            if "description" in spec:
                assert spec["description"].strip(), f"{schema['name']}.{name}"
