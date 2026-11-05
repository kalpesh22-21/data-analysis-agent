"""Explicit historical evidence reuse without a database execution or scope bypass."""

from dataclasses import replace

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import UpdateAnalysisStateTool
from data_agent.runtime.context.budget import render_entry
from data_agent.runtime.context.scope_filter import compute_scope_hash
from data_agent.runtime.loop.agent_loop import TurnContext, _tool_trail_entry_to_canonical
from data_agent.runtime.loop.proposal import assess_deliverable_evidence
from data_agent.runtime.model.conversation import restore_response_batches
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import ResultPreview, TrailEntry, live_analysis_state

SCOPE = frozenset({"hr.employee.salary"})
CREDS = RuntimeCredentials(session_id="reuse-test", jwt="not-for-output", column_scope=SCOPE)


def original(**kwargs):
    entry = TrailEntry(
        turn_index=0,
        tool_call_id="prior",
        tool_name="runQuery",
        args={"sql": "SELECT sum(salary) AS Total FROM hr.employee"},
        status="ok",
        error_code=None,
        provenance=frozenset({("hr.employee", "salary")}),
        result_preview=ResultPreview(
            columns=["Total"], row_count=1, truncated=False, preview_rows=[[100]]
        ),
        result_full_ref=None,
        ts="2026-10-14T12:00:00Z",
        model_response={"scope_hash": compute_scope_hash(SCOPE)},
    )
    return replace(entry, **kwargs)


async def setup(entry=None):
    store = InMemorySessionStore()
    await store.append_trail_entry(CREDS.session_id, entry or original())
    tool = UpdateAnalysisStateTool(session_store=store)
    result = await tool.run(
        {"intents": [{"description": "Explain the earlier salary total"}]},
        CREDS,
        turn=TurnContext(turn_index=1),
    )
    assert result.status == "ok"
    return tool, store


async def reuse(tool, **overrides):
    args = {
        "intent_id": "i1",
        "status": "completed",
        "result_id": "prior",
        "reuse_previous_result": True,
    }
    args.update(overrides)
    return await tool.run({"intents": [args]}, CREDS, turn=TurnContext(turn_index=1))


async def test_reuse_is_explicit_labeled_and_finalizable_without_new_query():
    tool, store = await setup()
    result = await reuse(tool)
    assert result.status == "ok"
    doc = await store.get_or_create_session(CREDS.session_id)
    state = live_analysis_state(doc, 1)
    reference_id = state.intents[0].evidence_tool_call_id
    assert reference_id != "prior"
    reference = next(e for e in doc.tool_trail if e.tool_call_id == reference_id)
    assert reference.ts == original().ts
    assert reference.args == original().args
    assert reference.provenance == original().provenance
    assert reference.model_response["reused_from_result_id"] == "prior"
    rendered = render_entry(TrailEntry.from_doc(reference.to_doc()), 5)
    assert "not freshly executed" in rendered["window_note"]
    assert "not freshly executed" in str(_tool_trail_entry_to_canonical(rendered))
    assessment = assess_deliverable_evidence(state, {"evidence": [reference_id]}, [reference])
    assert assessment.feedback is None
    assert "not-for-output" not in str(result) + str(rendered)
    await reuse(tool)
    assert len((await store.get_or_create_session(CREDS.session_id)).tool_trail) == 2


async def test_prior_result_without_opt_in_stays_rejected():
    tool, store = await setup()
    result = await reuse(tool, reuse_previous_result=False)
    assert result.error_code == "ANALYSIS_STATE_INVALID"
    assert "reuse_previous_result=true" in result.denial_detail
    assert len((await store.get_or_create_session(CREDS.session_id)).tool_trail) == 1


@pytest.mark.parametrize("reasoning_enabled", [False, True])
async def test_reference_receipt_survives_response_batch_restoration(reasoning_enabled):
    tool, store = await setup()
    assert (await reuse(tool)).status == "ok"
    doc = await store.get_or_create_session(CREDS.session_id)
    reference = doc.tool_trail[-1]
    canonical = _tool_trail_entry_to_canonical(render_entry(reference, 5))
    assert "_model_response" not in canonical[0]
    restored = restore_response_batches(
        canonical,
        scope_hash=compute_scope_hash(SCOPE),
        use_reasoning_metadata=reasoning_enabled,
    )
    assert restored == canonical
    assert restored[1]["tool_call_id"] == reference.tool_call_id
    assert "not freshly executed" in restored[1]["content"]


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "error"},
        {"status": "denied"},
        {"provenance": None},
        {"model_response": None},
        {"model_response": {"scope_hash": "other"}},
        {"provenance": frozenset({("hr.employee", "ssn")})},
        {"provenance": frozenset({("scratch.table", "salary")})},
        {"tool_name": "runBlueprint", "authoritative": False},
        {"tool_name": "getTableSchema"},
        {"result_preview": None},
        {"turn_index": 2},
        {
            "model_response": {
                "scope_hash": compute_scope_hash(SCOPE),
                "reused_from_result_id": "older",
            }
        },
    ],
)
async def test_ineligible_results_cannot_be_promoted(changes):
    tool, store = await setup(original(**changes))
    result = await reuse(tool)
    assert result.error_code == "ANALYSIS_STATE_INVALID"
    assert len((await store.get_or_create_session(CREDS.session_id)).tool_trail) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "blocked"},
        {"reuse_previous_result": "true"},
        {"intent_id": "invented"},
        {"result_id": "unknown"},
    ],
)
async def test_invalid_reuse_requests_do_not_create_receipts(overrides):
    tool, store = await setup()
    assert (await reuse(tool, **overrides)).error_code == "ANALYSIS_STATE_INVALID"
    assert len((await store.get_or_create_session(CREDS.session_id)).tool_trail) == 1


