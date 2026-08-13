"""Layer-4 harness plumbing — the recording observer, the fixture loader, and the
app builder (07 §B).

Everything here stands up the SHIPPED composition: `create_app(...)` with the
real `AgentLoop`, the real `ContextAssembler` carrying the real base system
prompt, the real `RetrievalPipeline`, the real evidence validators and the real
finalization enforcement. Only the four EDGES are doubled — the MCP transport,
the session store, the embedder/vector index, and (in A1 only) the model.

Two gates that cost an afternoon if you rediscover them:

  * `searchBlueprints` / `getBlueprint` / `runBlueprint` are wired ONLY
    `if active_retrieval is not None` (`app.py`). Without an injected pipeline
    every blueprint case gets `RETRIEVAL_TOOL_UNAVAILABLE` and the case fails for
    a wiring reason that looks like a routing result.
  * blueprint recall PRE-FILTERS on `uses ⊆ column_scope`
    (`retrieval/scope_filter.py`), so an EMPTY `column_scope` drops every card.
    Empty means "allow-all" for QUERY execution (`auth/credentials.py`) and
    "match nothing" for blueprint recall — the two are not the same switch.

The corpus is loaded from `tests/fixtures/corpus/` through the real
`load_seed_fixtures` + `resolve_blueprint_references`, never hand-copied, so a
fixture cannot drift from the seeded corpus and `ref:` nodes are inlined exactly
as the hydrator inlines them.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from data_agent.runtime import app as app_module
from data_agent.runtime.app import create_app
from data_agent.runtime.composite.analysis_state import INTENT_TAGGABLE_TOOLS
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.mcp.client import MCPToolError, MCPToolSpec
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.mcp.scratch_client import FakeScratchClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.retrieval.corpus_loader import (
    BlueprintSeed,
    load_seed_fixtures,
    resolve_blueprint_references,
)
from data_agent.runtime.retrieval.models import BlueprintDetail, Candidate
from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import AnalysisState, TrailEntry
from tests._catalog_fixture import fixture_catalog_handle

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORPUS_DIR = _REPO_ROOT / "tests" / "fixtures" / "corpus"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "routing"

SESSION_ID = "sess-eval"
HEADERS = {"Authorization": "Bearer eval-jwt", "X-Session-Id": SESSION_ID}


# ---------------------------------------------------------------------------
# The recording observer — the reason 07 §B.1 adds `create_app(extra_observers=)`
# ---------------------------------------------------------------------------


class RecordingObserver:
    """A `ToolObserver` that keeps every `(event, payload)` the runtime emits.

    SSE is not a substitute: `observability/progress.py::to_progress_event` drops
    any event it does not recognise, which is most of 06's set. And a
    hand-assembled `AgentLoop` is not a substitute either — it would duplicate
    `_build_agent_loop` and, because `ContextAssembler` defaults
    `base_system_prompt=None`, could silently run with NO PROMPT AT ALL.

    Payloads are copied on capture: the loop reuses dicts in places, and a
    recorder holding live references would report whatever the payload became.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, dict(payload)))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def payloads(self, event: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.events if name == event]

    def count(self, event: str) -> int:
        return len(self.payloads(event))


# ---------------------------------------------------------------------------
# Fixtures (07 §C)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScriptedCall:
    """One scripted tool call, plus its `serves_intent` tag.

    `serves_intent` is LOAD-BEARING (07 §C.2): the re-derivation predicate is
    INTENT-scoped, not turn-scoped, and without the per-call intent label the
    turn-scoped form flags case 3 — which `prompts.py` explicitly permits ("You
    MAY run further queries only for a DISTINCT part of the user's question that
    the blueprint did not answer").

    IT IS NO LONGER FIXTURE-ONLY. When 07 was written, `serves_intent` was a
    harness concept that lived on the fixture's tool call and was stripped before
    dispatch. Call-time intent tagging made it a REAL model-facing argument on
    `runQuery`/`runBlueprint`/`getTableSchema`, so `build_model` now passes it into
    the tool call's ARGUMENTS and the runtime does the stripping and the
    validation. The fixture key is unchanged; what changed is that the label is now
    measured off the persisted `TrailEntry.serves_intent` rather than believed
    because a YAML file said so.
    """

    name: str
    id: str
    args: dict[str, Any]
    serves_intent: str | None = None


