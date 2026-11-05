import json
from dataclasses import replace

import pytest

from data_agent.runtime.composite.analysis_state import previous_result_reference
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.context.review_feedback import with_historical_feedback
from data_agent.runtime.context.scope_filter import compute_scope_hash
from data_agent.runtime.loop.agent_loop import _assembled_to_canonical
from data_agent.runtime.loop.answer_judge import JudgeVerdict
from data_agent.runtime.loop.proposal import ReviewState
from data_agent.runtime.session.memory_store import InMemorySessionStore
from tests.runtime.composite.test_previous_result_reuse import SCOPE, original
from tests.runtime.context.test_delivered_answer import message


async def fixture():
    store = InMemorySessionStore()
    entry = original()
    await store.append_trail_entry("s", entry)
    state = ReviewState(scope_hash=compute_scope_hash(SCOPE))
    state.reject(JudgeVerdict(False, "measurement_mismatch", "Missing the required event filter.",
                             repair_type="analysis", result_ids=("prior",)), "v1")
    await store.write_review_state("s", 0, state.to_doc())
    return store, entry, state


async def test_feedback_and_corrected_answer_reach_next_request():
    store, entry, state = await fixture()
    saved = replace(message(), turn_index=0, provenance=entry.provenance)
    await store.append_message("s", saved)
    doc = await store.get_or_create_session("s")
    trail = with_historical_feedback(doc.tool_trail, doc, 1, SCOPE)
    rendered = ContextAssembler(store)._interleave(
        in_scope_messages=[saved], in_scope_trail=trail, raw_trail=trail,
        current_turn_index=1, withheld_call_ids=None, observer=None,
    )
    canonical = _assembled_to_canonical(rendered)
    wire = json.dumps(canonical)
    assert state.feedback in wire
    assert saved.content in wire
    assert "finalizeAnswer" in wire
    assert "reference data" in wire
    assert entry.window_note is None  # Annotation never mutates stored receipts.


@pytest.mark.parametrize("scope", [frozenset(), frozenset({"other.table.column"})])
async def test_feedback_not_replayed_under_changed_scope(scope):
    store, entry, state = await fixture()
    doc = await store.get_or_create_session("s")
    assert with_historical_feedback([entry], doc, 1, scope) == [entry]


async def test_feedback_follows_reused_reference_for_judge_and_salvage():
    store, entry, state = await fixture()
    alias = previous_result_reference(entry, 1, compute_scope_hash(SCOPE))
    await store.append_trail_entry("s", alias)
    doc = await store.get_or_create_session("s")
    annotated = with_historical_feedback(doc.tool_trail, doc, 1, SCOPE)
    assert state.feedback in annotated[-1].window_note
    assert "not freshly executed" in annotated[-1].window_note
    assert with_historical_feedback([entry], doc, 0, SCOPE) == [entry]


def test_rejections_persist_without_losing_earlier_reason():
    state = ReviewState(scope_hash="scope")
    for reason in ("First reason", "Second reason"):
        state.reject(JudgeVerdict(False, "measurement_mismatch", reason,
                                 result_ids=("r",)), reason)
    restored = ReviewState.restore(state.to_doc(), "scope")
    assert [r["feedback"] for r in restored.rejection_history] == ["First reason", "Second reason"]
    assert not ReviewState.restore(state.to_doc(), "changed").rejection_history


async def test_assembly_and_judge_evidence_use_the_same_feedback():
    from tests.runtime.test_harness_improvements import build

    store, entry, state = await fixture()
    alias = previous_result_reference(entry, 1, compute_scope_hash(SCOPE))
    await store.append_trail_entry("s", alias)
    assembled = await ContextAssembler(store).assemble("s", SCOPE, current_turn_index=1)
    canonical = _assembled_to_canonical(assembled.messages)
    assert state.feedback in json.dumps(canonical)
    loop, *_ = build([], store=store)
    results, _, _ = await loop._judge_results("s", 1, SCOPE)
    assert len(results) == 1
    assert results[0]["tool_call_id"] == alias.tool_call_id
    assert state.feedback in results[0]["window_note"]


async def test_legacy_state_keeps_last_rejection_without_fabricating_history():
    store, entry, state = await fixture()
    legacy = state.to_doc()
    legacy.pop("rejection_history")
    await store.write_review_state("s", 0, legacy)
    doc = await store.get_or_create_session("s")
    assert state.feedback in with_historical_feedback([entry], doc, 1, SCOPE)[0].window_note


