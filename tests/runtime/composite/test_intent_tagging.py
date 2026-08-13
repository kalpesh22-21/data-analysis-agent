"""Call-time intent tagging — the tool half (Release 1, 03/04 as amended).

WHY THIS EXISTS. Release 1 shipped completion-by-citation: mark an intent
`completed` and cite the `tool_call_id` of a qualifying earlier call. Against a
live model that failed essentially always — 9 attempts, 0 successes across two
sessions, with the model reaching for the blueprint id, the tool NAME, a
hallucinated `call_UUGw4Mtd…` and finally `""`, while the real ids sat in its
context the whole time. One session burned 5 of its 10 tool calls on rejected
completions and never answered.

So the binding moved to the moment the work is dispatched: the model tags the
call (`serves_intent="i2"`), and closes the intent with `{intent_id, status}`
alone. These tests cover the RESOLUTION half — that the tag closes an intent and
that every one of 04's conditions still binds through it.

Citation was RETIRED outright on 2026-08-12 (01a §14) after a further count of the
same measurement: 0 successes, ever. The one thing it was kept for — ONE call
answering TWO intents, which 04 §A deliberately permits — is now the AUTO-BIND
BACKSTOP's rule 2, and the backstop's three rules have their own section at the
bottom of this file.

Layer 1: `InMemorySessionStore`, no loop, no model — the tool driven directly with
an explicit `TurnContext`, exactly as the loop drives it.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import (
    ANALYSIS_STATE_INVALID_CODE,
    INTENT_TAGGABLE_TOOLS,
    UpdateAnalysisStateTool,
    split_serves_intent,
)
from data_agent.runtime.context.assembly import IDEMPOTENT_READ_ALREADY_SERVED_CODE
from data_agent.runtime.loop.agent_loop import TurnContext
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import (
    AnalysisState,
    ResultPreview,
    TrackedIntent,
    TrailEntry,
    live_analysis_state,
)

SESSION_ID = "sess-intent-tagging"
TURN = 1


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, dict(payload)))

    def named(self, event: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.events if name == event]


async def _entry(
    store: InMemorySessionStore,
    tool_call_id: str,
    tool_name: str,
    *,
    serves_intent: str | None = None,
    status: str = "ok",
    error_code: str | None = None,
    row_count: int | None = 1,
    authoritative: bool = False,
    turn_index: int = TURN,
    args: dict[str, Any] | None = None,
) -> None:
    preview = (
        None
        if row_count is None
        else ResultPreview(columns=["x"], row_count=row_count, truncated=False, preview_rows=[])
    )
    await store.append_trail_entry(
        SESSION_ID,
        TrailEntry(
            turn_index=turn_index,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args=args or {},
            status=status,
            error_code=error_code,
            provenance=frozenset(),
            result_preview=preview,
            result_full_ref=None,
            ts="2026-08-12T00:00:00+00:00",
            authoritative=authoritative,
            serves_intent=serves_intent,
        ),
    )


async def _initialized(
    store: InMemorySessionStore, *descriptions: str, observer: _Recorder | None = None
) -> UpdateAnalysisStateTool:
    tool = UpdateAnalysisStateTool(
        session_store=store, observer=observer or _Recorder()
    )
    await tool.run(
        {"intents": [{"description": d} for d in descriptions]},
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )
    return tool


async def _update(tool: UpdateAnalysisStateTool, *updates: dict[str, Any]):
    return await tool.run(
        {"intents": list(updates)}, _credentials(), turn=TurnContext(turn_index=TURN)
    )


async def _intents(store: InMemorySessionStore) -> dict[str, TrackedIntent]:
    state = live_analysis_state(await store.get_or_create_session(SESSION_ID), TURN)
    assert state is not None
    return {intent.intent_id: intent for intent in state.intents}


# ---------------------------------------------------------------------------
# The split at dispatch — pure, no store
# ---------------------------------------------------------------------------


def _live(*ids: str) -> AnalysisState:
    return AnalysisState(
        turn_index=TURN,
        intents=tuple(
            TrackedIntent(intent_id=i, description=f"d{i}", status="pending") for i in ids
        ),
    )


def test_the_tag_is_stripped_from_the_arguments_of_every_taggable_tool() -> None:
    """⚠ The load-bearing half. `runQuery`/`getTableSchema` are dispatched to the
    live MCP, which rejects an argument its own schema does not declare, and
    `runBlueprint`'s executor validates its arguments too."""
    for tool_name in sorted(INTENT_TAGGABLE_TOOLS):
        args, tag, dropped = split_serves_intent(
            tool_name, {"sql": "SELECT 1", "serves_intent": "i1"}, _live("i1")
        )
        assert args == {"sql": "SELECT 1"}, tool_name
        assert tag == "i1"
        assert dropped is None


