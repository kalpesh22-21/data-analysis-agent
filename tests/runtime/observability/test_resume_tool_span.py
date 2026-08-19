"""R7: the RESUME half of the `runBlueprint` TOOL span.

A turn that pauses at an approval node and comes back through `/turn/resume` does its
real work in `AgentLoop._resume_blueprint`, which re-enters the executor DIRECTLY —
bypassing `RunBlueprintTool` and therefore the envelope that spans the first call. The
gap was visible in Phoenix as a trace with a span for the pausing half and nothing at
all for the half that produced the answer.

Layer-1: the REAL `AgentLoop` + real `BlueprintExecutor` + real OTel SDK with an
InMemorySpanExporter, `ScriptedModelClient` and the in-memory store — no OpenAI spend,
no Phoenix. Proves:
  - the resume opens EXACTLY ONE TOOL span, `tool.name=runBlueprint`, and the span is
    told apart from the first call by `tool.args.resumed=True`;
  - `tool.args.*` is EXACTLY the allowlisted projection (whole-namespace equality, so a
    future enrichment field has to be declared here — D25 deny-by-default);
  - a raising executor stamps `tool.status=error` with `tool.error_code`, and neither
    the exception text nor a slot VALUE reaches the span's attributes or events;
  - `tracer=None` (every Layer-1 loop test) creates no span and changes nothing.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import BlueprintExecutor
from data_agent.runtime.blueprint.tool import RunBlueprintTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.observability import tracing
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex
from data_agent.runtime.session.memory_store import InMemorySessionStore
from tests._blueprint_gate import expand_blueprint

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle(
    {
        _E: {
            "EmployeeCode": "String",
            "Department": "Nullable(String)",
            "AnnualSalary": "Nullable(Float64)",
        }
    }
)
SESSION_ID = "sess-r7-resume-span"
_BID = "bp-flag-departments"
PII_SLOT_VALUE = "Jane Doe, Radiology"
CRASH_TEXT = f"neo4j blip while re-fetching for {PII_SLOT_VALUE}"


def _creds() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


def _rq(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


def _tracer_with_exporter() -> tuple[Any, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return tracing.get_tracer(provider), exporter


def _tool_spans(exporter: InMemorySpanExporter) -> list[Any]:
    return [
        s
        for s in exporter.get_finished_spans()
        if s.attributes.get(SpanAttributes.OPENINFERENCE_SPAN_KIND)
        == OpenInferenceSpanKindValues.TOOL.value
    ]


def _detail() -> BlueprintDetail:
    return BlueprintDetail(
        id=_BID,
        intent="Flag departments above the company average",
        slots_summary="",
        uses=frozenset({f"{_E}.Department", f"{_E}.AnnualSalary", f"{_E}.EmployeeCode"}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        composes=[
            {
                "order": 0,
                "output": {"n": "scalar"},
                "sql_template": "SELECT count() AS n FROM dbpcm_warehouse.employee",
            },
            {
                "order": 1,
                "node_kind": "approval",
                "feeds_from": [0],
                "requires_approval": {"prompt": "Flag these departments — proceed?"},
                "sql_template": (
                    "SELECT Department AS department FROM dbpcm_warehouse.employee "
                    "GROUP BY Department"
                ),
                "output": {},
            },
        ],
        result_grain=["Department"],
    )


async def _tools_provider(_c: RuntimeCredentials) -> list[dict]:
    return [{"type": "function", "name": "runBlueprint", "description": "", "parameters": {}}]


def _make_loop(
    store: InMemorySessionStore,
    model: ScriptedModelClient,
    mcp: FakeMCPClient,
    *,
    tracer: Any = None,
    executor: Any = None,
) -> AgentLoop:
    index = FakeVectorIndex()
    index.add_detail(_detail())
    real_executor = BlueprintExecutor(
        tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=index
    )
    return AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        runtime_tools={"runBlueprint": RunBlueprintTool(executor=real_executor)},
        blueprint_executor=executor or real_executor,
        tracer=tracer,
    )


def _prose_resume_script(text: str) -> list[ModelTurnResult]:
    """Two identical prose finishes: the resumed blueprint returns TWO rows, so the
    first bare-prose finish trips the answer-shape gate (05 §J) and costs a round. Same
    helper (and same reason) as `tests/runtime/blueprint/test_loop_resume_dag.py`."""
    return [ModelTurnResult(assistant_text=text), ModelTurnResult(assistant_text=text)]


async def _run_to_approval_pause(store: InMemorySessionStore) -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="c1", name="runBlueprint", arguments={"id": _BID, "slot_bindings": {}}
                    )
                ]
            ),
        ]
    )
    mcp = FakeMCPClient(scripted={"runQuery": [_rq(["n"], [[42]])]})  # node 0 only
    loop = _make_loop(store, model, mcp)
    await expand_blueprint(store, SESSION_ID, _BID)
    paused = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="flag depts")
    assert paused.status == "paused_ask_user"


def _resume_mcp() -> FakeMCPClient:
    return FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department"], [["Sales"], ["Eng"]]),  # node 1 query (approved)
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),  # grain probe
            ]
        }
    )


# ---------------------------------------------------------------------------
# (a) the span exists at all, and is stamped `ok`
# ---------------------------------------------------------------------------


async def test_resume_opens_exactly_one_tool_span_stamped_ok() -> None:
    """The gap R7 names: the resumed executor re-entry now carries a TOOL span, and
    exactly one — the first call's span belongs to the earlier, already-finished turn."""
    store = InMemorySessionStore()
    await _run_to_approval_pause(store)

    tracer, exporter = _tracer_with_exporter()
    loop = _make_loop(
        store,
        ScriptedModelClient(_prose_resume_script("Flagged 2 departments.")),
        _resume_mcp(),
        tracer=tracer,
    )
    done = await loop.resume(session_id=SESSION_ID, credentials=_creds(), answer="approve")

    assert done.status == "done"
    (span,) = _tool_spans(exporter)
    assert span.name == "tool.runBlueprint"
    assert span.attributes["tool.name"] == "runBlueprint"
    # The status is stamped from the OUTCOME, not left on the envelope's optimistic
    # open — an `ok` here is only meaningful next to the error case below.
    assert span.attributes["tool.status"] == "ok"
    assert "tool.error_code" not in span.attributes


