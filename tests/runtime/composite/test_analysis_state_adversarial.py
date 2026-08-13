"""Adversarial coverage for `updateAnalysisState` (Release 1, doc 03 §F).

Two jobs. First, prove the immutability rules that close the CHEAP evasion — the
model cannot silently drop an ask by shortening the array, rewriting a
description, minting its own ids, or re-declaring the set.

Second, and just as important, prove what is NOT closed. Manufactured block
evidence is cheap and permitted today (03 §C.4); it is asserted here, with its
telemetry, so the hole is measured rather than implied away.
"""

from __future__ import annotations

import json
from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import (
    ANALYSIS_STATE_INVALID_CODE,
    MAX_DESCRIPTION_CHARS,
    MAX_INTENTS,
    REJECTION_REASONS,
    UpdateAnalysisStateTool,
)
from data_agent.runtime.loop.agent_loop import TurnContext
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import ResultPreview, TrailEntry, live_analysis_state

SESSION_ID = "sess-analysis-adversarial"
TURN = 1


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, payload))

    def named(self, event: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.events if name == event]


def _tool(store: InMemorySessionStore, observer: _Recorder | None = None):
    return UpdateAnalysisStateTool(
        session_store=store, observer=observer or _Recorder()
    )


async def _entry(
    store: InMemorySessionStore,
    tool_call_id: str,
    tool_name: str,
    *,
    status: str = "ok",
    error_code: str | None = None,
    row_count: int | None = 1,
    serves_intent: str | None = None,
) -> None:
    preview = (
        None
        if row_count is None
        else ResultPreview(columns=["x"], row_count=row_count, truncated=False, preview_rows=[])
    )
    await store.append_trail_entry(
        SESSION_ID,
        TrailEntry(
            turn_index=TURN,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args={},
            status=status,
            error_code=error_code,
            provenance=frozenset(),
            result_preview=preview,
            result_full_ref=None,
            ts="2026-08-11T00:00:00+00:00",
            serves_intent=serves_intent,
        ),
    )


async def _initialized(
    store: InMemorySessionStore, *descriptions: str, observer: _Recorder | None = None
):
    tool = _tool(store, observer)
    await tool.run(
        {"intents": [{"description": d} for d in descriptions]},
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )
    return tool


async def _reject(tool, args: dict[str, Any]):
    result = await tool.run(args, _credentials(), turn=TurnContext(turn_index=TURN))
    assert result.status == "error", result
    assert result.error_code == ANALYSIS_STATE_INVALID_CODE
    assert result.retryable is True
    assert result.denial_detail
    return result


# ---------------------------------------------------------------------------
# The live finding: the model cannot omit keys
#
# A real two-deliverable turn against gpt-5.5 ("Give me the active headcount by
# department, and the average salary by department") called updateAnalysisState
# SIX times and had all six rejected as `model_supplied_intent_id` — over an
# `intent_id` of `""`. Five retries with a correct `denial_detail` in front of the
# model did not recover it. No state was ever created, so the whole feature was
# INERT for that turn and, because the turn still answered, it failed SILENTLY.
#
# The model emits every property the flat item schema declares and fills the ones
# it is not using with placeholders: `""` for a string, the FIRST ENUM MEMBER for
# an enum (hence `NO_ACCESS` on a `pending` intent — a filler, not a claim).
# ---------------------------------------------------------------------------