def test_a_tag_on_a_non_taggable_tool_is_left_exactly_where_the_model_put_it() -> None:
    """`sampleRows`/`resolveValues` are never valid evidence, so the parameter is
    not advertised for them and nothing here silently accepts it: the arguments
    pass through untouched and fail exactly as they do today."""
    args, tag, dropped = split_serves_intent(
        "sampleRows", {"table": "employee", "serves_intent": "i1"}, _live("i1")
    )
    assert args == {"table": "employee", "serves_intent": "i1"}
    assert tag is None and dropped is None


def test_an_unknown_or_stale_tag_is_dropped_and_reported_never_fatal() -> None:
    """Degrade-not-fail, never silently: the arguments still come back CLEAN so the
    work dispatches, and the caller is told why the tag went away. Refusing real
    work over a bookkeeping typo is strictly worse than an untagged entry."""
    args, tag, dropped = split_serves_intent(
        "runQuery", {"sql": "SELECT 1", "serves_intent": "i9"}, _live("i1")
    )
    assert args == {"sql": "SELECT 1"}
    assert tag is None
    assert dropped == "unknown_intent_id"

    # No state at all (a single-deliverable turn that tagged anyway).
    _args, tag, dropped = split_serves_intent(
        "runQuery", {"sql": "SELECT 1", "serves_intent": "i1"}, None
    )
    assert tag is None and dropped == "no_live_state"

    # A non-string value is a drop, reported by RULE NAME (the value is arbitrary
    # model text and never reaches telemetry).
    _args, tag, dropped = split_serves_intent(
        "runQuery", {"sql": "SELECT 1", "serves_intent": {"id": "i1"}}, _live("i1")
    )
    assert tag is None and dropped == "not_a_string"


def test_an_empty_tag_is_absent_not_a_drop() -> None:
    """The model cannot omit keys — it fills unused ones with `""`. That is the
    same "a key carrying no information is absent" rule the state payload uses, so
    it is not reported as a failure."""
    for empty in ("", "   ", None):
        args, tag, dropped = split_serves_intent(
            "runQuery", {"sql": "SELECT 1", "serves_intent": empty}, _live("i1")
        )
        assert args == {"sql": "SELECT 1"}
        assert tag is None and dropped is None


# ---------------------------------------------------------------------------
# Resolution at completion
# ---------------------------------------------------------------------------


async def test_completion_with_no_evidence_field_resolves_from_the_tag() -> None:
    """The primary path, end to end through the tool: the model sends
    `{intent_id, status}` and nothing else."""
    store = InMemorySessionStore()
    observer = _Recorder()
    tool = await _initialized(store, "headcount", observer=observer)
    await _entry(store, "call_q1", "runQuery", serves_intent="i1")

    result = await _update(tool, {"intent_id": "i1", "status": "completed"})

    assert result.status == "ok"
    intents = await _intents(store)
    assert intents["i1"].status == "completed"
    # The runtime resolved the opaque id the model never had to copy.
    assert intents["i1"].evidence_tool_call_id == "call_q1"
    assert observer.named("loop_intent_completed") == [
        {
            "intent_id": "i1",
            "evidence_tool_name": "runQuery",
            "evidence_binding": "tagged",
        }
    ]


async def test_completion_with_no_qualifying_call_at_all_is_refused() -> None:
    """The evidence obligation did not go away, it MOVED: it used to be a payload
    rule ("`completed` requires a non-empty string"), and is now a trail rule
    ("some qualifying call must exist for this intent"). The message has to name
    the fix, because a refusal the model cannot act on is what this whole change
    exists to stop.

    The trail here holds a `searchBlueprints` — a call that RAN and is not evidence
    of anything, so it is neither a tagged candidate nor an auto-bind one. That is
    the shape worth testing: an empty trail would pass for the wrong reason.
    """
    store = InMemorySessionStore()
    observer = _Recorder()
    tool = await _initialized(store, "headcount", observer=observer)
    await _entry(store, "call_s1", "searchBlueprints")

    result = await _update(tool, {"intent_id": "i1", "status": "completed"})

    assert result.status == "error"
    assert result.error_code == ANALYSIS_STATE_INVALID_CODE
    assert result.retryable is True
    assert "no call on this turn is tagged for it" in result.denial_detail
    assert "serves_intent='i1'" in result.denial_detail
    assert "NEXT message" in result.denial_detail
    assert observer.named("loop_analysis_state_rejected") == [
        {"reason": "unresolved_evidence", "intent_count": 1}
    ]
    assert (await _intents(store))["i1"].status == "pending"


