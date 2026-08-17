"""D94 Part 1/2 — `ok`+`None` provenance-stranding sentinel injection at the
`ContextAssembler.assemble` layer (design docs/decisions/none-provenance-stranding-design.md).

A current-turn tool result with `status="ok"` but `provenance=None` (a
catalog/extractor SKEW — the MCP's own parse passed so status is `ok`, but the
runtime's independent D44 re-parse could not determine provenance) is correctly
dropped by `scope_filter.filter_trail` (fail-closed). Before D94 it was SILENTLY
stranded: dropped every rebuild -> the model never saw a result for its dangling
`tool_call` -> re-emitted the identical call -> burned to the budget cap with no
signal.

`assemble` now injects a non-data-bearing SENTINEL tool message keyed to that
entry's `tool_call_id` (filling the dangling slot so the loop breaks) and emits a
de-duped `loop_result_withheld_provenance` diagnostic. The sentinel carries ZERO
data — PII-safe under ANY scope, including empty/narrow.

This file mirrors `tests/runtime/context/test_assembly.py`'s InMemory Layer-1
harness and covers matrix rows 1, 2, 4, 5, 6, 7, 11 (assembly-layer slices);
the end-to-end loop break (row 3) lives in
`tests/runtime/loop/test_withheld_provenance_loop_break_d94.py` and the seed
skew warning (rows 9/10) in
`tests/runtime/retrieval/test_corpus_loader_skew_d94.py`.
"""

from __future__ import annotations

import json
from typing import Any

from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.loop.agent_loop import _assembled_to_canonical
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import ResultPreview, TrailEntry

_P = "dbpcm_warehouse.payroll"
_E = "dbpcm_warehouse.employee"

# Independently transcribed from the design doc's §2 "Sentinel text (exact)"
# block — NOT imported from the implementation, so a drift in the shipped
# constant is caught byte-for-byte (note the U+2014 em-dash).
EXPECTED_SENTINEL = (
    "result withheld: provenance could not be determined for this call, so its "
    "result cannot be shown. Do not retry the identical call — it will be withheld "
    "again. Try a different query or approach, or ask the user."
)

# A recognizable token planted in the stranded entry's data-bearing fields; it
# must NEVER appear anywhere in the assembled/canonical output or in any event.
_SECRET = "SUPER_SECRET_PII_TOKEN_9f83b1"


def _entry(
    tool_call_id: str,
    provenance: frozenset | None,
    *,
    tool_name: str = "runQuery",
    args: dict[str, Any] | None = None,
    turn_index: int = 0,
    status: str = "ok",
    result_preview: ResultPreview | None = None,
) -> TrailEntry:
    return TrailEntry(
        turn_index=turn_index,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        args=args if args is not None else {"sql": "SELECT ..."},
        status=status,
        error_code=None if status == "ok" else "SOME_ERROR",
        provenance=provenance,
        result_preview=result_preview,
        result_full_ref=None,
        ts="2026-07-01T00:00:00+00:00",
    )


def _secret_preview() -> ResultPreview:
    return ResultPreview(
        columns=[f"col_{_SECRET}"],
        row_count=1,
        truncated=False,
        preview_rows=[[_SECRET]],
    )


def _events_collector() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    events: list[tuple[str, dict[str, Any]]] = []

    def observer(name: str, payload: dict[str, Any]) -> None:
        events.append((name, dict(payload)))

    return events, observer


def _sentinel_messages(messages: list[dict[str, Any]], tool_call_id: str) -> list[dict[str, Any]]:
    return [
        m
        for m in messages
        if m.get("role") == "tool"
        and m.get("tool_call_id") == tool_call_id
        and m.get("content") == EXPECTED_SENTINEL
    ]


# ---------------------------------------------------------------------------
# Row 1 — sentinel appears for BOTH paths (raw runQuery + runBlueprint)
# ---------------------------------------------------------------------------