async def test_the_six_rejection_live_turn_now_initializes_on_the_first_call() -> None:
    """The payload VERBATIM from the persisted trail of that turn."""
    store = InMemorySessionStore()
    observer = _Recorder()

    result = await _tool(store, observer).run(
        {
            "intents": [
                {
                    "description": "Active headcount by department.",
                    "intent_id": "",
                    "evidence_tool_call_id": "",
                    "reason_code": "NO_ACCESS",
                    "status": "pending",
                },
                {
                    "description": "Average salary by department.",
                    "intent_id": "",
                    "evidence_tool_call_id": "",
                    "reason_code": "NO_ACCESS",
                    "status": "pending",
                },
            ]
        },
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )

    assert result.status == "ok", result.denial_detail
    assert [i["intent_id"] for i in result.result_full["intents"]] == ["i1", "i2"]
    assert [i["description"] for i in result.result_full["intents"]] == [
        "Active headcount by department.",
        "Average salary by department.",
    ]
    # The placeholders are IGNORED, never recorded: a pending intent carries no
    # reason code (04 §B.2), so `NO_ACCESS` must not have survived into the ledger.
    assert all(i["status"] == "pending" for i in result.result_full["intents"])
    assert all(i["reason_code"] is None for i in result.result_full["intents"])
    assert all(i["evidence_tool_call_id"] is None for i in result.result_full["intents"])
    assert observer.named("loop_analysis_state_rejected") == []

    doc = await store.get_or_create_session(SESSION_ID)
    state = live_analysis_state(doc, TURN)
    assert [(i.intent_id, i.reason_code) for i in state.intents] == [("i1", None), ("i2", None)]


async def test_an_update_tolerates_the_same_placeholders() -> None:
    """The same serialisation one call later: the unused 'description' comes back
    as `""`, and the two RETIRED fields come back populated because the model is
    reading its own replayed history. None of the three may be read — a
    description rewrite, a hallucinated evidence id, or a `NO_ACCESS` label on a
    denial-free call would each be the identical silent failure, leaving every
    intent pending until enforcement force-blocked it."""
    store = InMemorySessionStore()
    observer = _Recorder()
    tool = await _initialized(store, "headcount by dept", "avg salary by dept",
                              observer=observer)
    await _entry(store, "call_q", "runQuery", serves_intent="i1")
    await _entry(store, "call_denied", "runQuery", status="denied",
                 error_code="COLUMN_SCOPE_VIOLATION", row_count=None, serves_intent="i2")

    result = await tool.run(
        {
            "intents": [
                {
                    "intent_id": "i1",
                    "status": "completed",
                    "evidence_tool_call_id": "",
                    "description": "",
                    "reason_code": "NO_ACCESS",
                },
                {
                    "intent_id": "i2",
                    "status": "blocked",
                    "evidence_tool_call_id": "call_hallucinated",
                    "description": "   ",
                    "reason_code": "REQUIRED_DATA_UNAVAILABLE",
                },
            ]
        },
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )

    assert result.status == "ok", result.denial_detail
    by_id = {i["intent_id"]: i for i in result.result_full["intents"]}
    assert by_id["i1"]["status"] == "completed"
    assert by_id["i1"]["reason_code"] is None  # dropped, not recorded
    # DERIVED from the tagged denial — the opposite of the label that was sent.
    assert by_id["i2"]["reason_code"] == "NO_ACCESS"
    assert by_id["i2"]["evidence_tool_call_id"] == "call_denied"
    # Descriptions are still the frozen originals.
    assert by_id["i1"]["description"] == "headcount by dept"
    assert observer.named("loop_analysis_state_rejected") == []


async def test_a_pending_update_tolerates_the_placeholder_enum_too() -> None:
    """`pending` + a filler `reason_code` is not a claim that the intent is
    blocked; the status is the claim, and it says otherwise. Since the trim the
    filler is dropped outright rather than status-gated, which is strictly
    simpler — but the OUTCOME asserted here is the one that matters and is
    unchanged."""
    store = InMemorySessionStore()
    tool = await _initialized(store, "a")
    result = await tool.run(
        {"intents": [{"intent_id": "i1", "status": "pending",
                      "evidence_tool_call_id": "", "reason_code": "NO_ACCESS"}]},
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )
    assert result.status == "ok", result.denial_detail
    assert result.result_full["intents"][0]["reason_code"] is None