async def test_a_tagged_unverified_blueprint_is_not_completion_evidence() -> None:
    """04 condition 4 still binds through the tag. A blueprint can return `ok` with
    an unclean verify block, so `status` alone does not establish an authoritative
    answer — and the tagged path must not become the weaker one."""
    store = InMemorySessionStore()
    tool = await _initialized(store, "headcount")
    await _entry(store, "call_b1", "runBlueprint", serves_intent="i1", authoritative=False)

    result = await _update(tool, {"intent_id": "i1", "status": "completed"})

    assert result.status == "error"
    assert "did not pass verification" in result.denial_detail
    assert (await _intents(store))["i1"].status == "pending"


async def test_a_tagged_dedup_guarded_read_is_refused_and_names_the_original() -> None:
    """04 condition 5. The idempotent-read guard persists an `ok`, data-free entry
    that passes every other condition having fetched NOTHING. Tagging it must not
    launder it — and since the tag IS recorded on the guard entry, the refusal can
    still name the call that actually served the result."""
    store = InMemorySessionStore()
    tool = await _initialized(store, "the employee schema")
    schema_args = {"database": "dbpcm_warehouse", "table": "employee"}
    await _entry(store, "call_s1", "getTableSchema", args=schema_args)
    await _entry(
        store,
        "call_s2",
        "getTableSchema",
        serves_intent="i1",
        args=schema_args,
        error_code=IDEMPOTENT_READ_ALREADY_SERVED_CODE,
        row_count=None,
    )

    result = await _update(tool, {"intent_id": "i1", "status": "completed"})

    assert result.status == "error"
    assert "duplicate read" in result.denial_detail
    assert "call_s1" in result.denial_detail, "the original that served it is not named"


async def test_the_most_recent_qualifying_tagged_call_wins() -> None:
    """A retry supersedes the attempt before it — but "most recent" means most
    recent QUALIFYING call, so a later failed attempt does not strand an intent
    whose earlier work succeeded."""
    store = InMemorySessionStore()
    tool = await _initialized(store, "headcount")
    await _entry(store, "call_q1", "runQuery", serves_intent="i1")
    await _entry(store, "call_q2", "runQuery", serves_intent="i1")
    await _update(tool, {"intent_id": "i1", "status": "completed"})
    assert (await _intents(store))["i1"].evidence_tool_call_id == "call_q2"

    store2 = InMemorySessionStore()
    tool2 = await _initialized(store2, "headcount")
    await _entry(store2, "call_ok", "runQuery", serves_intent="i1")
    await _entry(
        store2,
        "call_bad",
        "runQuery",
        serves_intent="i1",
        status="denied",
        error_code="COLUMN_SCOPE_VIOLATION",
        row_count=None,
    )
    await tool2.run(
        {"intents": [{"intent_id": "i1", "status": "completed"}]},
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )
    state = live_analysis_state(await store2.get_or_create_session(SESSION_ID), TURN)
    assert state.intents[0].evidence_tool_call_id == "call_ok"


async def test_a_tag_from_another_turn_is_not_evidence_for_this_one() -> None:
    """The turn gate is the same one `live_analysis_state` and 04's validators
    apply: evidence must come from the turn being enforced."""
    store = InMemorySessionStore()
    tool = await _initialized(store, "headcount")
    await _entry(store, "call_old", "runQuery", serves_intent="i1", turn_index=TURN - 1)

    result = await _update(tool, {"intent_id": "i1", "status": "completed"})
    assert result.status == "error"
    assert "no call on this turn is tagged for it" in result.denial_detail


# ---------------------------------------------------------------------------
# Blocking through the tag — and 04 §B.3's asymmetry
# ---------------------------------------------------------------------------