@pytest.mark.parametrize("legacy", [False, True])
async def test_answer_level_rejection_is_not_copied_to_ten_receipts(legacy):
    store, entry, state = await fixture()
    state.reject(JudgeVerdict(False, "unsupported_by_evidence",
                             "The takeaway claims an unsupported 12% rise.",
                             repair_type="prose", result_ids=()), "v2")
    saved = state.to_doc()
    if legacy:
        saved.pop("rejection_history")
    await store.write_review_state("s", 0, saved)
    for i in range(9):
        await store.append_trail_entry("s", replace(entry, tool_call_id=f"other-{i}",
                                                   tool_name="getTableSchema" if i < 7 else "runQuery"))
    doc = await store.get_or_create_session("s")
    annotated = with_historical_feedback(doc.tool_trail, doc, 1, SCOPE)
    assert len(annotated) == 10
    assert all("12%" not in (e.window_note or "") for e in annotated)
    assert sum(bool(e.window_note) for e in annotated) == (0 if legacy else 1)
    assert doc.review_states["0"]["feedback"] == saved["feedback"]


async def test_a_rejection_naming_an_alias_follows_the_result_into_a_later_turn():
    """Turn 1's judge rejected the reused copy; turn 2 reuses the same origin again and
    must see that feedback, whichever copy it is shown."""
    store = InMemorySessionStore()
    entry = original()
    await store.append_trail_entry("s", entry)
    first = previous_result_reference(entry, 1, compute_scope_hash(SCOPE))
    await store.append_trail_entry("s", first)
    state = ReviewState(scope_hash=compute_scope_hash(SCOPE))
    state.reject(JudgeVerdict(False, "measurement_mismatch", "Wrong population for this period.",
                              repair_type="analysis", result_ids=(first.tool_call_id,)), "v1")
    await store.write_review_state("s", 1, state.to_doc())
    second = previous_result_reference(entry, 2, compute_scope_hash(SCOPE))
    await store.append_trail_entry("s", second)
    doc = await store.get_or_create_session("s")
    notes = {e.tool_call_id: e.window_note or "" for e in with_historical_feedback(doc.tool_trail, doc, 2, SCOPE)}
    assert all("Wrong population" in notes[ref] for ref in (entry.tool_call_id, first.tool_call_id,
                                                              second.tool_call_id))


@pytest.mark.parametrize("state", [None, "rejected", ["v1"], {"rejection_history": [None, 3]}])
async def test_a_malformed_stored_review_state_is_skipped(state):
    store, entry, _ = await fixture()
    doc = await store.get_or_create_session("s")
    doc.review_states["0"] = state
    doc.review_states["not-a-turn"] = {"scope_hash": compute_scope_hash(SCOPE)}
    assert with_historical_feedback([entry], doc, 1, SCOPE) == [entry]


async def test_only_the_latest_rejections_are_attached_and_each_once():
    store = InMemorySessionStore()
    entry = original()
    await store.append_trail_entry("s", entry)
    for turn in range(5):
        state = ReviewState(scope_hash=compute_scope_hash(SCOPE))
        for _ in range(2):  # the same verdict recorded twice in one turn
            state.reject(JudgeVerdict(False, "measurement_mismatch", f"Turn {turn} problem.",
                                      repair_type="analysis", result_ids=("prior",)), "v1")
        await store.write_review_state("s", turn, state.to_doc())
    doc = await store.get_or_create_session("s")
    note = with_historical_feedback(doc.tool_trail, doc, 5, SCOPE)[0].window_note
    attached = json.loads(note[note.index("["):])
    assert [r["turn_index"] for r in attached] == [2, 3, 4]
    assert note.count("Turn 4 problem.") == 1


@pytest.mark.parametrize("ids", [("q",), ()])
async def test_a_follow_up_reusing_a_result_rejected_earlier_ships_its_approval(ids):
    """Turn 1 shows a result turn 0's judge had rejected once. The judge reads the stored
    feedback on that result; delivery must not then see a different answer version."""
    from tests.runtime.test_answer_document import section
    from tests.runtime.test_harness_improvements import (
        Judge,
        batch,
        build,
        call,
        discovery,
        query,
        run,
    )

    judge = Judge([
        JudgeVerdict(False, "contradicts_result", "Say 120, not 125.", True, repair_type="analysis", result_ids=ids),
        JudgeVerdict(True, reviewed=True),
        JudgeVerdict(True, reviewed=True),
    ])
    rows = {"columns": ["Department", "n"], "rows": [["Sales", 120]], "row_count": 1, "truncated": False}
    loop, *_ = build(
        [discovery(), query(),
         batch(call("finalizeAnswer", "a1", takeaway="There are 125.", sections=[section()], evidence=["q"])),
         batch(call("finalizeAnswer", "a2", takeaway="There are 120.", sections=[section()], evidence=["q"])),
         batch(call("finalizeAnswer", "a3", takeaway="Same counts.", sections=[section()], evidence=["q"]))],
        judge, rows=[rows],
    )
    assert (await run(loop)).review["status"] == "approved"
    second = await run(loop, "Show it again.")
    assert second.review["status"] == "approved", second.review
    assert len(judge.briefs) == 3
    if ids:
        assert any("Say 120, not 125." in (r.get("window_note") or "") for r in judge.briefs[-1].results)
