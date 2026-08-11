"""Unit tests for context/budget.py — D46/D50 compaction + preview rendering (Layer 1)."""

from __future__ import annotations

from data_agent.runtime.context.budget import (
    _SUMMARY_CONTEXT_PREFIX,
    SummaryCache,
    _render_entry,
    compact_trail,
    estimate_message_tokens,
    fit_request_to_budget,
    render_messages,
)
from data_agent.runtime.dispatch.denial_mapping import classify_denial
from data_agent.runtime.retrieval.render import _USER_CONTEXT_PREFIX as _RETRIEVAL_CONTEXT_PREFIX
from data_agent.runtime.session.models import ResultPreview, TrailEntry


def _entry(tool_call_id: str, sql: str, preview_rows: list[list] | None = None) -> TrailEntry:
    preview = (
        ResultPreview(
            columns=["a"],
            row_count=len(preview_rows) if preview_rows else 0,
            truncated=False,
            preview_rows=preview_rows or [],
        )
        if preview_rows is not None
        else None
    )
    return TrailEntry(
        turn_index=0,
        tool_call_id=tool_call_id,
        tool_name="runQuery",
        args={"sql": sql},
        status="ok",
        error_code=None,
        provenance=frozenset(),
        result_preview=preview,
        result_full_ref=None,
        ts="2026-07-01T00:00:00+00:00",
    )


def test_small_trail_fits_entirely_verbatim_no_compaction() -> None:
    entries = [_entry("c1", "SELECT 1"), _entry("c2", "SELECT 2")]
    result = compact_trail(entries, token_budget=10_000, scope_hash="h1")
    assert result.verbatim == entries
    assert result.summarized == []
    assert result.summary_text is None


def test_overflow_keeps_recent_verbatim_and_summarizes_older() -> None:
    # A budget large enough for a few newest entries but not all: the newest
    # survive verbatim (they each fit under the budget), the oldest overflow into
    # the summary, and no entry is lost.
    entries = [_entry(f"c{i}", f"SELECT {i} FROM some_wide_table_name_padding") for i in range(8)]
    result = compact_trail(entries, token_budget=200, scope_hash="h1")
    # The newest entry survives verbatim (it fits well under the budget).
    assert result.verbatim[-1] == entries[-1]
    # Overflow genuinely happened: some older entries were summarized, not all kept.
    assert result.summarized
    assert len(result.verbatim) < len(entries)
    assert result.summary_text is not None
    seen_ids = {e.tool_call_id for e in result.summarized} | {
        e.tool_call_id for e in result.verbatim
    }
    assert seen_ids == {e.tool_call_id for e in entries}


def test_single_over_budget_entry_is_summarized_not_forced_verbatim() -> None:
    """Part 3 fix: when the NEWEST entry ALONE exceeds `token_budget` it must NOT
    be force-kept verbatim (the old `if verbatim` guard admitted it regardless of
    size, so one un-truncated ~30k getTableSchema survived verbatim and blew the
    budget). It now falls into `summarized` — kept in a bounded form, not verbatim."""
    giant_sql = "SELECT " + ("padding_col, " * 4000)  # far over any tiny budget
    entry = _entry("giant", giant_sql)
    result = compact_trail([entry], token_budget=50, scope_hash="h1")
    # NOT kept verbatim — the whole point of the fix.
    assert result.verbatim == []
    # ...but still kept in a bounded form (folded into the summary).
    assert [e.tool_call_id for e in result.summarized] == ["giant"]
    assert result.summary_text is not None


def test_over_budget_newest_does_not_drag_in_older_verbatim() -> None:
    """With a giant NEWEST entry over budget, the walk stops at it (newest-first),
    so nothing is force-kept verbatim and the older small entry is summarized too —
    the compacted verbatim set is bounded by the budget (here: empty)."""
    older_small = _entry("old", "SELECT 1")
    giant_new = _entry("new", "SELECT " + ("x, " * 4000))
    result = compact_trail([older_small, giant_new], token_budget=50, scope_hash="h1")
    assert result.verbatim == []
    assert {e.tool_call_id for e in result.summarized} == {"old", "new"}