async def test_a_block_resolves_from_the_tag_on_the_failed_call() -> None:
    store = InMemorySessionStore()
    observer = _Recorder()
    tool = await _initialized(store, "salaries", observer=observer)
    await _entry(
        store,
        "call_denied",
        "runQuery",
        serves_intent="i1",
        status="denied",
        error_code="COLUMN_SCOPE_VIOLATION",
        row_count=None,
    )

    result = await _update(tool, {"intent_id": "i1", "status": "blocked"})

    assert result.status == "ok"
    intents = await _intents(store)
    # The reason was DERIVED from the tagged call, not declared: the entry was
    # refused with an access code, so it can only be NO_ACCESS.
    assert (intents["i1"].status, intents["i1"].reason_code) == ("blocked", "NO_ACCESS")
    assert intents["i1"].evidence_tool_call_id == "call_denied"
    assert observer.named("loop_intent_blocked") == [
        {
            "intent_id": "i1",
            "reason_code": "NO_ACCESS",
            "evidence_tool_name": "runQuery",
            "evidence_binding": "tagged",
        }
    ]


async def test_block_distinctness_survives_resolution_from_a_tag() -> None:
    """04 §B.3 — one denial does not block a different deliverable — is checked over
    the MERGED state, AFTER the bindings have resolved. So it does not care which
    path produced each: an intent whose block resolved from a tag still SPENDS that
    `tool_call_id`, and a second intent that reaches the same id through the
    auto-bind backstop is refused.

    This is the shape that matters now. A pure-tag collision is not reachable — one
    trail entry carries ONE tag and the runtime mints one entry per dispatched call
    — but the BACKSTOP can hand the same denial to every untagged blocked intent in
    the batch (its rule 1 sees one untagged candidate for each of them), which is
    exactly 04 §B.4's O(1) bulk-block. It is refused here, over the merged state,
    which is why the backstop does not need to know about the other intents.
    """
    store = InMemorySessionStore()
    observer = _Recorder()
    tool = await _initialized(store, "a", "b", observer=observer)
    await _entry(
        store,
        "call_scratch",
        "getTableSchema",
        serves_intent="i1",
        status="denied",
        error_code="SCRATCH_SESSION_VIOLATION",
        row_count=None,
    )

    result = await _update(
        tool,
        {"intent_id": "i1", "status": "blocked"},  # from the tag
        {"intent_id": "i2", "status": "blocked"},  # ...and from the backstop, same id
    )

    assert result.status == "error"
    assert "already the evidence" in result.denial_detail
    assert {"reason": "block_evidence_reused", "intent_count": 2} in observer.named(
        "loop_analysis_state_rejected"
    )
    assert all(i.status == "pending" for i in (await _intents(store)).values())

    # ...and the same holds ACROSS calls, since the check runs over the merged
    # state: i1's block lands, then i2 cannot spend the id it already used.
    ok = await _update(tool, {"intent_id": "i1", "status": "blocked"})
    assert ok.status == "ok"
    second = await _update(tool, {"intent_id": "i2", "status": "blocked"})
    assert second.status == "error"
    assert "already the evidence" in second.denial_detail


# ---------------------------------------------------------------------------
# The auto-bind backstop (01a §14) — what replaced the citation path
# ---------------------------------------------------------------------------


async def test_rule_1_binds_the_one_untagged_call_and_says_so() -> None:
    """RULE 1. The model did the work and forgot the tag — the single most likely
    failure, and one it CANNOT repair: a call that already ran cannot be
    retro-tagged, so refusing here costs the user the answer rather than teaching
    the model anything.

    The bind is announced. `loop_analysis_state_auto_bound` is the counter that keeps
    the backstop honest: a high rate means the tag is not landing, not that the
    backstop is working well, and without the event the two look identical.
    """
    store = InMemorySessionStore()
    observer = _Recorder()
    tool = await _initialized(store, "headcount", observer=observer)
    await _entry(store, "call_q1", "runQuery")  # ran, untagged

    result = await _update(tool, {"intent_id": "i1", "status": "completed"})

    assert result.status == "ok", result.denial_detail
    intents = await _intents(store)
    assert (intents["i1"].status, intents["i1"].evidence_tool_call_id) == (
        "completed",
        "call_q1",
    )
    assert observer.named("loop_analysis_state_auto_bound") == [{"intent_id": "i1"}]
    assert observer.named("loop_intent_completed") == [
        {
            "intent_id": "i1",
            "evidence_tool_name": "runQuery",
            "evidence_binding": "auto_bound",
        }
    ]