async def test_an_empty_intent_id_is_absent_but_a_supplied_one_is_still_rejected() -> None:
    """The boundary the fix must not cross. `""` carries no information and is
    absent; a REAL id on a first declaration is the model minting its own, and ids
    stay runtime-assigned."""
    observer = _Recorder()
    result = await _reject(
        _tool(InMemorySessionStore(), observer),
        {"intents": [{"description": "a", "intent_id": "i7", "status": "pending",
                      "evidence_tool_call_id": "", "reason_code": "NO_ACCESS"}]},
    )
    assert "assigned by the runtime" in result.denial_detail
    assert {"reason": "model_supplied_intent_id", "intent_count": 1} in observer.named(
        "loop_analysis_state_rejected"
    )


async def test_a_non_pending_status_on_initialize_is_rejected_not_ignored() -> None:
    """`pending` is the enum's first member, so it is the model's filler AND what
    the runtime writes anyway — ignoring it is safe. Any other status is a claim
    that a just-declared intent is already resolved, and nothing could have
    resolved it: state calls are dispatched before every other call in the batch."""
    for status in ("completed", "blocked"):
        store = InMemorySessionStore()
        observer = _Recorder()
        result = await _reject(
            _tool(store, observer),
            {"intents": [{"description": "a", "intent_id": "", "status": status,
                          "evidence_tool_call_id": "", "reason_code": "NO_ACCESS"}]},
        )
        assert status in result.denial_detail
        assert {"reason": "status_on_initialize", "intent_count": 1} in observer.named(
            "loop_analysis_state_rejected"
        )
        doc = await store.get_or_create_session(SESSION_ID)
        assert doc.analysis_state is None


async def test_an_unknown_key_is_still_rejected_even_when_it_is_empty() -> None:
    """Normalisation elides only the fields the SCHEMA declares. A typo'd key is
    not one of them, whatever it holds — otherwise a mistyped 'descriptoin' would
    vanish and the call would look like it landed."""
    await _reject(
        _tool(InMemorySessionStore()),
        {"intents": [{"description": "a", "descriptoin": ""}]},
    )


async def test_an_all_placeholder_item_has_no_description_at_all() -> None:
    """Eliding empties must not turn an EMPTY item into a valid declaration — the
    description rule still fires, on the same reason enum."""
    observer = _Recorder()
    result = await _reject(
        _tool(InMemorySessionStore(), observer),
        {"intents": [{"description": "  ", "intent_id": "", "status": "pending",
                      "evidence_tool_call_id": "", "reason_code": "NO_ACCESS"}]},
    )
    assert "non-empty 'description'" in result.denial_detail
    assert {"reason": "missing_description", "intent_count": 1} in observer.named(
        "loop_analysis_state_rejected"
    )


# ---------------------------------------------------------------------------
# Immutability — the evasions this closes
# ---------------------------------------------------------------------------


async def test_adding_an_intent_by_update_is_rejected() -> None:
    store = InMemorySessionStore()
    tool = await _initialized(store, "a", "b")
    await _entry(store, "call_q", "runQuery")
    await _reject(
        tool,
        {"intents": [{"intent_id": "i9", "status": "completed",
                      "evidence_tool_call_id": "call_q"}]},
    )


async def test_dropping_an_intent_is_structurally_impossible() -> None:
    """There is no shape that removes one. A shorter array is a PARTIAL update,
    and the survivor keeps its pending disposition."""
    store = InMemorySessionStore()
    tool = await _initialized(store, "easy", "hard")
    await _entry(store, "call_q", "runQuery")
    result = await tool.run(
        {"intents": [{"intent_id": "i1", "status": "completed",
                      "evidence_tool_call_id": "call_q"}]},
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )
    assert result.status == "ok"
    doc = await store.get_or_create_session(SESSION_ID)
    state = live_analysis_state(doc, TURN)
    assert [(i.intent_id, i.status) for i in state.intents] == [
        ("i1", "completed"),
        ("i2", "pending"),
    ]


