"""Unit tests for context/budget.py — D46/D50 compaction + preview rendering (Layer 1)."""

from __future__ import annotations

from data_agent.runtime.context.budget import (
    _SUMMARY_CONTEXT_PREFIX,
    SummaryCache,
    compact_trail,
    render_messages,
)
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


def test_overflow_is_compacted_newest_verbatim() -> None:
    # Each entry costs a nontrivial number of tokens; force a tiny budget so only
    # the newest entry survives verbatim.
    entries = [_entry(f"c{i}", f"SELECT {i} FROM some_wide_table_name_padding") for i in range(5)]
    result = compact_trail(entries, token_budget=1, scope_hash="h1")
    # At least the newest entry always survives verbatim (progress guarantee).
    assert result.verbatim[-1] == entries[-1]
    assert result.summary_text is not None
    seen_ids = {e.tool_call_id for e in result.summarized} | {
        e.tool_call_id for e in result.verbatim
    }
    assert seen_ids == {e.tool_call_id for e in entries}


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