async def test_rule_1_prefers_the_untagged_call_over_one_tagged_elsewhere() -> None:
    """RULE 1 BEFORE RULE 2, and this is the case that proves the order matters.

    Two candidates exist, one of them already tagged for ANOTHER intent. Rule 2
    alone would see two candidates and refuse; rule 1 sees exactly one UNTAGGED and
    binds it — which is also the right answer semantically, since the tagged one
    has an owner and this one does not.
    """
    store = InMemorySessionStore()
    observer = _Recorder()
    tool = await _initialized(store, "headcount", "salary", observer=observer)
    await _entry(store, "call_q1", "runQuery", serves_intent="i1")
    await _entry(store, "call_q2", "runQuery")  # untagged

    result = await _update(tool, {"intent_id": "i2", "status": "completed"})

    assert result.status == "ok", result.denial_detail
    assert (await _intents(store))["i2"].evidence_tool_call_id == "call_q2"
    assert observer.named("loop_analysis_state_auto_bound") == [{"intent_id": "i2"}]


async def test_rule_2_lets_one_call_close_two_deliverables() -> None:
    """RULE 2, and the whole reason the citation path could be retired.

    04 §A ALLOWS evidence reuse for completion — one query genuinely answers
    "headcount and average salary by department" — and a single-valued tag cannot
    express it. Citation used to be the only way; it never once worked live. Now
    the second intent has NO untagged candidate (the single call belongs to i1), so
    rule 2 binds the same id to both.

    `loop_evidence_reused` — 04 §A's stated mitigation for permitting reuse at all
    — must still fire for both intents, whichever path established each binding.
    """
    store = InMemorySessionStore()
    observer = _Recorder()
    tool = await _initialized(store, "headcount", "average salary", observer=observer)
    await _entry(store, "call_q1", "runQuery", serves_intent="i1")

    result = await _update(
        tool,
        {"intent_id": "i1", "status": "completed"},  # from the tag
        {"intent_id": "i2", "status": "completed"},  # ...and from rule 2
    )

    assert result.status == "ok", result.denial_detail
    intents = await _intents(store)
    assert [i.status for i in intents.values()] == ["completed", "completed"]
    assert {i.intent_id: i.evidence_tool_call_id for i in intents.values()} == {
        "i1": "call_q1",
        "i2": "call_q1",
    }
    assert {
        payload["intent_id"]: payload["evidence_binding"]
        for payload in observer.named("loop_intent_completed")
    } == {"i1": "tagged", "i2": "auto_bound"}
    assert observer.named("loop_analysis_state_auto_bound") == [{"intent_id": "i2"}]
    assert observer.named("loop_evidence_reused") == [
        {"intent_id": "i1", "tool_call_id": "call_q1"},
        {"intent_id": "i2", "tool_call_id": "call_q1"},
    ]


async def test_rule_3_refuses_rather_than_guessing_between_two_untagged_calls() -> None:
    """RULE 3. Two untagged candidates and no tag: binding either one would be a
    coin flip recorded as evidence, which is worse than a refusal the model can act
    on — so the refusal NAMES the action, and names it for a LATER message, since
    state calls dispatch first.

    Its own rejection reason (`ambiguous_evidence`, not `unresolved_evidence`):
    "you did no qualifying work" and "you did two pieces of qualifying work and
    labelled neither" need telling apart in telemetry, because only the second one
    means the tag is being skipped.
    """
    store = InMemorySessionStore()
    observer = _Recorder()
    tool = await _initialized(store, "headcount", observer=observer)
    await _entry(store, "call_q1", "runQuery")
    await _entry(store, "call_q2", "runQuery")

    result = await _update(tool, {"intent_id": "i1", "status": "completed"})

    assert result.status == "error"
    assert result.error_code == ANALYSIS_STATE_INVALID_CODE
    assert result.retryable is True
    assert "more than one call could have served it" in result.denial_detail
    assert "serves_intent='i1'" in result.denial_detail
    assert "NEXT message" in result.denial_detail
    assert observer.named("loop_analysis_state_rejected") == [
        {"reason": "ambiguous_evidence", "intent_count": 1}
    ]
    assert observer.named("loop_analysis_state_auto_bound") == []
    assert (await _intents(store))["i1"].status == "pending"


