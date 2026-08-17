"""The trim-aware re-fetch exemption to the repeated-idempotent-read guard.

**The defect this closes.** The guard's premise is *"the already-served result is in
the history above"*. That is true of the persisted TRAIL — which is what
`seen_read_calls` is seeded from — and NOT of the RENDERED window:
`context/budget.py::fit_request_to_budget` pins only the K most recent current-turn
tool pairs and drops older ones under budget pressure, and `context/assembly.py`
replaces a D44-stranded entry with a data-free sentinel. In either case the model was
told *"you already have this"* about something it demonstrably could not read, with
no move available that recovered it. It also made the base prompt lie: *"Fetch it
again only if it has been summarized away and you can no longer read it"* described
an escape the guard had welded shut.

The whole defect was proved by driving a REAL trim rather than by reasoning about the
code, and that is why the harness lives here as a test instead of as a throwaway:
`_trimming_loop` sets `request_token_budget=1` with `pinned_recent_tool_pairs=0`, so
round 1's tool pair is genuinely dropped from round 2's rebuild by the shipped
`fit_request_to_budget`. Nothing is faked or monkeypatched.

**What must NOT change:** when the result IS readable the guard fires exactly as
before. That is D94's protection against the observed dozens-of-re-fetches spin, and
`test_a_visible_repeat_is_still_guarded` is its pin.

Every assertion here checks the MCP client's own call log — "exempted" is meaningless
unless something actually re-fetched.
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import (
    _REPEATED_IDEMPOTENT_READ_NUDGE,
    ContextAssembler,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.loop.read_guard import _MAX_TRIMMED_READ_REFETCHES
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import TrailEntry

SESSION_ID = "sess-trim-refetch"
_DB = "dbpcm_warehouse"
_TABLE = "employee"
CATALOG = CatalogHandle({f"{_DB}.{_TABLE}": {"EmployeeCode": "String"}})

TOOLS_SCHEMA = [
    {"type": "function", "name": "getTableSchema", "description": "", "parameters": {}},
    {"type": "function", "name": "listTables", "description": "", "parameters": {}},
    {"type": "function", "name": "explainQuery", "description": "", "parameters": {}},
    {"type": "function", "name": "runQuery", "description": "", "parameters": {}},
]


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return list(TOOLS_SCHEMA)


def _creds(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=scope)


def _schema_response() -> dict[str, Any]:
    return {"database": _DB, "table": _TABLE, "columns": ["EmployeeCode"]}


def _schema_call(call_id: str) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id, name="getTableSchema", arguments={"database": _DB, "table": _TABLE}
    )


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, dict(payload)))

    def payloads(self, event: str) -> list[dict[str, Any]]:
        return [p for name, p in self.events if name == event]


def _loop(
    *,
    model: ScriptedModelClient,
    mcp: FakeMCPClient,
    observer: _Recorder,
    store: InMemorySessionStore | None = None,
    request_token_budget: int | None = None,
    pinned_recent_tool_pairs: int = 3,
    scope: frozenset[str] = frozenset(),
) -> tuple[AgentLoop, InMemorySessionStore]:
    store = store or InMemorySessionStore()
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(mcp, CATALOG),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=20,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=observer,
        request_token_budget=request_token_budget,
        request_budget_pinned_recent_tool_pairs=pinned_recent_tool_pairs,
    )
    return loop, store


def _trimming_loop(**kwargs: Any) -> tuple[AgentLoop, InMemorySessionStore]:
    """A loop whose request budget is unsatisfiable and which pins NO recent tool
    pairs — so every current-turn tool pair is a tier-2 droppable unit and the
    SHIPPED `fit_request_to_budget` removes it from the next rebuild. This is the
    real mechanism, not a simulation of one."""
    return _loop(request_token_budget=1, pinned_recent_tool_pairs=0, **kwargs)


def _dispatched(mcp: FakeMCPClient, tool_name: str) -> int:
    return len([c for c in mcp.calls if c.tool_name == tool_name])


# ---------------------------------------------------------------------------
# The defect, and the fix
# ---------------------------------------------------------------------------


async def test_a_visible_repeat_is_still_guarded() -> None:
    """D94's protection, unchanged and load-bearing: when the first result is still
    readable, an identical repeat is NOT re-dispatched. If this ever goes green by
    re-fetching, the exemption has swallowed the guard."""
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_schema_call("m1")]),
            ModelTurnResult(tool_calls=[_schema_call("m2")]),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    mcp = FakeMCPClient(scripted={"getTableSchema": [_schema_response()]})
    recorder = _Recorder()
    loop, store = _loop(model=model, mcp=mcp, observer=recorder)

    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="schema?")

    assert _dispatched(mcp, "getTableSchema") == 1
    assert len(recorder.payloads("loop_repeated_idempotent_read_guarded")) == 1
    assert recorder.payloads("loop_trimmed_read_refetch_allowed") == []
    # The model got the "you already have this" nudge, not a second schema.
    trail = await store.load_trail(SESSION_ID)
    assert [e.error_code for e in trail] == [None, "IDEMPOTENT_READ_ALREADY_SERVED"]


async def test_a_repeat_whose_result_was_trimmed_away_is_re_dispatched() -> None:
    """THE DEFECT. Before the exemption this dispatched ONCE and the model was left
    holding a nudge pointing at a schema the budget had deleted."""
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_schema_call("m1")]),
            ModelTurnResult(tool_calls=[_schema_call("m2")]),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    mcp = FakeMCPClient(scripted={"getTableSchema": [_schema_response(), _schema_response()]})
    recorder = _Recorder()
    loop, store = _trimming_loop(model=model, mcp=mcp, observer=recorder)

    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="schema?")

    # THE ASSERTION THAT MATTERS: the MCP really served it again.
    assert _dispatched(mcp, "getTableSchema") == 2
    assert recorder.payloads("loop_repeated_idempotent_read_guarded") == []
    # ...and the trim really happened, so this is not a vacuous pass.
    assert recorder.payloads("loop_request_budget_trimmed")

    allowed = recorder.payloads("loop_trimmed_read_refetch_allowed")
    assert len(allowed) == 1
    assert allowed[0]["tool_name"] == "getTableSchema"
    assert allowed[0]["reason"] == "result_not_readable_in_window"
    assert allowed[0]["deduped"] is False
    assert allowed[0]["dedup_target"] == f"{_DB}.{_TABLE}"
    assert allowed[0]["refetch_count"] == 0
    assert allowed[0]["refetch_cap"] == _MAX_TRIMMED_READ_REFETCHES

    # Two real entries — neither is a data-free guard marker.
    trail = await store.load_trail(SESSION_ID)
    assert [(e.tool_name, e.error_code) for e in trail] == [
        ("getTableSchema", None),
        ("getTableSchema", None),
    ]


async def test_two_identical_reads_in_one_response_still_dedup_to_one_dispatch() -> None:
    """The regression the exemption most easily causes, pinned separately.

    `readable_tool_call_ids` is computed from the window as it stood BEFORE the batch
    ran, so a read dispatched moments ago is necessarily absent from it. Treating
    that as "trimmed away" would re-dispatch the second of two identical calls in one
    message — exactly the duplicate the guard exists to collapse. Nothing can be
    trimmed between two calls of one batch, because no rebuild has happened."""
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_schema_call("m1"), _schema_call("m2")]),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    mcp = FakeMCPClient(scripted={"getTableSchema": [_schema_response()]})
    recorder = _Recorder()
    loop, _store = _trimming_loop(model=model, mcp=mcp, observer=recorder)

    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="schema?")

    assert _dispatched(mcp, "getTableSchema") == 1
    assert len(recorder.payloads("loop_repeated_idempotent_read_guarded")) == 1
    assert recorder.payloads("loop_trimmed_read_refetch_allowed") == []


# ---------------------------------------------------------------------------
# The stranded-sentinel hole — reachable for getTableSchema, unlike getBlueprint
# ---------------------------------------------------------------------------


async def test_a_stranded_read_rendered_as_a_sentinel_is_not_counted_as_readable() -> None:
    """Visibility must mean THE RESULT IS READABLE, not *some message bearing that
    id exists*.

    A D44-stranded (`ok` + undetermined-provenance) entry is rendered by
    `_build_withheld_sentinel_message` as a `tool` message under its OWN
    `tool_call_id` whose content is the data-free "result withheld … Do not retry"
    text. A naive `{m["tool_call_id"] for m in canonical if m["role"] == "tool"}`
    counts that as visible — so the guard would fire and the model, seeing only the
    withheld marker, could never recover the schema.

    ⚠ THE SCOPE ROUTE TO THIS IS NOT CURRENTLY LIVE, and saying so matters.
    `getTableSchema`/`listTables`/`listDatabases`/`explainQuery` are all
    `_NO_PROVENANCE_TOOLS` (`provenance/capture.py`), so they are recorded with
    safe-empty `frozenset()` provenance and can be neither scope-dropped nor
    stranded — a PRIOR fix moved `getTableSchema` there for exactly this symptom
    ("the model then saw the repeated-read guard's 'you already have this' nudge
    with no schema anywhere"). So this test seeds the stranded shape DIRECTLY rather
    than pretending a narrowed scope produces it: it is the pre-fix entry shape, and
    the shape any future re-classification would reintroduce. The sentinel exclusion
    is cheap and keeps the readability test honest whatever provenance policy a tool
    later acquires; the guard's OWN marker (next test) is the live case.
    """
    store = InMemorySessionStore()
    await store.append_trail_entry(
        SESSION_ID,
        TrailEntry(
            turn_index=0,
            tool_call_id="legacy-1",
            tool_name="getTableSchema",
            args={"database": _DB, "table": _TABLE},
            status="ok",
            error_code=None,
            # Undetermined -> D44 strands it -> rendered as the withheld sentinel.
            provenance=None,
            result_preview=None,
            result_full_ref=None,
            ts="2026-01-01T00:00:00+00:00",
        ),
    )
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_schema_call("m1")]),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    mcp = FakeMCPClient(scripted={"getTableSchema": [_schema_response()]})
    recorder = _Recorder()
    loop, _store = _loop(model=model, mcp=mcp, observer=recorder, store=store)

    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="schema?")

    # The sentinel really was rendered (otherwise this proves nothing) ...
    assert any(
        m.get("role") == "tool" and "result withheld" in str(m.get("content", ""))
        for m in model.calls[0].messages
    )
    # ... and the repeat was NOT treated as already-readable: it re-fetched.
    assert _dispatched(mcp, "getTableSchema") == 1
    assert recorder.payloads("loop_repeated_idempotent_read_guarded") == []
    assert len(recorder.payloads("loop_trimmed_read_refetch_allowed")) == 1


async def test_the_guards_own_nudge_never_counts_as_the_readable_result() -> None:
    """The second sentinel flavour, and the one that would self-perpetuate: the
    guard's own "you already have this" marker renders through the SAME sentinel
    path. If it counted as readable, one dedup would make every later repeat look
    satisfied forever — the marker vouching for itself."""
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_schema_call("m1")]),
            ModelTurnResult(tool_calls=[_schema_call("m2")]),  # guarded -> marker
            ModelTurnResult(tool_calls=[_schema_call("m3")]),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    mcp = FakeMCPClient(scripted={"getTableSchema": [_schema_response(), _schema_response()]})
    recorder = _Recorder()
    loop, store = _loop(model=model, mcp=mcp, observer=recorder)

    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="schema?")

    # Round 2 is deduped (m1 is readable) and its marker is rendered as a nudge...
    assert any(
        m.get("role") == "tool" and _REPEATED_IDEMPOTENT_READ_NUDGE in str(m.get("content", ""))
        for m in model.calls[2].messages
    )
    # ...and round 3 is ALSO deduped, because the pointer still names m1 — the
    # marker never became the pointer.
    assert _dispatched(mcp, "getTableSchema") == 1
    assert len(recorder.payloads("loop_repeated_idempotent_read_guarded")) == 2
    assert recorder.payloads("loop_trimmed_read_refetch_allowed") == []
    trail = await store.load_trail(SESSION_ID)
    assert [e.error_code for e in trail] == [
        None,
        "IDEMPOTENT_READ_ALREADY_SERVED",
        "IDEMPOTENT_READ_ALREADY_SERVED",
    ]


# ---------------------------------------------------------------------------
# The oscillation cap
# ---------------------------------------------------------------------------


async def test_the_refetch_exemption_is_capped_and_the_guard_resumes() -> None:
    """Fetched, trimmed, re-fetched, trimmed, re-fetched … is individually correct
    each time and in aggregate the exact waste the guard exists to prevent. Past the
    cap the budget has judged that bulk droppable twice over, so re-adding it cannot
    help and the cheap nudge is strictly better."""
    assert _MAX_TRIMMED_READ_REFETCHES == 2
    rounds = _MAX_TRIMMED_READ_REFETCHES + 3  # comfortably past the cap
    model = ScriptedModelClient(
        [ModelTurnResult(tool_calls=[_schema_call(f"m{n}")]) for n in range(rounds)]
        + [ModelTurnResult(assistant_text="done")]
    )
    mcp = FakeMCPClient(scripted={"getTableSchema": [_schema_response()] * rounds})
    recorder = _Recorder()
    loop, _store = _trimming_loop(model=model, mcp=mcp, observer=recorder)

    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="schema?")

    # 1 first fetch + exactly `_MAX_TRIMMED_READ_REFETCHES` exempted re-fetches.
    assert _dispatched(mcp, "getTableSchema") == 1 + _MAX_TRIMMED_READ_REFETCHES
    assert len(recorder.payloads("loop_trimmed_read_refetch_allowed")) == (
        _MAX_TRIMMED_READ_REFETCHES
    )
    # Beyond it the guard takes over again, and the cap is reported every time so a
    # thrashing turn is visible rather than merely quiet.
    capped = recorder.payloads("loop_trimmed_read_refetch_capped")
    assert len(capped) == rounds - 1 - _MAX_TRIMMED_READ_REFETCHES
    assert capped[0]["refetch_count"] == _MAX_TRIMMED_READ_REFETCHES
    assert capped[0]["deduped"] is False
    assert "thrashing" in capped[0]["note"]
    assert len(recorder.payloads("loop_repeated_idempotent_read_guarded")) == (
        rounds - 1 - _MAX_TRIMMED_READ_REFETCHES
    )


async def test_the_cap_is_per_signature_not_global() -> None:
    """Two different tables must not exhaust each other's allowance — the waste the
    cap bounds is re-reading THE SAME thing, and a per-loop counter would starve an
    unrelated read that had been trimmed once."""
    other = ToolCallRequest(
        id="p1", name="getTableSchema", arguments={"database": _DB, "table": "payroll"}
    )
    other2 = ToolCallRequest(
        id="p2", name="getTableSchema", arguments={"database": _DB, "table": "payroll"}
    )
    payroll = {"database": _DB, "table": "payroll", "columns": ["Amount"]}
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_schema_call("m1")]),
            ModelTurnResult(tool_calls=[_schema_call("m2")]),
            ModelTurnResult(tool_calls=[_schema_call("m3")]),
            ModelTurnResult(tool_calls=[_schema_call("m4")]),  # capped by now
            ModelTurnResult(tool_calls=[other]),
            ModelTurnResult(tool_calls=[other2]),  # still has its own allowance
            ModelTurnResult(assistant_text="done"),
        ]
    )
    mcp = FakeMCPClient(
        scripted={"getTableSchema": [_schema_response()] * 3 + [payroll] * 2}
    )
    recorder = _Recorder()
    loop, _store = _trimming_loop(model=model, mcp=mcp, observer=recorder)

    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="schemas?")

    tables = [c.args.get("table") for c in mcp.calls if c.tool_name == "getTableSchema"]
    # employee: 1 + 2 exemptions then capped. payroll: 1 + 1 exemption of its own.
    assert tables == ["employee", "employee", "employee", "payroll", "payroll"]


# ---------------------------------------------------------------------------
# Tool coverage — the predicate is a property of the CONTEXT, not of the tool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool_name", "arguments", "response"),
    [
        ("getTableSchema", {"database": _DB, "table": _TABLE}, {"columns": ["EmployeeCode"]}),
        ("listTables", {"database": _DB}, [{"name": _TABLE}]),
        ("explainQuery", {"sql": "SELECT 1"}, {"plan": "trivial"}),
    ],
)
async def test_the_exemption_covers_every_guarded_read_not_just_schemas(
    tool_name: str, arguments: dict[str, Any], response: Any
) -> None:
    """Uniform across `IDEMPOTENT_READ_TOOLS`. A per-tool carve-out would leave the
    prompt's re-fetch escape silently working for some reads and not others."""
    def call(call_id: str) -> ToolCallRequest:
        return ToolCallRequest(id=call_id, name=tool_name, arguments=dict(arguments))

    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[call("r1")]),
            ModelTurnResult(tool_calls=[call("r2")]),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    mcp = FakeMCPClient(scripted={tool_name: [response, response]})
    recorder = _Recorder()
    loop, _store = _trimming_loop(model=model, mcp=mcp, observer=recorder)

    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="look")

    assert _dispatched(mcp, tool_name) == 2
    assert len(recorder.payloads("loop_trimmed_read_refetch_allowed")) == 1