async def test_rewriting_a_description_is_rejected() -> None:
    """The description is the record enforcement reads. Rewriting it is how a hard
    ask would be laundered into an easy one."""
    store = InMemorySessionStore()
    tool = await _initialized(store, "attrition by department for last quarter")
    result = await _reject(
        tool,
        {"intents": [{"intent_id": "i1", "status": "pending", "description": "say hello"}]},
    )
    assert "cannot be rewritten" in result.denial_detail
    doc = await store.get_or_create_session(SESSION_ID)
    assert live_analysis_state(doc, TURN).intents[0].description == (
        "attrition by department for last quarter"
    )


async def test_model_supplied_intent_id_on_initialize_is_rejected() -> None:
    store = InMemorySessionStore()
    result = await _reject(
        _tool(store), {"intents": [{"description": "a", "intent_id": "i7"}]}
    )
    assert "assigned by the runtime" in result.denial_detail


async def test_a_second_initialize_is_rejected() -> None:
    store = InMemorySessionStore()
    tool = await _initialized(store, "a")
    result = await _reject(tool, {"intents": [{"description": "b"}, {"description": "c"}]})
    assert "already declared" in result.denial_detail
    doc = await store.get_or_create_session(SESSION_ID)
    assert [i.description for i in live_analysis_state(doc, TURN).intents] == ["a"]


async def test_unknown_keys_are_rejected_not_ignored() -> None:
    """A typo'd key that silently vanished would look like a state update that
    landed."""
    store = InMemorySessionStore()
    await _reject(_tool(store), {"intents": [{"description": "a"}], "mode": "initialize"})

    tool = await _initialized(InMemorySessionStore(), "a")
    await _reject(tool, {"intents": [{"intent_id": "i1", "status": "pending", "note": "x"}]})


async def test_an_over_length_description_is_rejected_not_truncated() -> None:
    """03 §A.4: `recordAssumptions` truncates because its content is ADVISORY.
    This content is not — silently trimming it rewrites the record."""
    store = InMemorySessionStore()
    long_description = "x" * (MAX_DESCRIPTION_CHARS + 1)
    result = await _reject(_tool(store), {"intents": [{"description": long_description}]})
    assert str(MAX_DESCRIPTION_CHARS) in result.denial_detail
    doc = await store.get_or_create_session(SESSION_ID)
    assert doc.analysis_state is None  # nothing persisted, not a trimmed record


async def test_too_many_intents_is_rejected_not_capped() -> None:
    store = InMemorySessionStore()
    await _reject(
        _tool(store),
        {"intents": [{"description": f"d{n}"} for n in range(MAX_INTENTS + 1)]},
    )
    doc = await store.get_or_create_session(SESSION_ID)
    assert doc.analysis_state is None


async def test_a_runtime_only_reason_code_from_the_model_is_unreachable() -> None:
    """The MODEL/RUNTIME split, restated for a world where the model declares no
    code at all.

    It used to be an allowlist rejection. It is now STRUCTURAL and strictly
    stronger: the field is not in the schema, anything arriving under that name is
    dropped unread, and the derivation only ever consults `MODEL_REASON_CODES` —
    so a runtime-only code cannot be reached from this path even by accident. 05
    still writes them directly, which is the only way they are ever written.

    The three are sent anyway, against a call that legitimately supports a block,
    to prove they are ignored rather than honoured: the persisted code is the
    DERIVED `NO_ACCESS` every time.
    """
    for code in ("ENFORCEMENT_EXHAUSTED", "BUDGET_EXHAUSTED", "USER_STOPPED"):
        store = InMemorySessionStore()
        tool = await _initialized(store, "a")
        await _entry(store, "call_q", "runQuery", status="denied",
                     error_code="COLUMN_SCOPE_VIOLATION", row_count=None,
                     serves_intent="i1")
        result = await tool.run(
            {"intents": [{"intent_id": "i1", "status": "blocked", "reason_code": code}]},
            _credentials(),
            turn=TurnContext(turn_index=TURN),
        )
        assert result.status == "ok", result.denial_detail
        assert live_analysis_state(
            await store.get_or_create_session(SESSION_ID), TURN
        ).intents[0].reason_code == "NO_ACCESS"