def test_sql_preserved_verbatim_for_kept_entries() -> None:
    entries = [_entry("c1", "SELECT very_specific_column FROM table_x")]
    result = compact_trail(entries, token_budget=10_000, scope_hash="h1")
    assert result.verbatim[0].args["sql"] == "SELECT very_specific_column FROM table_x"


def test_summarizer_receives_full_entries_including_sql() -> None:
    captured: list = []

    def spy_summarizer(entries):
        captured.extend(entries)
        return "summary"

    entries = [_entry(f"c{i}", f"SELECT {i}") for i in range(5)]
    compact_trail(entries, token_budget=1, scope_hash="h1", summarizer=spy_summarizer)
    # Whatever got compacted must have been handed to the summarizer with SQL intact.
    for e in captured:
        assert e.args["sql"].startswith("SELECT")


def test_cache_miss_then_hit() -> None:
    cache = SummaryCache()
    calls = {"n": 0}

    def counting_summarizer(entries):
        calls["n"] += 1
        return f"summary-{calls['n']}"

    entries = [_entry(f"c{i}", f"SELECT {i} FROM padding_table_name") for i in range(5)]

    first = compact_trail(
        entries, token_budget=1, scope_hash="h1", summarizer=counting_summarizer, cache=cache
    )
    assert first.cache_hit is False
    assert calls["n"] == 1

    second = compact_trail(
        entries, token_budget=1, scope_hash="h1", summarizer=counting_summarizer, cache=cache
    )
    assert second.cache_hit is True
    assert second.summary_text == first.summary_text
    assert calls["n"] == 1  # summarizer NOT called again on cache hit


def test_cache_miss_for_different_scope_hash() -> None:
    cache = SummaryCache()
    entries = [_entry(f"c{i}", f"SELECT {i} FROM padding_table_name") for i in range(5)]

    first = compact_trail(entries, token_budget=1, scope_hash="scope-a", cache=cache)
    second = compact_trail(entries, token_budget=1, scope_hash="scope-b", cache=cache)
    assert first.cache_hit is False
    assert second.cache_hit is False  # different scope_hash -> different cache key


def test_render_messages_preview_truncated_flag() -> None:
    entry = _entry("c1", "SELECT * FROM big_table", preview_rows=[["r1"], ["r2"], ["r3"]])
    result = compact_trail([entry], token_budget=10_000, scope_hash="h1")
    messages = render_messages(result, preview_row_count=2)
    tool_msg = next(m for m in messages if m.get("tool_call_id") == "c1")
    assert tool_msg["result_preview"]["truncated"] is True
    assert len(tool_msg["result_preview"]["preview_rows"]) == 2


def test_render_messages_no_truncation_when_preview_fits() -> None:
    entry = _entry("c1", "SELECT * FROM small_table", preview_rows=[["r1"]])
    result = compact_trail([entry], token_budget=10_000, scope_hash="h1")
    messages = render_messages(result, preview_row_count=20)
    tool_msg = next(m for m in messages if m.get("tool_call_id") == "c1")
    assert tool_msg["result_preview"]["truncated"] is False
    assert len(tool_msg["result_preview"]["preview_rows"]) == 1


def test_render_messages_summary_prepended() -> None:
    entries = [_entry(f"c{i}", f"SELECT {i} FROM padding_table_name") for i in range(5)]
    result = compact_trail(entries, token_budget=1, scope_hash="h1")
    messages = render_messages(result)
    # The summary is a NON-system (`user`) message so it never competes with the
    # base prompt; the raw summary text is preserved, only prefixed as prior-context.
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == _SUMMARY_CONTEXT_PREFIX + result.summary_text