async def test_sentinel_injected_for_current_turn_ok_none_runquery() -> None:
    store = InMemorySessionStore()
    stranded = _entry(
        "call_rq",
        None,
        tool_name="runQuery",
        args={"sql": "SELECT * FROM generateRandom('a Int')"},
        turn_index=0,
        status="ok",
        result_preview=_secret_preview(),
    )
    await store.append_trail_entry("sess-1", stranded)

    assembler = ContextAssembler(store)
    assembled = await assembler.assemble("sess-1", frozenset(), current_turn_index=0)

    hits = _sentinel_messages(assembled.messages, "call_rq")
    assert len(hits) == 1
    # Byte-exact content.
    assert hits[0]["content"] == EXPECTED_SENTINEL


async def test_sentinel_injected_for_current_turn_ok_none_runblueprint() -> None:
    store = InMemorySessionStore()
    stranded = _entry(
        "call_bp",
        None,
        tool_name="runBlueprint",
        args={"id": "bp-overtime-by-department"},
        turn_index=0,
        status="ok",
        result_preview=_secret_preview(),
    )
    await store.append_trail_entry("sess-1", stranded)

    assembler = ContextAssembler(store)
    assembled = await assembler.assemble("sess-1", frozenset(), current_turn_index=0)

    hits = _sentinel_messages(assembled.messages, "call_bp")
    assert len(hits) == 1
    assert hits[0]["content"] == EXPECTED_SENTINEL


# ---------------------------------------------------------------------------
# Row 2 (CRITICAL) — no data leak, under empty AND narrow scope
# ---------------------------------------------------------------------------


async def _assert_no_secret_anywhere(assembled_messages: list[dict[str, Any]]) -> None:
    # Serialize the render-shape messages AND their canonical (model-facing) form.
    blob = json.dumps(assembled_messages, default=str, ensure_ascii=False)
    canonical = _assembled_to_canonical(assembled_messages)
    canonical_blob = json.dumps(canonical, default=str, ensure_ascii=False)
    assert _SECRET not in blob
    assert _SECRET not in canonical_blob


async def test_sentinel_leaks_no_data_under_empty_scope() -> None:
    store = InMemorySessionStore()
    # FIX 1: the model's OWN current-turn SQL args now legitimately reappear in
    # the synthesized assistant tool_call (correlation), so the SECRET must live
    # in the entry's RESULT payload (result_preview) — the actual warehouse data
    # that must NEVER surface — not in the SQL. Args here are benign by design.
    stranded = _entry(
        "call_leak",
        None,
        tool_name="runQuery",
        args={"sql": "SELECT Department FROM employee"},
        turn_index=0,
        status="ok",
        result_preview=_secret_preview(),
    )
    await store.append_trail_entry("sess-1", stranded)

    assembler = ContextAssembler(store)
    # Empty scope == allow-all, yet an undetermined (None) entry is STILL dropped.
    assembled = await assembler.assemble("sess-1", frozenset(), current_turn_index=0)

    # The sentinel IS present (loop-break) ...
    assert len(_sentinel_messages(assembled.messages, "call_leak")) == 1
    # ... but NONE of the entry's data-bearing bytes are.
    await _assert_no_secret_anywhere(assembled.messages)


async def test_sentinel_leaks_no_data_under_narrow_scope() -> None:
    store = InMemorySessionStore()
    # SECRET lives in the RESULT payload only; the args (table name) are benign.
    stranded = _entry(
        "call_leak",
        None,
        tool_name="sampleRows",
        args={"database": "dbpcm_warehouse", "table": "employee"},
        turn_index=0,
        status="ok",
        result_preview=_secret_preview(),
    )
    await store.append_trail_entry("sess-1", stranded)

    assembler = ContextAssembler(store)
    narrow = frozenset({f"{_E}.EmployeeCode"})
    assembled = await assembler.assemble("sess-1", narrow, current_turn_index=0)

    assert len(_sentinel_messages(assembled.messages, "call_leak")) == 1
    await _assert_no_secret_anywhere(assembled.messages)