async def test_the_auto_bind_pool_only_holds_calls_that_would_validate() -> None:
    """The backstop may never bind something 04 would refuse — that is the one way
    it could quietly weaken the guarantee it exists to preserve.

    Three ran, none qualifies: a `sampleRows` (substantive work, but never
    completion evidence), an unverified `runBlueprint` (04 condition 4), and a
    `resolveValues`. A pool built from "successful substantive call" ALONE would
    hold all three and bind one; a pool built from the validators holds none, and
    the intent is refused as if nothing had run.
    """
    store = InMemorySessionStore()
    tool = await _initialized(store, "headcount")
    await _entry(store, "call_s1", "sampleRows")
    await _entry(store, "call_b1", "runBlueprint", authoritative=False)
    await _entry(store, "call_r1", "resolveValues")

    result = await _update(tool, {"intent_id": "i1", "status": "completed"})

    assert result.status == "error"
    assert "nothing this turn answered it" in result.denial_detail
    assert (await _intents(store))["i1"].status == "pending"


async def test_a_schema_fetch_is_tagged_evidence_but_never_auto_bound() -> None:
    """`getTableSchema` is admitted as completion evidence only as an ACCEPTED
    TRADE (04 §A / `COMPLETION_EVIDENCE_TOOLS`), measured by
    `loop_metadata_evidence_completion`. The trade is the model's to claim, so it
    is claimed with a TAG — auto-binding a schema fetch to an analytical intent
    would spend it silently, on a call the model may only have made to look around.

    Both halves are asserted together, because the gap between them IS the rule.
    """
    store = InMemorySessionStore()
    tagged = await _initialized(store, "what columns does employee have")
    await _entry(store, "call_s1", "getTableSchema", serves_intent="i1")
    ok = await _update(tagged, {"intent_id": "i1", "status": "completed"})
    assert ok.status == "ok", ok.denial_detail
    assert (await _intents(store))["i1"].evidence_tool_call_id == "call_s1"

    store2 = InMemorySessionStore()
    untagged = await _initialized(store2, "what columns does employee have")
    await store2.append_trail_entry(
        SESSION_ID,
        TrailEntry(
            turn_index=TURN,
            tool_call_id="call_s2",
            tool_name="getTableSchema",
            args={"database": "d", "table": "t"},
            status="ok",
            error_code=None,
            provenance=frozenset(),
            result_preview=ResultPreview(
                columns=["x"], row_count=1, truncated=False, preview_rows=[]
            ),
            result_full_ref=None,
            ts="2026-08-12T00:00:00+00:00",
        ),
    )
    refused = await untagged.run(
        {"intents": [{"intent_id": "i1", "status": "completed"}]},
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )
    assert refused.status == "error"
    assert "nothing this turn answered it" in refused.denial_detail


# ---------------------------------------------------------------------------
# Deriving the block reason instead of declaring it
# ---------------------------------------------------------------------------


async def test_the_block_reason_is_classified_off_the_bound_call() -> None:
    """04 §B already refused any block whose evidence did not PROVE the declared
    code, so the model's `reason_code` was a second copy of a fact the trail
    carried. Both codes are derived here from calls the model labelled with
    nothing but `status: "blocked"`.
    """
    store = InMemorySessionStore()
    tool = await _initialized(store, "salaries", "who left last month")
    await _entry(
        store,
        "call_denied",
        "runQuery",
        serves_intent="i1",
        status="denied",
        error_code="COLUMN_SCOPE_VIOLATION",
        row_count=None,
    )
    await _entry(store, "call_empty", "runQuery", serves_intent="i2", row_count=0)

    result = await _update(
        tool,
        {"intent_id": "i1", "status": "blocked"},
        {"intent_id": "i2", "status": "blocked"},
    )

    assert result.status == "ok", result.denial_detail
    intents = await _intents(store)
    assert intents["i1"].reason_code == "NO_ACCESS"
    assert intents["i2"].reason_code == "REQUIRED_DATA_UNAVAILABLE"