def test_render_messages_never_exposes_more_than_preview() -> None:
    """budget.py must never surface anything beyond the stored preview (D46 preview-only)."""
    entry = _entry("c1", "SELECT * FROM huge_table", preview_rows=[["r1"], ["r2"]])
    result = compact_trail([entry], token_budget=10_000, scope_hash="h1")
    messages = render_messages(result, preview_row_count=100)
    tool_msg = next(m for m in messages if m.get("tool_call_id") == "c1")
    # Only the two rows that were ever stored are ever exposed - no way to grow back.
    assert len(tool_msg["result_preview"]["preview_rows"]) == 2


def test_render_entry_includes_static_denial_user_message_for_non_ok_status() -> None:
    """S4: a non-`"ok"` entry's rendered content includes the static,
    PII-safe denial message re-derived from `error_code` (never persisted on
    `TrailEntry` itself — see `dispatch/denial_mapping.py::classify_denial`),
    so a model that DOES see this entry (e.g. a caller other than
    `ContextAssembler`, which currently drops denied entries entirely — see
    `tests/runtime/loop/test_agent_loop.py`'s S4 note) has a reason to
    self-correct instead of a bare `error_code`."""
    entry = TrailEntry(
        turn_index=0,
        tool_call_id="c1",
        tool_name="runQuery",
        args={"sql": "SELECT bad"},
        status="denied",
        error_code="CLICKHOUSE_QUERY_ERROR",
        provenance=None,
        result_preview=None,
        result_full_ref=None,
        ts="2026-07-01T00:00:00+00:00",
    )
    result = compact_trail([entry], token_budget=10_000, scope_hash="h1")
    messages = render_messages(result)
    tool_msg = next(m for m in messages if m.get("tool_call_id") == "c1")
    assert tool_msg["user_message"] == "That query didn't run correctly. Let me fix it and try again."


def test_render_entry_resolve_values_unknown_target_uses_specific_message() -> None:
    """L5: a replayed `resolveValues` RESOLVE_VALUES_UNKNOWN_TARGET trail entry
    renders the composite code's crafted, actionable message (from the denial
    table), NOT the generic "Something went wrong processing that request."
    fallback — preserving the `retryable=True` self-correction rationale even
    though `user_message` is not persisted on `TrailEntry`."""
    entry = TrailEntry(
        turn_index=0,
        tool_call_id="c1",
        tool_name="resolveValues",
        args={"table": "dbpcm_warehouse.accrual_events", "column": "Nope", "concept": "x"},
        status="error",
        error_code="RESOLVE_VALUES_UNKNOWN_TARGET",
        provenance=None,
        result_preview=None,
        result_full_ref=None,
        ts="2026-07-01T00:00:00+00:00",
    )
    result = compact_trail([entry], token_budget=10_000, scope_hash="h1")
    messages = render_messages(result)
    tool_msg = next(m for m in messages if m.get("tool_call_id") == "c1")
    assert tool_msg["user_message"] == (
        "That table or column isn't available. Check the exact name with "
        "getTableSchema and try again."
    )
    assert tool_msg["user_message"] != "Something went wrong processing that request."


def test_render_entry_user_message_is_none_for_ok_status() -> None:
    entry = _entry("c1", "SELECT 1")
    result = compact_trail([entry], token_budget=10_000, scope_hash="h1")
    messages = render_messages(result)
    tool_msg = next(m for m in messages if m.get("tool_call_id") == "c1")
    assert tool_msg["user_message"] is None


# ---------------------------------------------------------------------------
# fit_request_to_budget — total-request token budget (2026-08 fix).
# ---------------------------------------------------------------------------


def _sys(content: str) -> dict:
    return {"role": "system", "content": content}


def _user(content: str) -> dict:
    return {"role": "user", "content": content}


def _assistant_answer(content: str) -> dict:
    return {"role": "assistant", "content": content}


def _tool_pair(call_id: str, sql: str, result: str) -> list[dict]:
    """One [assistant(tool_calls), tool] canonical pair, as the loop emits them."""
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "runQuery", "arguments": f'{{"sql": "{sql}"}}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": result},
    ]