async def test_sentinel_message_carries_only_id_toolname_args_and_content() -> None:
    """The injected message exposes exactly the FIX-1 sentinel keys — it carries
    the model's OWN args (correlation) + the `withheld_sentinel` discriminator +
    the byte-exact sentinel content, but NONE of the protected RESULT payload
    (`result_preview`/`result_full`, status/error_code data fields)."""
    store = InMemorySessionStore()
    own_sql = "SELECT Department FROM employee"
    await store.append_trail_entry(
        "sess-1",
        _entry(
            "call_x",
            None,
            args={"sql": own_sql},
            turn_index=0,
            result_preview=_secret_preview(),
        ),
    )
    assembler = ContextAssembler(store)
    assembled = await assembler.assemble("sess-1", frozenset(), current_turn_index=0)

    sentinel = _sentinel_messages(assembled.messages, "call_x")[0]
    assert set(sentinel.keys()) == {
        "role",
        "tool_call_id",
        "tool_name",
        "args",
        "withheld_sentinel",
        "content",
    }
    # content is byte-exact the fixed, data-free marker.
    assert sentinel["content"] == EXPECTED_SENTINEL
    assert sentinel["withheld_sentinel"] is True
    # The model's OWN args are replayed (correlation)...
    assert sentinel["args"] == {"sql": own_sql}
    # ...but the protected RESULT payload is absent from the dict.
    assert "result_preview" not in sentinel
    assert "result_full" not in sentinel
    assert "result_full_ref" not in sentinel
    assert "status" not in sentinel


# ---------------------------------------------------------------------------
# Row 5 — cross-turn ok+None stays dropped (no sentinel, no event)
# ---------------------------------------------------------------------------


async def test_cross_turn_ok_none_gets_no_sentinel_and_no_event() -> None:
    store = InMemorySessionStore()
    # A PRIOR-turn (turn 0) ok+None entry; current turn is 1.
    await store.append_trail_entry(
        "sess-1",
        _entry(
            "prior_call",
            None,
            turn_index=0,
            status="ok",
            args={"sql": f"SELECT {_SECRET}"},
            result_preview=_secret_preview(),
        ),
    )
    events, observer = _events_collector()
    assembler = ContextAssembler(store)
    assembled = await assembler.assemble(
        "sess-1", frozenset(), current_turn_index=1, withheld_call_ids=set(), observer=observer
    )

    # No sentinel for a cross-turn stranded entry, and no message at all for it.
    assert _sentinel_messages(assembled.messages, "prior_call") == []
    assert not any(m.get("tool_call_id") == "prior_call" for m in assembled.messages)
    # No diagnostic event fired.
    assert [e for e in events if e[0] == "loop_result_withheld_provenance"] == []
    await _assert_no_secret_anywhere(assembled.messages)


# ---------------------------------------------------------------------------
# Row 4 (predicate is provenance-is-None ONLY) — out-of-scope but
# provenance-POPULATED entry stays dropped and is NOT sentinel'd
# ---------------------------------------------------------------------------


async def test_out_of_scope_populated_provenance_entry_is_not_sentineled() -> None:
    store = InMemorySessionStore()
    # Real, determined provenance that is OUT of a narrow scope -> dropped by D44,
    # but provenance is NOT None, so it must NOT be sentinel'd.
    await store.append_trail_entry(
        "sess-1",
        _entry(
            "oos_call",
            frozenset({(_P, "Amount")}),
            turn_index=0,
            status="ok",
            args={"sql": f"SELECT Amount, {_SECRET} FROM payroll"},
            result_preview=_secret_preview(),
        ),
    )
    events, observer = _events_collector()
    assembler = ContextAssembler(store)
    narrow = frozenset({f"{_E}.EmployeeCode"})
    assembled = await assembler.assemble(
        "sess-1", narrow, current_turn_index=0, withheld_call_ids=set(), observer=observer
    )

    # Dropped (out of scope) AND not sentinel'd (predicate is None-only).
    assert not any(m.get("tool_call_id") == "oos_call" for m in assembled.messages)
    assert _sentinel_messages(assembled.messages, "oos_call") == []
    assert [e for e in events if e[0] == "loop_result_withheld_provenance"] == []
    assert assembled.dropped_by_scope_count == 1
    await _assert_no_secret_anywhere(assembled.messages)