async def test_explain_query_sql_never_reaches_the_exemption_event() -> None:
    """D25, on the NEW event as well as the guard's. `explainQuery`'s `sql` can carry
    PII literals, so it identifies as the empty target rather than by its text — a
    less specific span is the right trade against a query literal in the backend."""
    pii = "SELECT * FROM employee WHERE last_name = 'Rasmussen'"
    def call(call_id: str) -> ToolCallRequest:
        return ToolCallRequest(id=call_id, name="explainQuery", arguments={"sql": pii})

    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[call("e1")]),
            ModelTurnResult(tool_calls=[call("e2")]),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    mcp = FakeMCPClient(scripted={"explainQuery": [{"plan": "x"}, {"plan": "x"}]})
    recorder = _Recorder()
    loop, _store = _trimming_loop(model=model, mcp=mcp, observer=recorder)

    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="explain")

    payload = recorder.payloads("loop_trimmed_read_refetch_allowed")[0]
    assert "Rasmussen" not in str(payload)
    assert "sql" not in payload
    assert payload["dedup_target"] == ""


# ---------------------------------------------------------------------------
# Emulated discovery keeps its saving
# ---------------------------------------------------------------------------


async def test_emulated_discovery_pairs_are_readable_so_a_re_call_is_still_deduped() -> None:
    """The sweep exists so the model does NOT re-dispatch `listDatabases`/
    `listTables`. Its pairs carry no trail entry, so the exemption would find "no
    readable source" and re-fetch — undoing the saving — unless the loop points
    those signatures at the synthetic entries. `fit_request_to_budget` pins the pairs
    (invariant 7), so they stay readable for the whole window."""
    from data_agent.runtime.context.discovery_emulation import EmulatedDiscovery
    from data_agent.runtime.loop.read_guard import idempotent_read_signature

    entry = {
        "tool_call_id": "emulated-listTables-dbpcm_warehouse",
        "tool_name": "listTables",
        "args": {"database": _DB},
        "status": "ok",
        "error_code": None,
        "user_message": None,
        "result_preview": {
            "columns": ["name"],
            "rows": [[_TABLE]],
            "row_count": 1,
            "truncated": False,
        },
    }
    emulation = EmulatedDiscovery(
        entries=[entry],
        read_signatures={idempotent_read_signature("listTables", {"database": _DB})},
    )

    async def _provider(_c: RuntimeCredentials) -> EmulatedDiscovery:
        return emulation

    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="lt1", name="listTables", arguments={"database": _DB})
                ]
            ),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    mcp = FakeMCPClient(scripted={"listTables": [[{"name": _TABLE}]]})
    recorder = _Recorder()
    store = InMemorySessionStore()
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(mcp, CATALOG),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=20,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=recorder,
        discovery_emulation_provider=_provider,
    )

    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="tables?")

    assert _dispatched(mcp, "listTables") == 0
    assert len(recorder.payloads("loop_repeated_idempotent_read_guarded")) == 1
    assert recorder.payloads("loop_trimmed_read_refetch_allowed") == []


async def test_every_exemption_payload_key_survives_the_guardrail_allowlist() -> None:
    """`guardrail_observer` drops any payload key not on
    `_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST`, so an unlisted key reaches Phoenix as a
    correctly-named span carrying nothing. The capped event is the one worth
    alerting on — a turn thrashing against the budget — and it is useless without
    `refetch_count`/`reason`."""
    from data_agent.runtime.loop.read_guard import _trimmed_read_refetch_event
    from data_agent.runtime.observability.tracing import _GUARDRAIL_OBSERVER_ATTR_ALLOWLIST

    payload = _trimmed_read_refetch_event(
        "getTableSchema",
        {"database": _DB, "table": _TABLE},
        granted=_MAX_TRIMMED_READ_REFETCHES,
        reason="result_not_readable_in_window",
    )
    missing = sorted(k for k in payload if k not in _GUARDRAIL_OBSERVER_ATTR_ALLOWLIST)
    assert not missing, f"payload keys dropped by the allowlist: {missing}"