def _assert_pairing_intact(messages: list[dict]) -> None:
    """Every `tool` message has an immediately-announcing assistant `tool_calls`,
    and no assistant `tool_calls` message is left without its `tool` result(s)."""
    open_ids: set[str] = set()
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                open_ids.add(tc["id"])
        elif m["role"] == "tool":
            assert m["tool_call_id"] in open_ids, (
                f"orphan tool message {m['tool_call_id']} without a preceding tool_calls"
            )
            open_ids.discard(m["tool_call_id"])
    assert not open_ids, f"assistant tool_calls left without a tool result: {open_ids}"


def test_fit_no_trim_when_under_budget() -> None:
    messages = [_sys("base"), *_tool_pair("c1", "SELECT 1", "r1"), _user("q?")]
    result = fit_request_to_budget(messages, token_budget=100_000)
    assert result.messages == messages
    assert result.dropped_messages == 0
    assert result.dropped_units == 0


def test_fit_pins_base_prompt_and_current_question_dropping_oldest() -> None:
    base = _sys("BASE PROMPT " + "x" * 400)
    question = _user("current question?")
    # Several fat tool pairs, oldest first.
    pairs: list[dict] = []
    for i in range(10):
        pairs.extend(_tool_pair(f"c{i}", f"SELECT {i} " + "col, " * 200, "row " * 200))
    messages = [base, *pairs, question]
    total = sum(estimate_message_tokens(m) for m in messages)
    budget = total // 2  # force trimming

    result = fit_request_to_budget(messages, token_budget=budget)

    # Invariant 1: base prompt is still byte-identical at index 0.
    assert result.messages[0] == base
    # Invariant 3: the current question is still present (and last).
    assert result.messages[-1] == question
    # Invariant 2: fits the budget.
    assert sum(estimate_message_tokens(m) for m in result.messages) <= budget
    # Invariant 4: pairing intact.
    _assert_pairing_intact(result.messages)
    # Invariant 5: OLDEST dropped — c0 gone, the most recent c9 survived.
    surviving_ids = {
        tc["id"]
        for m in result.messages
        if m["role"] == "assistant" and m.get("tool_calls")
        for tc in m["tool_calls"]
    }
    assert "c0" not in surviving_ids
    assert "c9" in surviving_ids
    assert result.dropped_units > 0


def test_fit_drops_a_tool_pair_as_a_unit_never_orphaning() -> None:
    base = _sys("base")
    question = _user("q?")
    messages = [base, *_tool_pair("c1", "SELECT " + "a, " * 500, "x" * 4000), question]
    budget = estimate_message_tokens(base) + estimate_message_tokens(question) + 5

    result = fit_request_to_budget(messages, token_budget=budget)

    # The whole pair went (both messages), never a lone orphan tool message.
    assert result.messages == [base, question]
    assert result.dropped_units == 1
    assert result.dropped_messages == 2
    _assert_pairing_intact(result.messages)


def test_fit_never_drops_base_or_question_even_if_they_exceed_budget() -> None:
    # Degenerate corner: base + question alone exceed a tiny budget. They must
    # STILL survive (invariants 1 + 3 outrank invariant 2).
    base = _sys("BASE " + "x" * 2000)
    question = _user("Q " + "y" * 2000)
    messages = [base, *_tool_pair("c1", "SELECT 1", "r"), question]
    result = fit_request_to_budget(messages, token_budget=1)
    assert result.messages[0] == base
    assert result.messages[-1] == question
    _assert_pairing_intact(result.messages)


def test_fit_no_user_message_never_orphans_a_trailing_tool_pair() -> None:
    """SHOULD-FIX 1: a list ending in a tool PAIR with NO user message anywhere
    must never pin the trailing `tool` while its announcing assistant unit stays
    droppable (which would orphan the tool → API 400). The no-user-message branch
    pins nothing as the tail; the whole pair is one droppable unit."""
    base = _sys("base")
    messages = [base, *_tool_pair("c1", "SELECT " + "a, " * 800, "x" * 6000)]
    budget = estimate_message_tokens(base) + 5  # only the base fits

    result = fit_request_to_budget(messages, token_budget=budget)

    # The over-budget pair went as a whole unit — no orphan tool left behind.
    assert result.messages == [base]
    _assert_pairing_intact(result.messages)
    assert result.dropped_by_kind == {"trail": 1}