@dataclass(frozen=True)
class ScriptedRound:
    assistant_text: str | None
    tool_calls: tuple[ScriptedCall, ...]


@dataclass(frozen=True)
class HttpRequest:
    """One HTTP call the case makes. `kind` is `turn` or `resume`."""

    kind: str
    message: str | None = None
    answer: str | None = None


@dataclass(frozen=True)
class RoutingCase:
    id: str
    question: str
    ground_truth_multi_intent: bool
    ground_truth_intent_count: int
    column_scope: frozenset[str]
    corpus: tuple[str, ...]
    embedding: dict[str, list[float]]
    mcp_tools: tuple[str, ...]
    mcp_script: dict[str, list[Any]]
    model_script: tuple[ScriptedRound, ...]
    requests: tuple[HttpRequest, ...]
    expect: dict[str, Any] = field(default_factory=dict)

    @property
    def serves_intent(self) -> dict[str, str]:
        """`tool_call_id -> intent_id`, from the fixture's own declarations."""
        mapping: dict[str, str] = {}
        for round_ in self.model_script:
            for call in round_.tool_calls:
                if call.serves_intent:
                    mapping[call.id] = call.serves_intent
        return mapping


def _parse_call(raw: dict[str, Any]) -> ScriptedCall:
    unknown = set(raw) - {"name", "id", "args", "serves_intent"}
    if unknown:
        raise ValueError(f"unknown tool-call key(s) {sorted(unknown)} in routing fixture")
    return ScriptedCall(
        name=raw["name"],
        id=raw["id"],
        args=dict(raw.get("args") or {}),
        serves_intent=raw.get("serves_intent"),
    )


def load_case(path: Path) -> RoutingCase:
    raw = yaml.safe_load(path.read_text())
    ground_truth = raw.get("ground_truth") or {}
    question = raw["question"]
    requests_raw = raw.get("requests") or [{"kind": "turn", "message": question}]
    return RoutingCase(
        id=raw["id"],
        question=question,
        ground_truth_multi_intent=bool(ground_truth["multi_intent"]),
        ground_truth_intent_count=int(ground_truth["intent_count"]),
        column_scope=frozenset(raw.get("column_scope") or ()),
        corpus=tuple(raw.get("corpus") or ()),
        embedding={k: [float(x) for x in v] for k, v in (raw.get("embedding") or {}).items()},
        mcp_tools=tuple(raw.get("mcp_tools") or ()),
        mcp_script={k: list(v) for k, v in (raw.get("mcp_script") or {}).items()},
        model_script=tuple(
            ScriptedRound(
                assistant_text=round_.get("assistant_text"),
                tool_calls=tuple(_parse_call(c) for c in (round_.get("tool_calls") or ())),
            )
            for round_ in (raw.get("model_script") or ())
        ),
        requests=tuple(
            HttpRequest(
                kind=r["kind"], message=r.get("message"), answer=r.get("answer")
            )
            for r in requests_raw
        ),
        expect=dict(raw.get("expect") or {}),
    )


def load_cases() -> list[RoutingCase]:
    return [load_case(path) for path in sorted(FIXTURE_DIR.glob("*.yaml"))]


def case_by_id(case_id: str) -> RoutingCase:
    path = FIXTURE_DIR / f"{case_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"no routing fixture {case_id!r} under {FIXTURE_DIR}")
    return load_case(path)


# ---------------------------------------------------------------------------
# Corpus — loaded through the real loader, never hand-copied
# ---------------------------------------------------------------------------


def seed_corpus() -> dict[str, BlueprintSeed]:
    """Every committed blueprint seed, with `ref:` composition nodes INLINED.

    `resolve_blueprint_references` must see the WHOLE list (a reference may point
    at a blueprint outside the case's own subset), so the resolve runs before the
    per-case selection, exactly as `load_corpus` does it.
    """
    blueprints, _knowledge = load_seed_fixtures(_CORPUS_DIR)
    return {seed.id: seed for seed in resolve_blueprint_references(blueprints)}