# ---------------------------------------------------------------------------
# Row 6/7 — diagnostic event fires with the right non-sensitive payload,
# de-duped once per tool_call_id per turn across rebuilds
# ---------------------------------------------------------------------------


async def test_event_payload_shape_runquery() -> None:
    store = InMemorySessionStore()
    await store.append_trail_entry(
        "sess-1",
        _entry("call_rq", None, tool_name="runQuery", args={"sql": f"SELECT {_SECRET}"}, turn_index=2),
    )
    events, observer = _events_collector()
    assembler = ContextAssembler(store)
    await assembler.assemble(
        "sess-1", frozenset(), current_turn_index=2, withheld_call_ids=set(), observer=observer
    )

    withheld = [e for e in events if e[0] == "loop_result_withheld_provenance"]
    assert len(withheld) == 1
    payload = withheld[0][1]
    assert payload == {
        "tool_name": "runQuery",
        "turn_index": 2,
        "tool_call_id": "call_rq",
        "blueprint_id": None,
        "reason": "provenance_undetermined",
    }
    # PII-safe: no data-bearing bytes anywhere in the payload.
    assert _SECRET not in json.dumps(payload, default=str)


async def test_event_payload_carries_blueprint_id_on_runblueprint_path() -> None:
    store = InMemorySessionStore()
    await store.append_trail_entry(
        "sess-1",
        _entry(
            "call_bp",
            None,
            tool_name="runBlueprint",
            args={"id": "bp-overtime-by-department", "slot_bindings": {"x": _SECRET}},
            turn_index=0,
        ),
    )
    events, observer = _events_collector()
    assembler = ContextAssembler(store)
    await assembler.assemble(
        "sess-1", frozenset(), current_turn_index=0, withheld_call_ids=set(), observer=observer
    )

    payload = [e for e in events if e[0] == "loop_result_withheld_provenance"][0][1]
    assert payload["tool_name"] == "runBlueprint"
    assert payload["blueprint_id"] == "bp-overtime-by-department"
    assert payload["reason"] == "provenance_undetermined"
    # The slot binding secret must NOT ride along in the payload.
    assert _SECRET not in json.dumps(payload, default=str)


async def test_event_deduped_once_per_tool_call_id_per_turn_across_rebuilds() -> None:
    store = InMemorySessionStore()
    await store.append_trail_entry("sess-1", _entry("call_dup", None, turn_index=0))
    events, observer = _events_collector()
    assembler = ContextAssembler(store)

    withheld_call_ids: set[str] = set()
    # Three round-trip rebuilds within the SAME turn, sharing the turn-local memo.
    for _ in range(3):
        assembled = await assembler.assemble(
            "sess-1",
            frozenset(),
            current_turn_index=0,
            withheld_call_ids=withheld_call_ids,
            observer=observer,
        )
        # The sentinel is injected on EVERY rebuild (loop-break is unconditional)...
        assert len(_sentinel_messages(assembled.messages, "call_dup")) == 1

    # ...but the diagnostic event fired AT MOST once for that tool_call_id.
    withheld = [e for e in events if e[0] == "loop_result_withheld_provenance"]
    assert len(withheld) == 1


async def test_event_fires_once_per_distinct_stranded_call() -> None:
    store = InMemorySessionStore()
    await store.append_trail_entry("sess-1", _entry("call_a", None, turn_index=0))
    await store.append_trail_entry("sess-1", _entry("call_b", None, turn_index=0))
    events, observer = _events_collector()
    assembler = ContextAssembler(store)

    await assembler.assemble(
        "sess-1", frozenset(), current_turn_index=0, withheld_call_ids=set(), observer=observer
    )
    ids = {e[1]["tool_call_id"] for e in events if e[0] == "loop_result_withheld_provenance"}
    assert ids == {"call_a", "call_b"}


# ---------------------------------------------------------------------------
# Row 7 — idempotent injection: rebuilds never accumulate duplicate sentinels
# ---------------------------------------------------------------------------