def test_fit_priority_keeps_current_turn_retrieval_over_stale_conversation() -> None:
    """SHOULD-FIX 2: under moderate pressure the fit drops stale CONVERSATION and
    old TRAIL first and keeps THIS question's retrieval-cards block (and the
    compaction summary) longer — they carry the blueprint candidates / knowledge /
    access rules that matter exactly when context is tight."""
    base = _sys("base")
    question = _user("current question?")
    # A stale prior-turn conversation exchange + an old trail pair, both fat, plus
    # this turn's (smaller) retrieval + summary context blocks.
    stale_user = _user("stale old question " + "w" * 1500)
    stale_answer = _assistant_answer("stale old answer " + "z" * 1500)
    old_trail = _tool_pair("old", "SELECT " + "c, " * 300, "r" * 1500)
    retrieval = _user(_RETRIEVAL_CONTEXT_PREFIX + "candidate blueprint bp.headcount")
    summary = _user(_SUMMARY_CONTEXT_PREFIX + "earlier steps summarized")
    messages = [base, stale_user, stale_answer, *old_trail, retrieval, summary, question]

    # Budget that forces dropping the stale conversation + old trail but leaves
    # room for base + retrieval + summary + question.
    keep_tokens = (
        estimate_message_tokens(base)
        + estimate_message_tokens(retrieval)
        + estimate_message_tokens(summary)
        + estimate_message_tokens(question)
    )
    budget = keep_tokens + 5

    result = fit_request_to_budget(messages, token_budget=budget)

    contents = [m.get("content") for m in result.messages]
    # The current-turn retrieval + summary survived...
    assert retrieval["content"] in contents
    assert summary["content"] in contents
    # ...while the stale conversation + old trail were dropped (tier-0 first).
    assert stale_user["content"] not in contents
    assert stale_answer["content"] not in contents
    assert "old" not in {
        tc["id"]
        for m in result.messages
        if m["role"] == "assistant" and m.get("tool_calls")
        for tc in m["tool_calls"]
    }
    # Base + question still pinned; pairing intact; by-kind breakdown reflects it.
    assert result.messages[0] == base
    assert result.messages[-1] == question
    _assert_pairing_intact(result.messages)
    assert result.dropped_by_kind.get("trail") == 1
    assert result.dropped_by_kind.get("conversation") == 2
    assert "retrieval" not in result.dropped_by_kind
    assert "summary" not in result.dropped_by_kind


def test_fit_pins_current_turn_retrieval_and_summary_even_under_heavy_pressure() -> None:
    """The current-turn retrieval + summary blocks are now PINNED as part of the
    current-turn tail (they sit after `_current_turn_start`, the interleave blocker
    fix), so under heavy pressure ONLY the PRIOR trail is dropped — the retrieval +
    summary + question survive even when base + question alone would exhaust the
    budget (the pinned-corner: invariants 1/3/6 outrank strict budget compliance)."""
    base = _sys("base")
    question = _user("q?")
    retrieval = _user(_RETRIEVAL_CONTEXT_PREFIX + "cards " + "k" * 400)
    summary = _user(_SUMMARY_CONTEXT_PREFIX + "summary " + "s" * 400)
    old_trail = _tool_pair("old", "SELECT 1", "r")
    messages = [base, *old_trail, retrieval, summary, question]
    # A budget so tight only base + question would "fit" — proving the current-turn
    # retrieval/summary are pinned (kept) rather than dropped as a last resort.
    budget = estimate_message_tokens(base) + estimate_message_tokens(question) + 2

    result = fit_request_to_budget(messages, token_budget=budget)

    # The PRIOR trail pair is dropped (tier 0); the current-turn retrieval + summary
    # + question are pinned and survive.
    assert result.messages == [base, retrieval, summary, question]
    _assert_pairing_intact(result.messages)
    assert result.dropped_by_kind.get("trail") == 1
    assert "retrieval" not in result.dropped_by_kind
    assert "summary" not in result.dropped_by_kind