def blueprint_detail(seed: BlueprintSeed) -> BlueprintDetail:
    return BlueprintDetail(
        id=seed.id,
        intent=seed.intent,
        slots_summary=seed.slots_summary,
        uses=frozenset(seed.uses),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha=seed.catalog_sha or "",
        resolves=dict(seed.resolves) if seed.resolves else None,
        slots=[dict(s) for s in (seed.slots or [])] or None,
        uses_rules=list(seed.uses_rules or []) or None,
        sql_template=seed.sql_template,
        composes=[dict(c) for c in (seed.composes or [])] or None,
        result_grain=seed.result_grain,
    )


def blueprint_candidate(seed: BlueprintSeed) -> Candidate:
    """The recall projection — the same payload keys `_RECALL_QUERY` returns.

    `pipeline._to_thin_card` is the ONE place a card is built, and it reads
    `intent`/`slots_summary`/`resolves`/`slots`/`result_grain` off this payload.
    A key missing here is silently dropped from every card (no error, no failing
    assertion) — which is exactly how the 02 enrichment would appear to ship
    while carrying nothing.
    """
    return Candidate(
        id=seed.id,
        kind="blueprint",
        text=seed.intent,
        uses=frozenset(seed.uses),
        payload={
            "intent": seed.intent,
            "slots_summary": seed.slots_summary,
            "resolves": dict(seed.resolves) if seed.resolves else None,
            "slots": [dict(s) for s in (seed.slots or [])],
            "result_grain": seed.result_grain,
        },
    )


_UNIT_VECTOR = [1.0, 0.0]


def build_retrieval(case: RoutingCase) -> RetrievalPipeline:
    """The real `RetrievalPipeline` over a `FakeVectorIndex` seeded with the
    case's slice of the committed corpus.

    Every card is stored at the same vector as the question, so recall returns
    the whole slice at score 1.0 with a deterministic id tiebreak: the case
    controls WHICH blueprints are visible, and the RUNTIME controls whether the
    `uses ⊆ column_scope` pre-filter then drops any of them (which is the point of
    case 7).
    """
    seeds = seed_corpus()
    index = FakeVectorIndex()
    for blueprint_id in case.corpus:
        seed = seeds.get(blueprint_id)
        if seed is None:
            raise AssertionError(
                f"{case.id}: blueprint {blueprint_id!r} is not in the committed corpus"
            )
        index.add(blueprint_candidate(seed), list(_UNIT_VECTOR))
        index.add_detail(blueprint_detail(seed))
    return RetrievalPipeline(
        embedding_client=FakeEmbeddingClient(dict(case.embedding)),
        reranker=None,
        vector_index=index,
        user_memory=NullUserMemoryProvider(),
        recall_k=30,
        # Above the number of blueprints any case seeds, so a card that is ABSENT
        # from the pre-injected block was dropped by the scope pre-filter rather
        # than cut by top-k. Case 7 asserts exactly that difference.
        top_k_blueprints=8,
        top_k_knowledge=3,
    )


def _mcp_response(raw: Any) -> Any:
    """One scripted MCP response. `{error: {code, message}}` becomes the
    `MCPToolError` the real MCP raises for a tool-level denial.

    COLUMN-SCOPE ENFORCEMENT IS SERVER-SIDE. `RuntimeCredentials.column_scope` is
    consumed by the runtime for blueprint recall and for the D44 replay filter;
    the QUERY-time denial is raised by `clickhouse-api` and reaches the runtime as
    an `MCPToolError("[COLUMN_SCOPE_VIOLATION] …")`. So a narrowed scope is real
    for recall and SCRIPTED for the denial — there is no in-process code path that
    would turn a narrow scope into a denied `runQuery` on its own.
    """
    if isinstance(raw, dict) and "error" in raw and len(raw) == 1:
        spec = raw["error"]
        return MCPToolError(spec.get("code"), spec.get("message", ""))
    return raw