async def test_a_block_on_a_call_that_proves_neither_code_is_refused() -> None:
    """The derivation is not a fallback to "something went wrong". A call that
    merely FAILED — a retryable SQL error, not an access denial — proves neither
    declarable code, so there is nothing honest to write and the block is refused.

    This is the rule the model's own `reason_code` used to be checked against; it
    is unchanged, only nobody has to state it any more.
    """
    store = InMemorySessionStore()
    tool = await _initialized(store, "salaries")
    await _entry(
        store,
        "call_bad",
        "runQuery",
        serves_intent="i1",
        status="error",
        error_code="PARSE_FAILED_CLOSED",
        row_count=None,
    )

    result = await _update(tool, {"intent_id": "i1", "status": "blocked"})

    assert result.status == "error"
    assert "is not an access denial" in result.denial_detail
    assert (await _intents(store))["i1"].status == "pending"


async def test_an_empty_table_listing_is_not_auto_bound_as_absent_data() -> None:
    """THE HOLE THIS RELEASE WOULD OTHERWISE HAVE OPENED, found on review.

    `_build_preview`'s bare-list branch gives `listDatabases`/`listTables` a real
    `row_count`, so an EMPTY listing is `status="ok"` + `row_count == 0` — which
    `validate_block_evidence` accepts as `REQUIRED_DATA_UNAVAILABLE`. Under the
    old contract that was unreachable in practice: the model had to CITE the id,
    and citation never once worked. The auto-bind backstop would have handed it
    over for free, turning "this database has no tables" into "this deliverable
    cannot be done" with no model claim at all.

    So the blocked pool is narrowed PER DERIVED CODE: a zero-row claim must come
    from `SUBSTANTIVE_TOOLS`. A listing is discovery, not evidence of absence.
    """
    store = InMemorySessionStore()
    tool = await _initialized(store, "which tables hold payroll data")
    await _entry(store, "call_lt", "listTables", row_count=0)

    result = await _update(tool, {"intent_id": "i1", "status": "blocked"})

    assert result.status == "error"
    assert "came back empty" in result.denial_detail
    assert (await _intents(store))["i1"].status == "pending"

    # ...and the narrowing is scoped to the ZERO-ROW code only: the same listing
    # call, REFUSED, is still auto-bindable as NO_ACCESS. A denial is a denial
    # whichever tool hit it (04 §B.4 prices that route separately and openly).
    store2 = InMemorySessionStore()
    tool2 = await _initialized(store2, "which tables hold payroll data")
    await _entry(
        store2,
        "call_lt2",
        "listTables",
        status="denied",
        error_code="SCRATCH_SESSION_VIOLATION",
        row_count=None,
    )
    denied = await _update(tool2, {"intent_id": "i1", "status": "blocked"})
    assert denied.status == "ok", denied.denial_detail
    intents = await _intents(store2)
    assert (intents["i1"].reason_code, intents["i1"].evidence_tool_call_id) == (
        "NO_ACCESS",
        "call_lt2",
    )


async def test_a_zero_row_query_is_still_auto_bindable_as_absent_data() -> None:
    """The other side of the same narrowing: `SUBSTANTIVE_TOOLS` is the pool, so a
    zero-row `runQuery` still binds. Asserted beside the exclusion because a
    narrowing that also broke the legitimate case would look identical in the test
    above."""
    store = InMemorySessionStore()
    tool = await _initialized(store, "who left last month")
    await _entry(store, "call_empty", "runQuery", row_count=0)

    result = await _update(tool, {"intent_id": "i1", "status": "blocked"})

    assert result.status == "ok", result.denial_detail
    intents = await _intents(store)
    assert (intents["i1"].reason_code, intents["i1"].evidence_tool_call_id) == (
        "REQUIRED_DATA_UNAVAILABLE",
        "call_empty",
    )


async def test_a_successful_non_empty_call_cannot_be_turned_into_a_block() -> None:
    """The other direction: a query that RETURNED ROWS shows neither an access
    denial nor absent data. Without the derivation this needed the model to
    mislabel it; with the derivation there is no label to mislabel, and the
    classification simply finds nothing."""
    store = InMemorySessionStore()
    tool = await _initialized(store, "headcount")
    await _entry(store, "call_q1", "runQuery", serves_intent="i1", row_count=7)

    result = await _update(tool, {"intent_id": "i1", "status": "blocked"})

    assert result.status == "error"
    assert "so the data is not unavailable" in result.denial_detail
    assert (await _intents(store))["i1"].status == "pending"


# ---------------------------------------------------------------------------
# Tolerant reading of the two retired fields
# ---------------------------------------------------------------------------


