"""Known-permitted evidence manufacture — is it actually MEASURED? (04 §B.4, 03 §C.4)

The README states these survive Release 1 by design, and that the mitigation is
*measurement*, not a structural rule. That makes an UNMEASURED one a real defect:
a hole nobody can see in the telemetry is indistinguishable from a hole nobody
knows about.

Three routes, none needing warehouse access or knowledge of the user's scope:

  1. complete three intents citing ONE `runQuery`;
  2. `SELECT … WHERE 1=0` ⇒ `row_count == 0` ⇒ `REQUIRED_DATA_UNAVAILABLE`;
  3. `getTableSchema(<scratch_db>, <anything>)` ⇒ `SCRATCH_SESSION_VIOLATION` ⇒
     `NO_ACCESS`, for ONE metadata call that does not even lock the late-init
     boundary.

Route 2 is measured by the `loop_zero_row_block` / `loop_zero_row_completion`
pair. Route 1 is measured by `loop_evidence_reused` — with a counting gap proved
below. Route 3, the CHEAPEST of the three, emitted NOTHING that identified how
the denial was obtained until `loop_intent_blocked{evidence_tool_name}` was added
(it was a strict `xfail` here first); both halves of that comparison — the
scratch probe and an earned blueprint denial — are asserted below.

Also here: the zero-row ambiguity on a `runBlueprint`, which is the shape that
fires on honest work in a blueprint-first release (the live warehouse has NULL
`most_recent_hire_date`, so every hires blueprint returns zero rows).
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import (
    SUBSTANTIVE_TOOLS,
    UpdateAnalysisStateTool,
    find_locking_tool,
    validate_block_evidence,
    validate_completion_evidence,
)
from data_agent.runtime.loop.agent_loop import TurnContext
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import ResultPreview, TrailEntry, live_analysis_state

SESSION_ID = "sess-manufacture-qa"
TURN = 3


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, payload))

    def named(self, event: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.events if name == event]


async def _entry(
    store: InMemorySessionStore,
    tool_call_id: str,
    tool_name: str,
    *,
    status: str = "ok",
    error_code: str | None = None,
    row_count: int | None = 1,
    authoritative: bool = False,
    args: dict[str, Any] | None = None,
) -> TrailEntry:
    preview = (
        None
        if row_count is None
        else ResultPreview(columns=["x"], row_count=row_count, truncated=False, preview_rows=[])
    )
    entry = TrailEntry(
        turn_index=TURN,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        args=args or {},
        status=status,
        error_code=error_code,
        provenance=frozenset(),
        result_preview=preview,
        result_full_ref=None,
        ts="2026-08-11T00:00:00+00:00",
        authoritative=authoritative,
    )
    await store.append_trail_entry(SESSION_ID, entry)
    return entry


async def _initialized(
    store: InMemorySessionStore, observer: _Recorder, *descriptions: str
) -> UpdateAnalysisStateTool:
    tool = UpdateAnalysisStateTool(session_store=store, observer=observer)
    result = await tool.run(
        {"intents": [{"description": d} for d in descriptions]},
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )
    assert result.status == "ok", result
    return tool


async def _update(tool: UpdateAnalysisStateTool, *updates: dict[str, Any]):
    result = await tool.run(
        {"intents": list(updates)}, _credentials(), turn=TurnContext(turn_index=TURN)
    )
    assert result.status == "ok", result.denial_detail
    return result


# ---------------------------------------------------------------------------
# Route 1 — one query, three completions
# ---------------------------------------------------------------------------


async def test_one_query_completing_three_intents_is_permitted_and_all_three_are_flagged() -> None:
    """PERMITTED BY DESIGN (04 §A): one query genuinely answers "headcount and
    average salary by department", so completion reuse is telemetry-flagged, not
    refused. When all three land in ONE call every one of them is flagged, so the
    reuse is fully visible."""
    store = InMemorySessionStore()
    observer = _Recorder()
    # Declared FIRST — 03 §E.2 dispatches state calls before everything else, and
    # a `runQuery` already on the trail would lock the late-init boundary.
    tool = await _initialized(store, observer, "one", "two", "three")
    await _entry(store, "q1", "runQuery", args={"sql": "SELECT 1"})

    await _update(
        tool,
        {"intent_id": "i1", "status": "completed", "evidence_tool_call_id": "q1"},
        {"intent_id": "i2", "status": "completed", "evidence_tool_call_id": "q1"},
        {"intent_id": "i3", "status": "completed", "evidence_tool_call_id": "q1"},
    )

    state = live_analysis_state(await store.get_or_create_session(SESSION_ID), TURN)
    assert [i.status for i in state.intents] == ["completed"] * 3
    assert observer.named("loop_evidence_reused") == [
        {"intent_id": "i1", "tool_call_id": "q1"},
        {"intent_id": "i2", "tool_call_id": "q1"},
        {"intent_id": "i3", "tool_call_id": "q1"},
    ]


async def test_reuse_split_across_calls_undercounts_by_the_first_intent() -> None:
    """MEASURED GAP, not a guarantee failure. `_emit_transition_events` skips an
    intent whose status did not CHANGE in this call, and it computes the owner set
    from the merged state — so when the same evidence is spent across two calls,
    the intent completed FIRST is never flagged: at its own call it was the only
    owner, and at the second call it is unchanged and skipped.

    The fact of reuse is still visible (the later intent is flagged), so the ratio
    07 reads is not blind — but any per-intent count derived from
    `loop_evidence_reused` understates the group by exactly one. Asserted so the
    number is trusted for what it is."""
    store = InMemorySessionStore()
    observer = _Recorder()
    tool = await _initialized(store, observer, "one", "two")
    await _entry(store, "q1", "runQuery", args={"sql": "SELECT 1"})

    await _update(tool, {"intent_id": "i1", "status": "completed", "evidence_tool_call_id": "q1"})
    await _update(tool, {"intent_id": "i2", "status": "completed", "evidence_tool_call_id": "q1"})

    state = live_analysis_state(await store.get_or_create_session(SESSION_ID), TURN)
    # Both intents ARE closed on the same evidence...
    assert {i.evidence_tool_call_id for i in state.intents} == {"q1"}
    # ...and only the second is flagged.
    assert observer.named("loop_evidence_reused") == [{"intent_id": "i2", "tool_call_id": "q1"}]


# ---------------------------------------------------------------------------
# Route 3 — NO_ACCESS for one metadata call
# ---------------------------------------------------------------------------


async def test_a_scratch_metadata_probe_blocks_an_intent_and_does_not_lock_late_init() -> None:
    """THE CHEAPEST ROUTE, end-to-end through the tool rather than the validator
    alone. `getTableSchema(<scratch_db>, <anything>)` fails closed with
    `SCRATCH_SESSION_VIOLATION` — no data touched, no knowledge of the caller's
    scope needed — and `getTableSchema` is deliberately NOT in `SUBSTANTIVE_TOOLS`,
    so the probe can be fired BEFORE declaring intents and the boundary still
    permits initialization afterwards. Both halves asserted, because it is the
    combination that makes the route free."""
    store = InMemorySessionStore()
    observer = _Recorder()
    await _entry(
        store,
        "m1",
        "getTableSchema",
        status="denied",
        error_code="SCRATCH_SESSION_VIOLATION",
        row_count=None,
        args={"database": "scratch_someone_else", "table": "anything"},
    )

    # The boundary is untouched: a metadata probe does not lock late init.
    assert "getTableSchema" not in SUBSTANTIVE_TOOLS
    assert find_locking_tool(await store.load_trail(SESSION_ID), TURN) is None

    tool = await _initialized(store, observer, "something hard")
    await _update(
        tool,
        {
            "intent_id": "i1",
            "status": "blocked",
            "reason_code": "NO_ACCESS",
            "evidence_tool_call_id": "m1",
        },
    )

    state = live_analysis_state(await store.get_or_create_session(SESSION_ID), TURN)
    assert [(i.status, i.reason_code) for i in state.intents] == [("blocked", "NO_ACCESS")]
    # The ledger records a falsifiable disposition — which is exactly what the
    # contract promises, and exactly why the SEMANTIC claim is not guaranteed.
    assert state.intents[0].evidence_tool_call_id == "m1"


async def test_a_manufactured_block_identifies_its_evidence_tool_in_telemetry() -> None:
    """Was a strict `xfail`: a model-declared BLOCK emitted only
    `loop_analysis_state_transition{intent_id, from_status, to_status,
    reason_code}`, so the cheapest manufacture route in the release —
    `getTableSchema(<scratch_db>, <anything>)` => `SCRATCH_SESSION_VIOLATION` =>
    `NO_ACCESS` — was telemetrically IDENTICAL to a denial earned by a
    `runBlueprint` that hit `COLUMN_SCOPE_VIOLATION` on the user's own data. The
    release keeps these holes open on the explicit basis that they are MEASURED,
    so an unmeasurable one was unmitigated.

    `loop_intent_blocked` now carries `evidence_tool_name`, mirroring
    `loop_intent_completed`."""
    store = InMemorySessionStore()
    observer = _Recorder()
    await _entry(
        store,
        "m1",
        "getTableSchema",
        status="denied",
        error_code="SCRATCH_SESSION_VIOLATION",
        row_count=None,
        args={"database": "scratch_someone_else", "table": "anything"},
    )
    tool = await _initialized(store, observer, "something hard")
    await _update(
        tool,
        {
            "intent_id": "i1",
            "status": "blocked",
            "reason_code": "NO_ACCESS",
            "evidence_tool_call_id": "m1",
        },
    )

    identifying = [
        payload
        for _name, payload in observer.events
        if payload.get("evidence_tool_name") == "getTableSchema"
    ]
    assert identifying, (
        "no event identifies the tool that produced this block's evidence, so a "
        "metadata-probe NO_ACCESS is indistinguishable from an earned one"
    )
    assert observer.named("loop_intent_blocked") == [
        {
            "intent_id": "i1",
            "reason_code": "NO_ACCESS",
            "evidence_tool_name": "getTableSchema",
        }
    ]
    # D25: the evidence's tool_call_id is model-supplied and is NOT emitted.
    assert all("evidence_tool_call_id" not in p for _n, p in observer.events)


async def test_an_earned_denial_is_now_distinguishable_from_the_metadata_probe() -> None:
    """The other half — the comparison the counter exists to support. Same
    `NO_ACCESS` reason, same ledger entry; only `evidence_tool_name` separates a
    blueprint that hit `COLUMN_SCOPE_VIOLATION` on the user's own data from a
    scratch-database probe."""
    store = InMemorySessionStore()
    observer = _Recorder()
    tool = await _initialized(store, observer, "something hard")
    await _entry(
        store,
        "bp1",
        "runBlueprint",
        status="error",  # a blueprint propagates an access denial as `error`
        error_code="COLUMN_SCOPE_VIOLATION",
        row_count=None,
    )
    await _update(
        tool,
        {
            "intent_id": "i1",
            "status": "blocked",
            "reason_code": "NO_ACCESS",
            "evidence_tool_call_id": "bp1",
        },
    )

    assert observer.named("loop_intent_blocked") == [
        {"intent_id": "i1", "reason_code": "NO_ACCESS", "evidence_tool_name": "runBlueprint"}
    ]


# ---------------------------------------------------------------------------
# The zero-row ambiguity, on the blueprint path
# ---------------------------------------------------------------------------


async def test_a_zero_row_authoritative_blueprint_is_valid_for_both_dispositions() -> None:
    """Target of the whole ambiguity: `ok` + `authoritative` + `row_count == 0` is
    simultaneously valid COMPLETION evidence ("nobody") and valid BLOCK evidence
    (`REQUIRED_DATA_UNAVAILABLE`). Both are asserted directly against the
    validators — one entry, two legal readings — and then the pair of counters is
    shown to be what distinguishes them."""
    store = InMemorySessionStore()
    entry = await _entry(store, "bp1", "runBlueprint", row_count=0, authoritative=True)
    trail = [entry]

    assert validate_completion_evidence("bp1", trail, TURN) is None
    assert validate_block_evidence("bp1", "REQUIRED_DATA_UNAVAILABLE", trail, TURN) is None


async def test_the_zero_row_pair_distinguishes_the_two_readings_on_a_blueprint() -> None:
    """The blueprint variant of the ratio. It matters more than the `runQuery` one
    in a BLUEPRINT-FIRST release: the live warehouse has NULL
    `most_recent_hire_date`, so every hires blueprint returns zero rows on honest
    work — and `blocked` is the cheaper disposition for the model (no prose, no
    table, no `answerWithTable`). If the two counters ever collapsed into one, the
    skew this exists to detect would become invisible."""
    store = InMemorySessionStore()
    observer = _Recorder()
    tool = await _initialized(store, observer, "hires last month", "leavers last month")
    await _entry(store, "bp1", "runBlueprint", row_count=0, authoritative=True)
    await _entry(store, "bp2", "runBlueprint", row_count=0, authoritative=True)

    await _update(
        tool,
        {"intent_id": "i1", "status": "completed", "evidence_tool_call_id": "bp1"},
        {
            "intent_id": "i2",
            "status": "blocked",
            "reason_code": "REQUIRED_DATA_UNAVAILABLE",
            "evidence_tool_call_id": "bp2",
        },
    )

    assert observer.named("loop_zero_row_completion") == [{"intent_id": "i1"}]
    assert observer.named("loop_zero_row_block") == [{"intent_id": "i2"}]
    # The completion's route stays derivable — a zero-row answer is still a
    # blueprint answer, not a fallback to ad-hoc SQL.
    assert observer.named("loop_intent_completed") == [
        {"intent_id": "i1", "evidence_tool_name": "runBlueprint"}
    ]


async def test_a_zero_row_blueprint_that_failed_verification_completes_nothing() -> None:
    """The other half of the blueprint asymmetry: a blueprint can return `ok` with
    an unclean verify block, so `authoritative` is condition 4 and NOT implied by
    condition 2. A zero-row UNVERIFIED blueprint is still valid BLOCK evidence
    (the block predicate does not read `authoritative`) — which is the sharper
    version of the ambiguity: the cheaper disposition is available on strictly
    weaker evidence."""
    store = InMemorySessionStore()
    entry = await _entry(store, "bp1", "runBlueprint", row_count=0, authoritative=False)
    trail = [entry]

    completion = validate_completion_evidence("bp1", trail, TURN)
    assert completion is not None and "verification" in completion
    assert validate_block_evidence("bp1", "REQUIRED_DATA_UNAVAILABLE", trail, TURN) is None