def build_mcp(case: RoutingCase) -> FakeMCPClient:
    """The scripted MCP transport.

    `FakeMCPClient` consumes `{tool_name: [responses…]}` IN ORDER PER TOOL NAME
    and raises `AssertionError` when a tool is called more often than scripted —
    a fixture-authoring bug, surfaced loudly rather than as a mystery denial. It
    also serves the TOOL CATALOGUE from its `tools=[…]` list, so a tool missing
    from `mcp_tools` is an unknown tool to the model.
    """
    return FakeMCPClient(
        tools=[
            MCPToolSpec(
                name=name, description="", input_schema={"type": "object", "properties": {}}
            )
            for name in case.mcp_tools
        ],
        scripted={
            tool: [_mcp_response(item) for item in items]
            for tool, items in case.mcp_script.items()
        },
    )


def _scripted_arguments(call: ScriptedCall) -> dict[str, Any]:
    """The arguments the scripted model "sends" — the fixture's `args` plus the
    `serves_intent` TAG when the tool takes one.

    The tag rides the arguments because that is where a real model puts it: the
    schemas for `runQuery`, `runBlueprint` and `getTableSchema` advertise it, and
    the runtime strips it at the dispatch boundary. Injecting it here (rather than
    letting the harness carry it out of band, as it did when the field was
    fixture-only) is what makes the A1 fixtures exercise the REAL mechanism —
    including the strip, the validation against the live state, and the persistence
    onto `TrailEntry.serves_intent` that A2's derivation depends on.
    """
    if call.serves_intent and call.name in INTENT_TAGGABLE_TOOLS:
        return {**call.args, "serves_intent": call.serves_intent}
    return dict(call.args)


def build_model(case: RoutingCase) -> ScriptedModelClient:
    return ScriptedModelClient(
        [
            ModelTurnResult(
                assistant_text=round_.assistant_text,
                tool_calls=[
                    ToolCallRequest(
                        id=c.id, name=c.name, arguments=_scripted_arguments(c)
                    )
                    for c in round_.tool_calls
                ],
            )
            for round_ in case.model_script
        ]
    )


def eval_settings(**overrides: Any) -> RuntimeSettings:
    """The settings A1 runs under.

    `_env_file=None` so a developer's local `.env` cannot change what CI measures.
    `discovery_emulation_enabled=False` because the sweep would add
    `listDatabases`/`listTables` MCP dispatches the per-tool scripted queues do
    not carry — it is covered directly in
    `tests/runtime/context/test_discovery_emulation.py`.

    THE BASE PROMPT IS NOT OVERRIDDEN. `agent_system_prompt_enabled` stays at its
    shipped default, so `ContextAssembler` gets the real prompt. A1 cannot FAIL on
    a bad prompt (see README), but it must not run without one either.

    THE BUDGET IS PINNED HERE ON PURPOSE, and it tracks the shipped defaults (raised
    2026-08-12 to 25 iterations / 180s on live wall-clock evidence; the per-window
    token SPEND ceiling added the same day). Pinned so a config edit cannot silently
    change what A1 measures; tracking the shipped values so A1 measures the runtime
    that ships. None of these bind in this harness — the scripted model returns
    instantly, reports no `usage` at all (so spend stays 0), and no case scripts more
    than a handful of rounds — so a case that NEEDS a cap must override it explicitly
    and say why.
    """
    defaults: dict[str, Any] = {
        "_env_file": None,
        "discovery_emulation_enabled": False,
        "max_loop_iterations": 25,
        "max_wall_clock_seconds": 180,
        "max_budget_windows": 3,
        "max_window_token_spend": 1_000_000,
    }
    defaults.update(overrides)
    return RuntimeSettings(**defaults)