async def test_resume_span_is_identifiable_as_the_resume_half() -> None:
    """Same `tool.name` as the first call (so it groups with it and agrees with the
    `tool_dispatch_*` progress events), told apart by `tool.args.resumed`."""
    store = InMemorySessionStore()
    await _run_to_approval_pause(store)

    tracer, exporter = _tracer_with_exporter()
    loop = _make_loop(
        store,
        ScriptedModelClient(_prose_resume_script("Flagged 2 departments.")),
        _resume_mcp(),
        tracer=tracer,
    )
    await loop.resume(session_id=SESSION_ID, credentials=_creds(), answer="approve")

    (span,) = _tool_spans(exporter)
    assert span.attributes["tool.args.resumed"] is True


# ---------------------------------------------------------------------------
# (b) the args are the allowlisted projection and NOTHING else
# ---------------------------------------------------------------------------


async def test_resume_span_args_are_exactly_the_allowlisted_projection() -> None:
    """Whole-namespace equality rather than "the answer is absent", so a future field on
    this span has to be declared here — the D25 deny-by-default posture
    `test_outer_tools_disable_redaction.py` takes for `updateAnalysisState`.

    The two things that must never appear are the user's approval ANSWER (free text) and
    the slot VALUES; both are structurally excluded because the projection is built by
    hand from structural scalars and a COUNT, not forwarded from any arg dict."""
    store = InMemorySessionStore()
    await _run_to_approval_pause(store)

    tracer, exporter = _tracer_with_exporter()
    loop = _make_loop(
        store,
        ScriptedModelClient(_prose_resume_script("Flagged 2 departments.")),
        _resume_mcp(),
        tracer=tracer,
    )
    await loop.resume(
        session_id=SESSION_ID, credentials=_creds(), answer=f"approve — for {PII_SLOT_VALUE}"
    )

    (span,) = _tool_spans(exporter)
    arg_attributes = {k: v for k, v in span.attributes.items() if k.startswith("tool.args.")}
    assert arg_attributes == {
        "tool.args.id": _BID,
        "tool.args.resumed": True,
        "tool.args.awaiting_node": 1,
        "tool.args.slot_count": 0,
    }
    assert PII_SLOT_VALUE not in str(dict(span.attributes))