async def test_a_terminal_status_cannot_be_reached_with_no_binding_at_all() -> None:
    """What replaced 04 §B.2's presence rule, and the property that had to survive
    the trim: neither terminal status is reachable without a real call behind it.

    The old rule was a PAYLOAD check — "completed requires a non-empty string" —
    which was bypassable in five shapes (omitted, null, empty, whitespace, and a
    string naming nothing). There is no field left to omit, so the rule is now a
    TRAIL check with one shape and no bypass: an empty turn resolves to nothing,
    for either status, and `pending` writes no evidence whatever arrives with it.
    """
    store = InMemorySessionStore()
    tool = await _initialized(store, "a")
    for status in ("completed", "blocked"):
        await _reject(tool, {"intents": [{"intent_id": "i1", "status": status}]})
    # ...and the retired names cannot smuggle one in either.
    await _entry(store, "call_q", "runQuery")
    for legacy in ({"evidence_tool_call_id": "call_q"}, {"reason_code": "NO_ACCESS"}):
        await _reject(
            tool, {"intents": [{"intent_id": "i1", "status": "blocked", **legacy}]}
        )
    # `pending` is accepted and records NOTHING — the placeholder cannot make an
    # unresolved intent look evidenced.
    pending = await tool.run(
        {"intents": [{"intent_id": "i1", "status": "pending",
                      "evidence_tool_call_id": "call_q", "reason_code": "NO_ACCESS"}]},
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )
    assert pending.status == "ok", pending.denial_detail
    intent = pending.result_full["intents"][0]
    assert (intent["evidence_tool_call_id"], intent["reason_code"]) == (None, None)


async def test_one_denial_cannot_block_three_intents() -> None:
    """04 §B.3, Lead-approved. Without it, ONE `getTableSchema(<scratch_db>, "x")`
    blocks every intent at once and finalization proceeds on a fully "evidenced"
    record. This raises the price from O(1) to O(n) and makes bulk-blocking
    visible instead of hiding it behind a shared id."""
    store = InMemorySessionStore()
    observer = _Recorder()
    tool = await _initialized(store, "a", "b", "c", observer=observer)
    await _entry(store, "call_scratch", "getTableSchema", status="denied",
                 error_code="SCRATCH_SESSION_VIOLATION", row_count=None)

    result = await _reject(
        tool,
        {
            "intents": [
                {"intent_id": i, "status": "blocked", "reason_code": "NO_ACCESS",
                 "evidence_tool_call_id": "call_scratch"}
                for i in ("i1", "i2", "i3")
            ]
        },
    )
    assert "already the evidence" in result.denial_detail
    assert {"reason": "block_evidence_reused", "intent_count": 3} in observer.named(
        "loop_analysis_state_rejected"
    )
    doc = await store.get_or_create_session(SESSION_ID)
    assert all(i.status == "pending" for i in live_analysis_state(doc, TURN).intents)


async def test_block_evidence_distinctness_holds_across_separate_calls() -> None:
    """Checked over the MERGED state, so a second call cannot spend an id an
    earlier call already used."""
    store = InMemorySessionStore()
    tool = await _initialized(store, "a", "b")
    await _entry(store, "call_denied", "runQuery", status="denied",
                 error_code="COLUMN_SCOPE_VIOLATION", row_count=None)
    ok = await tool.run(
        {"intents": [{"intent_id": "i1", "status": "blocked", "reason_code": "NO_ACCESS",
                      "evidence_tool_call_id": "call_denied"}]},
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )
    assert ok.status == "ok"
    await _reject(
        tool,
        {"intents": [{"intent_id": "i2", "status": "blocked", "reason_code": "NO_ACCESS",
                      "evidence_tool_call_id": "call_denied"}]},
    )