@dataclass
class EvalHarness:
    case: RoutingCase
    client: TestClient
    store: InMemorySessionStore
    mcp: FakeMCPClient
    model: ScriptedModelClient
    observer: RecordingObserver
    scratch: FakeScratchClient
    outcomes: list[dict[str, Any]] = field(default_factory=list)

    # -- reads over what the runtime actually persisted -------------------

    def doc(self) -> Any:
        import anyio

        return anyio.run(self.store.get_or_create_session, SESSION_ID)

    def trail(self) -> list[TrailEntry]:
        import anyio

        return list(anyio.run(self.store.load_trail, SESSION_ID))

    def analysis_state(self) -> AnalysisState | None:
        return self.doc().analysis_state

    def entry(self, tool_call_id: str) -> TrailEntry:
        for entry in self.trail():
            if entry.tool_call_id == tool_call_id:
                return entry
        raise AssertionError(f"no trail entry for tool_call_id {tool_call_id!r}")

    def model_messages(self, round_index: int) -> list[dict[str, Any]]:
        return self.model.calls[round_index].messages

    def system_messages(self, round_index: int) -> list[str]:
        return [
            str(m.get("content") or "")
            for m in self.model_messages(round_index)
            if m.get("role") == "system"
        ]

    def user_messages(self, round_index: int) -> list[str]:
        return [
            str(m.get("content") or "")
            for m in self.model_messages(round_index)
            if m.get("role") == "user"
        ]


def build_harness(case: RoutingCase, monkeypatch: pytest.MonkeyPatch) -> EvalHarness:
    """`TestClient(create_app(...))` — the SHIPPED composition, with the recording
    observer folded in through the 07 §B.1 seam."""
    scope = case.column_scope
    monkeypatch.setattr(app_module, "verify_jwt", lambda *args, **kwargs: scope)

    store = InMemorySessionStore()
    mcp = build_mcp(case)
    model = build_model(case)
    observer = RecordingObserver()
    scratch = FakeScratchClient()
    app = create_app(
        settings=eval_settings(),
        session_store=store,
        mcp_client=mcp,
        model_client=model,
        catalog=fixture_catalog_handle(),
        retrieval=build_retrieval(case),
        scratch_client=scratch,
        extra_observers=(observer,),
    )
    return EvalHarness(
        case=case,
        client=TestClient(app),
        store=store,
        mcp=mcp,
        model=model,
        observer=observer,
        scratch=scratch,
    )


def parse_sse(body: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        lines = block.splitlines()
        event_line = next(line for line in lines if line.startswith("event:"))
        data_line = next(line for line in lines if line.startswith("data:"))
        events.append(
            {
                "event": event_line.split(":", 1)[1].strip(),
                "data": json.loads(data_line.split(":", 1)[1].strip()),
            }
        )
    return events


def drive(harness: EvalHarness) -> list[dict[str, Any]]:
    """Issue the case's HTTP requests in order and collect each turn's result.

    A case is a SEQUENCE of requests, not one turn: cases 9 and 10 are the only
    coverage of an intent lost BETWEEN rounds, and both need a pause and a second
    request to express it at all.
    """
    for request in harness.case.requests:
        if request.kind == "turn":
            response = harness.client.post(
                "/turn", json={"message": request.message}, headers=HEADERS
            )
        elif request.kind == "resume":
            response = harness.client.post(
                "/turn/resume", json={"answer": request.answer}, headers=HEADERS
            )
        else:  # pragma: no cover - fixture-authoring bug
            raise AssertionError(f"unknown request kind {request.kind!r}")
        assert response.status_code == 200, response.text
        events = parse_sse(response.text)
        final = events[-1]
        assert final["event"] == "result", f"{harness.case.id}: {final}"
        harness.outcomes.append(final["data"])
    return harness.outcomes


@pytest.fixture
def harness_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """`factory(case_id) -> EvalHarness`, already driven through its requests."""

    def _factory(case_id: str) -> EvalHarness:
        harness = build_harness(case_by_id(case_id), monkeypatch)
        drive(harness)
        return harness

    yield _factory


def rendered_blueprint_block(messages: Sequence[dict[str, Any]]) -> str:
    """The pre-injected retrieval block, or `""`.

    Identified by `retrieval/render.py`'s own header text rather than by position:
    05 §D.1 owns the splice ORDER (state block before the question, nudge last),
    and pinning this helper to an index would make it fail for the ordering rather
    than for the content.
    """
    for message in messages:
        content = str(message.get("content") or "")
        if "Candidate blueprints" in content:
            return content
    return ""