def test_fit_current_turn_older_tool_pairs_are_last_resort_droppable_tier2() -> None:
    """Interleave blocker fix: the CURRENT turn's tool pairs sit AFTER the question
    (their `ts` follows it), so they are in the current-turn tail. They must NOT be
    an un-trimmable pinned tail: the most-recent K (default 3) are pinned, but OLDER
    current-turn pairs are a last-resort droppable TIER 2 — dropped only after every
    PRIOR-turn unit, so a runaway single turn still fits the budget."""
    base = _sys("base")
    prior = _tool_pair("prior", "SELECT prior " + "p, " * 200, "r" * 800)
    question = _user("current question?")
    # Four current-turn tool pairs AFTER the question (a oldest ... d newest).
    cur_a = _tool_pair("a", "SELECT a " + "x, " * 200, "r" * 800)
    cur_b = _tool_pair("b", "SELECT b " + "x, " * 200, "r" * 800)
    cur_c = _tool_pair("c", "SELECT c " + "x, " * 200, "r" * 800)
    cur_d = _tool_pair("d", "SELECT d " + "x, " * 200, "r" * 800)
    messages = [base, *prior, question, *cur_a, *cur_b, *cur_c, *cur_d]

    def _surviving_ids(msgs: list[dict]) -> set[str]:
        return {
            tc["id"]
            for m in msgs
            if m["role"] == "assistant" and m.get("tool_calls")
            for tc in m["tool_calls"]
        }

    pair = estimate_message_tokens(cur_a[0]) + estimate_message_tokens(cur_a[1])
    base_q = estimate_message_tokens(base) + estimate_message_tokens(question)

    # (1) Moderate pressure: room for base + question + all four current pairs but
    # NOT the prior pair → the PRIOR pair drops first (tier 0), every current pair
    # (incl. the older `a`) survives — prior turns go before current-turn tools.
    result = fit_request_to_budget(messages, token_budget=base_q + 4 * pair + 5)
    ids = _surviving_ids(result.messages)
    assert "prior" not in ids
    assert {"a", "b", "c", "d"} <= ids
    assert result.messages[0] == base
    assert result.messages[1] == question  # question pinned right after base
    _assert_pairing_intact(result.messages)

    # (2) Heavy pressure: only base + question + K(=3) current pairs fit. The prior
    # pair AND the OLDEST current pair `a` (tier 2) are dropped; the most-recent
    # three (b, c, d) are pinned and survive.
    result2 = fit_request_to_budget(messages, token_budget=base_q + 3 * pair + 5)
    ids2 = _surviving_ids(result2.messages)
    assert "prior" not in ids2
    assert "a" not in ids2  # older current-turn pair trimmed as tier 2...
    assert {"b", "c", "d"} <= ids2  # ...most-recent K=3 pinned survive
    assert sum(estimate_message_tokens(m) for m in result2.messages) <= base_q + 3 * pair + 5
    assert result2.messages[0] == base
    assert any(m.get("content") == "current question?" for m in result2.messages)
    _assert_pairing_intact(result2.messages)

    # (3) A smaller K pins fewer current pairs (the knob is honored).
    result3 = fit_request_to_budget(
        messages, token_budget=base_q + pair + 5, pinned_recent_tool_pairs=1
    )
    ids3 = _surviving_ids(result3.messages)
    assert ids3 == {"d"}  # only the single most-recent current-turn pair pinned
    _assert_pairing_intact(result3.messages)