async def test_verified_blueprint_can_be_reused():
    tool, _ = await setup(original(tool_name="runBlueprint", authoritative=True))
    assert (await reuse(tool)).status == "ok"


@pytest.mark.parametrize("excluded_turn", [0, 1])
async def test_explicit_reuse_only_blocks_current_turn_exclusions(excluded_turn):
    tool, store = await setup()
    await store.write_review_state(CREDS.session_id, excluded_turn, {
        "excluded_components": [{"result_id": "prior"}]
    })
    result = await reuse(tool)
    assert (result.status == "ok") == (excluded_turn == 0)
    assert len(await store.load_trail(CREDS.session_id)) == (2 if excluded_turn == 0 else 1)


async def test_reuse_alias_across_successive_turns_resolves_original_and_is_idempotent():
    tool, store = await setup()
    assert (await reuse(tool)).status == "ok"
    alias = (await store.load_trail(CREDS.session_id))[-1]
    for turn_index in (2, 3):
        turn = TurnContext(turn_index=turn_index)
        assert (await tool.run({"intents": [{"description": "Restore the earlier total"}]},
                               CREDS, turn=turn)).status == "ok"
        args = {"intents": [{"intent_id": "i1", "status": "completed",
                             "result_id": alias.tool_call_id, "reuse_previous_result": True}]}
        assert (await tool.run(args, CREDS, turn=turn)).status == "ok"
        trail = await store.load_trail(CREDS.session_id)
        reference = trail[-1]
        assert reference.model_response["reused_from_result_id"] == "prior"
        assert reference.args == original().args
        assert reference.ts == original().ts
        assert reference.result_preview == original().result_preview
        assert (await tool.run(args, CREDS, turn=turn)).status == "ok"
        assert len(await store.load_trail(CREDS.session_id)) == len(trail)
        alias = reference


@pytest.mark.parametrize("failure", ["missing_origin", "cycle", "changed_scope", "failed_origin",
                                    "unverified_origin", "excluded_origin", "excluded_alias"])
async def test_alias_cannot_bypass_origin_eligibility_or_exclusions(failure):
    from data_agent.runtime.composite.analysis_state import previous_result_reference

    source = original()
    alias = previous_result_reference(source, 1, compute_scope_hash(SCOPE))
    if failure == "cycle":
        alias = replace(alias, model_response={**alias.model_response,
                                              "reused_from_result_id": alias.tool_call_id})
    elif failure == "changed_scope":
        source = replace(source, model_response={"scope_hash": "different"})
    elif failure == "failed_origin":
        source = replace(source, status="error")
    elif failure == "unverified_origin":
        source = replace(source, tool_name="runBlueprint", authoritative=False)
    store = InMemorySessionStore()
    if failure != "missing_origin":
        await store.append_trail_entry(CREDS.session_id, source)
    await store.append_trail_entry(CREDS.session_id, alias)
    if failure.startswith("excluded_"):
        await store.write_review_state(CREDS.session_id, 2, {"excluded_components": [
            {"result_id": source.tool_call_id if failure == "excluded_origin" else alias.tool_call_id}
        ]})
    tool = UpdateAnalysisStateTool(session_store=store)
    turn = TurnContext(turn_index=2)
    await tool.run({"intents": [{"description": "Restore the earlier total"}]}, CREDS, turn=turn)
    before = len(await store.load_trail(CREDS.session_id))
    response = await tool.run({"intents": [{"intent_id": "i1", "status": "completed",
        "result_id": alias.tool_call_id, "reuse_previous_result": True}]}, CREDS, turn=turn)
    assert response.error_code == "ANALYSIS_STATE_INVALID"
    assert len(await store.load_trail(CREDS.session_id)) == before


@pytest.mark.parametrize("bad_intermediate", [False, True])
def test_legacy_alias_chain_checks_every_link(bad_intermediate):
    from data_agent.runtime.composite.analysis_state import (
        AnalysisStateRejectedError,
        previous_result_reference,
        resolve_previous_result,
    )

    scope = compute_scope_hash(SCOPE)
    source = original()
    first = previous_result_reference(source, 1, scope)
    second = replace(first, turn_index=2, tool_call_id="second-alias",
                     model_response={"scope_hash": scope,
                                     "reused_from_result_id": first.tool_call_id})
    if bad_intermediate:
        first = replace(first, model_response={**first.model_response, "scope_hash": "other"})
        with pytest.raises(AnalysisStateRejectedError):
            resolve_previous_result(second, [source, first, second], 3, scope)
    else:
        assert resolve_previous_result(second, [source, first, second], 3, scope) is source