async def test_completion_evidence_reuse_is_permitted_and_flagged() -> None:
    """The deliberate asymmetry: one query genuinely answers two asks."""
    store = InMemorySessionStore()
    observer = _Recorder()
    tool = await _initialized(store, "headcount by dept", "avg salary by dept",
                              observer=observer)
    await _entry(store, "call_q", "runQuery")

    result = await tool.run(
        {
            "intents": [
                {"intent_id": "i1", "status": "completed", "evidence_tool_call_id": "call_q"},
                {"intent_id": "i2", "status": "completed", "evidence_tool_call_id": "call_q"},
            ]
        },
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )
    assert result.status == "ok"
    reused = observer.named("loop_evidence_reused")
    assert {p["intent_id"] for p in reused} == {"i1", "i2"}


# ---------------------------------------------------------------------------
# Known-open: manufactured block evidence
# ---------------------------------------------------------------------------


async def test_manufactured_block_evidence_is_permitted_today_and_is_measured() -> None:
    """03 §C.4 / 04 §B.4 — recorded as KNOWN-PERMITTED with its telemetry.

    `SELECT ... WHERE 1=0` returns zero rows, which is a valid
    `REQUIRED_DATA_UNAVAILABLE` block. Immutability does not close this and was
    never claimed to: it closes the SILENT DROP. This test fails the day someone
    believes they closed manufacture without saying so — and asserts the
    `loop_zero_row_block` counter that makes the frequency visible."""
    store = InMemorySessionStore()
    observer = _Recorder()
    tool = await _initialized(store, "who left last month", observer=observer)
    await _entry(store, "call_empty", "runQuery", row_count=0)

    result = await tool.run(
        {"intents": [{"intent_id": "i1", "status": "blocked",
                      "reason_code": "REQUIRED_DATA_UNAVAILABLE",
                      "evidence_tool_call_id": "call_empty"}]},
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )
    assert result.status == "ok"
    assert observer.named("loop_zero_row_block") == [{"intent_id": "i1"}]


async def test_the_same_zero_row_result_completing_an_intent_is_counted_separately() -> None:
    """The other half of the 04 §B.4 ratio: the SAME entry is valid completion
    evidence, which is why the two counters exist rather than a rule."""
    store = InMemorySessionStore()
    observer = _Recorder()
    tool = await _initialized(store, "who left last month", observer=observer)
    await _entry(store, "call_empty", "runQuery", row_count=0)

    result = await tool.run(
        {"intents": [{"intent_id": "i1", "status": "completed",
                      "evidence_tool_call_id": "call_empty"}]},
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )
    assert result.status == "ok"
    assert observer.named("loop_zero_row_completion") == [{"intent_id": "i1"}]
    assert observer.named("loop_zero_row_block") == []


# ---------------------------------------------------------------------------
# Telemetry posture
# ---------------------------------------------------------------------------


async def test_no_event_payload_ever_carries_a_description(monkeypatch) -> None:
    """THE D25 REGRESSION GUARD. `intent_id` is runtime-assigned and carries no
    user content; `description` is model-authored FROM THE USER'S QUESTION and is
    never shape. Scan every emitted payload for the fixture text — the same
    technique `test_tool_dispatcher.py` uses to scan every `ToolResult` field for
    JWT substrings, for the same reason."""
    secret = "salaries above ONE-HUNDRED-THOUSAND for the Sales team"
    store = InMemorySessionStore()
    observer = _Recorder()
    tool = await _initialized(store, secret, "second deliverable", observer=observer)
    await _entry(store, "call_q", "runQuery", row_count=0)
    await tool.run(
        {"intents": [{"intent_id": "i1", "status": "completed",
                      "evidence_tool_call_id": "call_q"}]},
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )
    # A rejection path too — its payload is the one most likely to echo input.
    await tool.run(
        {"intents": [{"intent_id": "i9", "status": "pending"}]},
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )

    assert observer.events, "the scan proves nothing if nothing was emitted"
    blob = json.dumps(observer.events, default=str)
    assert secret not in blob
    assert "ONE-HUNDRED-THOUSAND" not in blob
    for name, payload in observer.events:
        if name == "loop_analysis_state_rejected":
            assert payload["reason"] in REJECTION_REASONS, payload