def test_render_entry_surfaces_authoritative_verified_blueprint_flag() -> None:
    # A verified-blueprint TrailEntry threads its `authoritative` marker into the
    # rendered model-facing dict; a plain runQuery entry omits the key entirely.
    verified = TrailEntry(
        turn_index=0,
        tool_call_id="bp1",
        tool_name="runBlueprint",
        args={"id": "bp.headcount"},
        status="ok",
        error_code=None,
        provenance=frozenset(),
        result_preview=None,
        result_full_ref="ref-1",
        ts="2026-07-01T00:00:00+00:00",
        authoritative=True,
    )
    plain = _entry("c1", "SELECT 1")
    result = compact_trail([verified, plain], token_budget=10_000, scope_hash="h1")
    messages = render_messages(result)
    bp_msg = next(m for m in messages if m.get("tool_call_id") == "bp1")
    query_msg = next(m for m in messages if m.get("tool_call_id") == "c1")
    assert bp_msg["authoritative"] is True
    assert "authoritative" not in query_msg


def test_fit_pins_emulated_discovery_pairs_against_the_tier0_sweep() -> None:
    # Invariant 7. The emulated-discovery pairs are anchored at the SESSION'S FIRST
    # question, so they classify as PRIOR-TURN trail — tier 0, the first thing the
    # drop sweep takes. Dropping them is uniquely harmful: the loop seeds its
    # repeated-idempotent-read guard from the SAME sweep, so a trimmed listing leaves
    # the model unable to see the tables AND unable to re-fetch them (the guard
    # answers "already served"). Pinning by tool_call_id is what prevents that.
    base = _sys("BASE PROMPT " + "x" * 400)
    emulated = [
        *_tool_pair("emulated-listDatabases", "SELECT db", "databases"),
        *_tool_pair("emulated-listTables-dbpcm_warehouse", "SELECT tbl", "tables"),
    ]
    # Fat PRIOR-turn pairs + conversation that must be sacrificed instead.
    prior: list[dict] = []
    for i in range(10):
        prior.extend(_tool_pair(f"c{i}", f"SELECT {i} " + "col, " * 200, "row " * 200))
    question = _user("current question?")
    messages = [base, _user("Q0?"), *emulated, *prior, _assistant_answer("A0."), question]
    total = sum(estimate_message_tokens(m) for m in messages)
    budget = total // 3  # force heavy trimming

    result = fit_request_to_budget(
        messages,
        token_budget=budget,
        pinned_tool_call_ids=frozenset(
            {"emulated-listDatabases", "emulated-listTables-dbpcm_warehouse"}
        ),
    )

    surviving_ids = {
        tc["id"]
        for m in result.messages
        if m["role"] == "assistant" and m.get("tool_calls")
        for tc in m["tool_calls"]
    }
    # THE POINT: both emulated pairs survive heavy pressure...
    assert "emulated-listDatabases" in surviving_ids
    assert "emulated-listTables-dbpcm_warehouse" in surviving_ids
    # ...while the ordinary prior-turn pairs were genuinely sacrificed.
    assert "c0" not in surviving_ids
    assert result.dropped_units > 0
    assert result.messages[0] == base
    assert result.messages[-1] == question
    _assert_pairing_intact(result.messages)


def test_fit_without_pinned_ids_is_unchanged() -> None:
    # The parameter is additive: omitting it (or passing empty) must behave exactly
    # as before it existed — the emulated pairs are then droppable prior-turn trail.
    base = _sys("BASE PROMPT " + "x" * 400)
    emulated = _tool_pair("emulated-listDatabases", "SELECT db " + "x " * 200, "d " * 200)
    prior: list[dict] = []
    for i in range(6):
        prior.extend(_tool_pair(f"c{i}", f"SELECT {i} " + "col, " * 200, "row " * 200))
    question = _user("current question?")
    messages = [base, _user("Q0?"), *emulated, *prior, question]
    budget = sum(estimate_message_tokens(m) for m in messages) // 3

    default = fit_request_to_budget(messages, token_budget=budget)
    explicit_empty = fit_request_to_budget(
        messages, token_budget=budget, pinned_tool_call_ids=frozenset()
    )

    assert default.messages == explicit_empty.messages
    surviving_ids = {
        tc["id"]
        for m in default.messages
        if m["role"] == "assistant" and m.get("tool_calls")
        for tc in m["tool_calls"]
    }
    assert "emulated-listDatabases" not in surviving_ids  # droppable without the pin