async def test_repeated_assemble_does_not_accumulate_duplicate_sentinels() -> None:
    store = InMemorySessionStore()
    await store.append_trail_entry("sess-1", _entry("call_dup", None, turn_index=0))
    assembler = ContextAssembler(store)

    withheld_call_ids: set[str] = set()
    for _ in range(4):
        assembled = await assembler.assemble(
            "sess-1", frozenset(), current_turn_index=0, withheld_call_ids=withheld_call_ids
        )
        # Exactly one sentinel message every rebuild — never 2, 3, 4...
        all_tool_msgs = [m for m in assembled.messages if m.get("tool_call_id") == "call_dup"]
        assert len(all_tool_msgs) == 1
        assert all_tool_msgs[0]["content"] == EXPECTED_SENTINEL


# ---------------------------------------------------------------------------
# Row 11 / discriminator safety — a NORMAL ok in-scope entry still renders as
# the ordinary JSON-wrapped tool result, NOT the verbatim sentinel
# ---------------------------------------------------------------------------


async def test_normal_ok_entry_renders_as_json_not_sentinel() -> None:
    store = InMemorySessionStore()
    normal = _entry(
        "call_normal",
        frozenset({(_E, "Department")}),
        tool_name="runQuery",
        args={"sql": "SELECT Department FROM employee"},
        turn_index=0,
        status="ok",
        result_preview=ResultPreview(
            columns=["Department"], row_count=1, truncated=False, preview_rows=[["Sales"]]
        ),
    )
    await store.append_trail_entry("sess-1", normal)

    assembler = ContextAssembler(store)
    scope = frozenset({f"{_E}.Department"})
    assembled = await assembler.assemble("sess-1", scope, current_turn_index=0)

    # In the render shape, a normal entry has NO literal `content` key (only the
    # sentinel sets one — that is the discriminator).
    msg = next(m for m in assembled.messages if m.get("tool_call_id") == "call_normal")
    assert "content" not in msg
    assert msg.get("status") == "ok"

    # After canonicalization, its tool result is JSON-wrapped, NOT the sentinel.
    canonical = _assembled_to_canonical(assembled.messages)
    tool_msg = next(
        c for c in canonical if c.get("role") == "tool" and c.get("tool_call_id") == "call_normal"
    )
    assert tool_msg["content"] != EXPECTED_SENTINEL
    parsed = json.loads(tool_msg["content"])
    assert parsed["status"] == "ok"
    assert parsed["result_preview"]["preview_rows"] == [["Sales"]]


async def test_mixed_trail_normal_survives_and_stranded_gets_sentinel() -> None:
    """A single turn with one in-scope normal entry and one stranded ok+None
    entry: the normal one renders as JSON, the stranded one as the sentinel, and
    the two occupy distinct tool_call_id slots (no cross-contamination)."""
    store = InMemorySessionStore()
    await store.append_trail_entry(
        "sess-1",
        _entry(
            "call_ok",
            frozenset({(_E, "Department")}),
            args={"sql": "SELECT Department FROM employee"},
            turn_index=0,
            result_preview=ResultPreview(
                columns=["Department"], row_count=1, truncated=False, preview_rows=[["Sales"]]
            ),
        ),
    )
    # SECRET only in the stranded entry's RESULT payload (benign SQL args, which
    # FIX 1 now legitimately replays for correlation).
    await store.append_trail_entry(
        "sess-1",
        _entry("call_none", None, args={"sql": "SELECT Amount FROM payroll"}, turn_index=0,
               result_preview=_secret_preview()),
    )

    assembler = ContextAssembler(store)
    scope = frozenset({f"{_E}.Department"})
    assembled = await assembler.assemble("sess-1", scope, current_turn_index=0)

    canonical = _assembled_to_canonical(assembled.messages)
    ok_tool = next(c for c in canonical if c.get("role") == "tool" and c.get("tool_call_id") == "call_ok")
    none_tool = next(c for c in canonical if c.get("role") == "tool" and c.get("tool_call_id") == "call_none")
    assert json.loads(ok_tool["content"])["status"] == "ok"
    assert none_tool["content"] == EXPECTED_SENTINEL
    # Every assistant tool_call has a matching tool result (valid API pairing).
    _assert_valid_tool_pairing(canonical)
    await _assert_no_secret_anywhere(assembled.messages)