async def test_a_replayed_evidence_id_and_reason_code_are_dropped_not_read() -> None:
    """The model's own earlier calls are replayed to it verbatim, so a conversation
    that spanned the deploy WILL send both retired names again.

    Neither may be rejected (that fails a correct update over a field the model was
    shown by its own history) and neither may be READ — the values here are
    deliberately WRONG in both directions: a hallucinated id of the kind that
    scored 0/9 live, and a `NO_ACCESS` label on a zero-row call. The update lands
    on the tagged call with the DERIVED code, exactly as if neither had been sent.
    """
    store = InMemorySessionStore()
    observer = _Recorder()
    tool = await _initialized(store, "who left last month", observer=observer)
    await _entry(store, "call_empty", "runQuery", serves_intent="i1", row_count=0)

    result = await tool.run(
        {
            "intents": [
                {
                    "intent_id": "i1",
                    "status": "blocked",
                    "evidence_tool_call_id": "call_UUGw4MtdHallucinated",
                    "reason_code": "NO_ACCESS",
                }
            ]
        },
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )

    assert result.status == "ok", result.denial_detail
    intents = await _intents(store)
    assert intents["i1"].evidence_tool_call_id == "call_empty"
    assert intents["i1"].reason_code == "REQUIRED_DATA_UNAVAILABLE"
    assert observer.named("loop_analysis_state_rejected") == []


async def test_the_retired_fields_are_dropped_when_empty_too_in_both_modes() -> None:
    """The placeholder serialisation, which is what a model that cannot omit keys
    actually sends. Asserted on the DECLARATION as well as the update: the
    declaration path checks unknown keys against `{description}` alone, so an
    empty `evidence_tool_call_id` there would be `unknown_item_key` if the drop
    were only wired into the update half."""
    store = InMemorySessionStore()
    tool = UpdateAnalysisStateTool(session_store=store, observer=_Recorder())
    declared = await tool.run(
        {
            "intents": [
                {
                    "description": "who left last month",
                    "intent_id": "",
                    "status": "pending",
                    "evidence_tool_call_id": "",
                    "reason_code": "NO_ACCESS",
                }
            ]
        },
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )
    assert declared.status == "ok", declared.denial_detail
    assert declared.result_full["intents"][0]["reason_code"] is None

    await _entry(store, "call_q1", "runQuery", serves_intent="i1")
    updated = await tool.run(
        {
            "intents": [
                {
                    "intent_id": "i1",
                    "status": "completed",
                    "evidence_tool_call_id": "",
                    "reason_code": "NO_ACCESS",
                }
            ]
        },
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )
    assert updated.status == "ok", updated.denial_detail
    intents = await _intents(store)
    assert (intents["i1"].status, intents["i1"].reason_code) == ("completed", None)


async def test_re_affirming_a_closed_intent_is_a_no_op_not_a_re_resolution() -> None:
    """Live models re-send the whole intent list every round. An intent already
    closed on its own evidence keeps it, and is NOT put back through the backstop —
    the trail grows, so a bind that was unambiguous in round 2 becomes ambiguous in
    round 4, and that refusal would take the OTHER intents in the same batch down
    with it.

    It launders nothing: a status CHANGE misses this branch entirely and is
    resolved in full, which the second half asserts.
    """
    store = InMemorySessionStore()
    tool = await _initialized(store, "headcount", "salary")
    await _entry(store, "call_q1", "runQuery")
    first = await _update(tool, {"intent_id": "i1", "status": "completed"})
    assert first.status == "ok", first.denial_detail

    # A second qualifying call arrives, tagged for i2. i1 is re-sent unchanged.
    await _entry(store, "call_q2", "runQuery", serves_intent="i2")
    again = await _update(
        tool,
        {"intent_id": "i1", "status": "completed"},
        {"intent_id": "i2", "status": "completed"},
    )
    assert again.status == "ok", again.denial_detail
    intents = await _intents(store)
    assert intents["i1"].evidence_tool_call_id == "call_q1"  # kept, not re-bound
    assert intents["i2"].evidence_tool_call_id == "call_q2"

    # ...and a CHANGE of status is resolved in full, so it cannot ride the no-op.
    changed = await _update(tool, {"intent_id": "i1", "status": "blocked"})
    assert changed.status == "error"
    assert (await _intents(store))["i1"].status == "completed"