# ---------------------------------------------------------------------------
# `denial_detail` — the ONLY channel by which a SPECIFIC denial reason reaches
# the model. `ToolResult.user_message` does not: `TrailEntry` has no field for it,
# and `_render_entry` (the single producer of every model-facing tool message)
# regenerates the text from `error_code` alone. Anything not persisted here is
# invisible to the model, on the live turn and on every rebuild after it.
# ---------------------------------------------------------------------------


def _denied(error_code: str, *, detail: str | None = None) -> TrailEntry:
    return TrailEntry(
        turn_index=0, tool_call_id="c1", tool_name="runQuery", args={},
        status="denied", error_code=error_code, provenance=None,
        result_preview=None, result_full_ref=None, ts="t", denial_detail=detail,
    )


def test_denial_detail_reaches_the_model_verbatim() -> None:
    """The column-naming message the dispatcher works to produce must ARRIVE. Before
    this field it was assigned to `ToolResult.user_message`, dropped at persistence,
    and the model saw only the generic string — so it retried blind against a
    constraint it could not see."""
    detail = "Query references columns outside your permitted scope: employee.AnnualSalary"
    rendered = _render_entry(_denied("COLUMN_SCOPE_VIOLATION", detail=detail), 20)
    assert rendered["user_message"] == detail
    assert "AnnualSalary" in rendered["user_message"]


def test_without_a_detail_the_canned_string_is_unchanged() -> None:
    """An ordinary denial renders byte-identically to before the field existed."""
    rendered = _render_entry(_denied("COLUMN_SCOPE_VIOLATION"), 20)
    assert rendered["user_message"] == (
        classify_denial("COLUMN_SCOPE_VIOLATION").user_message
    )


def test_an_unregistered_code_with_no_detail_is_still_the_generic_failure() -> None:
    """The fallback is intact: a code nobody registered and no detail says nothing
    actionable. This is the failure mode `denial_detail` and the denial-table
    registration exist to avoid — it is silent, not loud."""
    rendered = _render_entry(_denied("SOME_UNREGISTERED_CODE"), 20)
    assert rendered["user_message"] == "Something went wrong processing that request."


def test_denial_detail_is_ignored_on_an_ok_entry() -> None:
    """`user_message` is a non-`ok` concept — a successful entry never carries one,
    detail or not, so a stray value cannot leak into a normal result."""
    entry = TrailEntry(
        turn_index=0, tool_call_id="c1", tool_name="runQuery", args={},
        status="ok", error_code=None, provenance=frozenset(),
        result_preview=None, result_full_ref=None, ts="t",
        denial_detail="should never be shown",
    )
    assert _render_entry(entry, 20)["user_message"] is None


def test_denial_detail_round_trips_through_the_session_document() -> None:
    """It has to survive persistence — that is the entire point of the field."""
    detail = "Query references columns outside your permitted scope: employee.AnnualSalary"
    restored = TrailEntry.from_doc(_denied("COLUMN_SCOPE_VIOLATION", detail=detail).to_doc())
    assert restored.denial_detail == detail


def test_a_legacy_document_without_the_field_loads_unchanged() -> None:
    """Documents written before this field existed must load, with `None`, and render
    exactly as they always did."""
    doc = _denied("COLUMN_SCOPE_VIOLATION", detail="x").to_doc()
    del doc["denial_detail"]
    restored = TrailEntry.from_doc(doc)
    assert restored.denial_detail is None
    assert _render_entry(restored, 20)["user_message"] == (
        classify_denial("COLUMN_SCOPE_VIOLATION").user_message
    )