async def test_multicall_turn_withheld_call_keeps_its_args_for_correlation() -> None:
    """FIX 1 regression guard — the loop-break correlation fix.

    A single turn with TWO distinct runQuery calls: one strands (ok+None), one is
    in scope (ok+provenance). The stranded call's synthesized ASSISTANT-side
    `tool_call` must replay its OWN original SQL (not `{}`), so the model can map
    the "do not retry the identical call" marker to the exact call that stranded
    in a multi-call turn; its paired tool result is the byte-exact sentinel; and
    the in-scope call still renders as an ordinary JSON tool result. The stranded
    call's RESULT payload still never surfaces."""
    store = InMemorySessionStore()
    stranded_sql = "SELECT Amount FROM payroll WHERE year = 2026"
    in_scope_sql = "SELECT Department FROM employee"
    # Stranded ok+None — secret ONLY in the result payload, real SQL in args.
    await store.append_trail_entry(
        "sess-1",
        _entry(
            "call_stranded",
            None,
            tool_name="runQuery",
            args={"sql": stranded_sql},
            turn_index=0,
            status="ok",
            result_preview=_secret_preview(),
        ),
    )
    # In-scope ok+provenance — renders normally.
    await store.append_trail_entry(
        "sess-1",
        _entry(
            "call_ok",
            frozenset({(_E, "Department")}),
            tool_name="runQuery",
            args={"sql": in_scope_sql},
            turn_index=0,
            status="ok",
            result_preview=ResultPreview(
                columns=["Department"], row_count=1, truncated=False, preview_rows=[["Sales"]]
            ),
        ),
    )

    assembler = ContextAssembler(store)
    scope = frozenset({f"{_E}.Department"})
    assembled = await assembler.assemble("sess-1", scope, current_turn_index=0)
    canonical = _assembled_to_canonical(assembled.messages)

    # The stranded call's ASSISTANT tool_call replays its OWN SQL (correlation).
    stranded_assistant = next(
        c
        for c in canonical
        if c.get("role") == "assistant"
        and c.get("tool_calls")
        and c["tool_calls"][0]["id"] == "call_stranded"
    )
    replayed_args = json.loads(stranded_assistant["tool_calls"][0]["function"]["arguments"])
    assert replayed_args == {"sql": stranded_sql}
    assert replayed_args != {}

    # Its paired tool result is the byte-exact sentinel.
    stranded_tool = next(
        c for c in canonical if c.get("role") == "tool" and c.get("tool_call_id") == "call_stranded"
    )
    assert stranded_tool["content"] == EXPECTED_SENTINEL

    # The in-scope call still renders as an ordinary JSON tool result.
    ok_assistant = next(
        c
        for c in canonical
        if c.get("role") == "assistant"
        and c.get("tool_calls")
        and c["tool_calls"][0]["id"] == "call_ok"
    )
    assert json.loads(ok_assistant["tool_calls"][0]["function"]["arguments"]) == {"sql": in_scope_sql}
    ok_tool = next(
        c for c in canonical if c.get("role") == "tool" and c.get("tool_call_id") == "call_ok"
    )
    assert ok_tool["content"] != EXPECTED_SENTINEL
    assert json.loads(ok_tool["content"])["result_preview"]["preview_rows"] == [["Sales"]]

    # Valid pairing, and the stranded RESULT payload never surfaced.
    _assert_valid_tool_pairing(canonical)
    await _assert_no_secret_anywhere(assembled.messages)


def _assert_valid_tool_pairing(canonical: list[dict[str, Any]]) -> None:
    """Every assistant `tool_call` id has exactly one matching `tool` result and
    vice-versa (the OpenAI API invariant the sentinel exists to preserve)."""
    assistant_ids: list[str] = []
    for m in canonical:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            assistant_ids.extend(tc["id"] for tc in m["tool_calls"])
    tool_ids = [m["tool_call_id"] for m in canonical if m.get("role") == "tool"]
    assert sorted(assistant_ids) == sorted(tool_ids)
    assert len(tool_ids) == len(set(tool_ids))  # no duplicate tool results