# ---------------------------------------------------------------------------
# (c) the error path: status stamped, no exception text, no slot values
# ---------------------------------------------------------------------------


class _RaisingExecutor:
    """Stands in for the executor blowing up on the re-fetch (the S3 case the resume
    path's crash guard exists for), with entity-bearing text in the message."""

    async def resume(self, **_kwargs: Any) -> Any:
        raise RuntimeError(CRASH_TEXT)


async def test_a_raising_resume_stamps_error_and_keeps_the_crash_text_off_the_span() -> None:
    """Two guarantees at once on the path most likely to leak.

    The crash guard converts the exception into a canned internal error, so the span sees
    an `error` OUTCOME (stamped, with its code) rather than a raised exception — and the
    envelope's `record_exception=False` belt is what keeps it content-free if that guard
    ever moved: no exception event, and no word of the message either way.

    The slot bindings are non-empty here (planted on the persisted checkpoint), so this
    also proves `slot_count` is a real count and that the VALUE behind it never reaches
    the span."""
    store = InMemorySessionStore()
    await _run_to_approval_pause(store)
    # Plant model-proposed slot bindings on the persisted checkpoint: the pausing
    # blueprint declares no slots, and a count of 0 could not distinguish "counted" from
    # "dropped the dict entirely".
    doc = await store.get_or_create_session(SESSION_ID)
    assert doc.pause_checkpoint is not None
    doc.pause_checkpoint = replace(
        doc.pause_checkpoint, slot_bindings_json=f'{{"department": "{PII_SLOT_VALUE}"}}'
    )

    tracer, exporter = _tracer_with_exporter()
    loop = _make_loop(
        store,
        ScriptedModelClient(_prose_resume_script("I could not complete that.")),
        _resume_mcp(),
        tracer=tracer,
        executor=_RaisingExecutor(),
    )
    await loop.resume(session_id=SESSION_ID, credentials=_creds(), answer="approve")

    (span,) = _tool_spans(exporter)
    assert span.attributes["tool.status"] == "error"
    assert span.attributes["tool.error_code"]
    assert span.attributes["tool.args.slot_count"] == 1
    assert [event.name for event in span.events] == []
    rendered = str(
        [dict(span.attributes or {}), [dict(e.attributes or {}) for e in span.events]]
    )
    assert CRASH_TEXT not in rendered
    assert PII_SLOT_VALUE not in rendered


# ---------------------------------------------------------------------------
# tracer=None — the contract every Layer-1 loop test depends on
# ---------------------------------------------------------------------------


async def test_resume_without_a_tracer_creates_no_span_and_still_completes() -> None:
    """Tracing is optional everywhere in the runtime; the loop's default `tracer=None`
    must run the resume unwrapped rather than skip or break it."""
    store = InMemorySessionStore()
    await _run_to_approval_pause(store)

    _tracer, exporter = _tracer_with_exporter()
    loop = _make_loop(
        store,
        ScriptedModelClient(_prose_resume_script("Flagged 2 departments.")),
        _resume_mcp(),
    )
    done = await loop.resume(session_id=SESSION_ID, credentials=_creds(), answer="approve")

    assert done.status == "done"
    assert _tool_spans(exporter) == []
