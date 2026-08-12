"""A1 — runtime mechanics, scripted model, per-commit CI (07 §A/§D).

WHAT THIS SUITE PROVES, and what it does not:

    GIVEN a routing decision, the runtime records, validates, enforces and
    terminates correctly.

It CANNOT prove that the agent routes, and it CANNOT go red on a bad prompt.
`ScriptedModelClient.send_turn` records `messages` and returns the next scripted
result — THE PROMPT IS NEVER READ — so every tool name and argument below comes
from the fixture, and these tests would pass identically against today's prompt,
a badly-rewritten one, or one that says "never use blueprints". That is what A2
(`test_routing_live.py`) is for. See `README.md`.

So every assertion here is deliberately something A FIXTURE CANNOT FAKE:

  * that 04's validators ACCEPTED the cited evidence (a fixture can claim
    `completed`; only the runtime decides whether the state records it);
  * that the D56 verify block earned `authoritative` on a `runBlueprint`;
  * that enforcement REFUSED a finalization the fixture asked for;
  * that the scope pre-filter dropped a card the fixture put in the corpus;
  * that merge-by-id kept intents an update stopped mentioning;
  * that a persisted counter survived a resume.
"""

from __future__ import annotations

from data_agent.runtime.composite.analysis_state import ANALYSIS_STATE_INVALID_CODE
from data_agent.runtime.context.assembly import IDEMPOTENT_READ_ALREADY_SERVED_CODE
from data_agent.runtime.session.models import TrackedIntent

from . import metrics
from .conftest import EvalHarness, load_cases, rendered_blueprint_block

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _intents(harness: EvalHarness) -> dict[str, TrackedIntent]:
    state = harness.analysis_state()
    assert state is not None, f"{harness.case.id}: no analysisState was persisted"
    return {intent.intent_id: intent for intent in state.intents}


def _assert_all_terminal(harness: EvalHarness) -> dict[str, TrackedIntent]:
    """Every tracked intent reached `completed` or `blocked`.

    This is the Release-1 contract restated over what the RUNTIME persisted: a
    recorded, falsifiable terminal disposition for every intent the model chose to
    track. It says nothing about whether the disposition is semantically true.
    """
    intents = _intents(harness)
    assert len(intents) == harness.case.ground_truth_intent_count
    pending = [i.intent_id for i in intents.values() if i.status == "pending"]
    assert not pending, f"{harness.case.id}: intents still pending at a terminal outcome: {pending}"
    return intents


def _assert_state_writes_accepted(harness: EvalHarness) -> None:
    """No `updateAnalysisState` call was rejected.

    The load-bearing half: 03 §C.3 and 04 turn nine different rules into one
    `ANALYSIS_STATE_INVALID`, and a rejected call still leaves an `error` trail
    entry — so a case whose intents are all `pending` because every state write
    bounced would otherwise look like a routing result. If this trips, read
    `denial_detail`; it names the rule.
    """
    rejected = [
        entry
        for entry in harness.trail()
        if entry.tool_name == "updateAnalysisState" and entry.status != "ok"
    ]
    assert not rejected, [
        (entry.tool_call_id, entry.error_code, entry.denial_detail) for entry in rejected
    ]
    assert harness.observer.count("loop_analysis_state_rejected") == 0


def _turn_records(harness: EvalHarness) -> list[metrics.TurnRecord]:
    """One `TurnRecord` per HTTP request the case made.

    `turn_index` is derived positionally: a `resume` continues the turn its
    `/turn` opened, a second `/turn` opens the next one. That is the same rule
    `AgentLoop.run`/`resume` apply (`messages[-1].turn_index + 1` vs
    `messages[-1].turn_index`).
    """
    records: list[metrics.TurnRecord] = []
    turn_index = -1
    state = harness.analysis_state()
    for request, outcome in zip(harness.case.requests, harness.outcomes, strict=True):
        if request.kind == "turn":
            turn_index += 1
        records.append(
            metrics.TurnRecord(
                case_id=harness.case.id,
                turn_index=turn_index,
                status=outcome["status"],
                state=state,
            )
        )
    return records


# ---------------------------------------------------------------------------
# Case 1 — pre-injection
# ---------------------------------------------------------------------------


def test_case_01_cards_are_pre_injected_with_02_enrichment(harness_factory) -> None:
    """The three cards reach the model BEFORE round 1, carrying 02's enrichment.

    NOT A FIXTURE ECHO: the fixture names three blueprint IDS and nothing else.
    Every string asserted below is produced by the runtime from the COMMITTED
    corpus — `blueprints.yaml` -> `load_seed_fixtures` -> recall ->
    `pipeline._to_thin_card` -> `render._card_detail_lines`. Deliberately asserted
    at the two ends 02 can silently break: `_to_thin_card` is the ONE place a card
    is built, so an unprojected field vanishes everywhere with no error, and
    `_card_detail_lines` is the only place it is rendered.
    """
    harness = harness_factory("case-01-pre-injection")
    assert harness.outcomes[-1]["status"] == "done"

    # Round 1 — before any tool ran, so the model could route on it.
    block = rendered_blueprint_block(harness.model_messages(0))
    assert block, "no retrieval block reached the model"

    for blueprint_id in harness.case.corpus:
        assert blueprint_id in block, blueprint_id

    # 02's enrichment, each field from a DIFFERENT blueprint in the corpus:
    #   `resolves` is declared only by bp-average-salary-by-department,
    #   `result grain` only by the two department blueprints,
    #   the typed slot line by bp-hires-projection.
    assert "resolves: salary -> annual_salary" in block
    assert "result grain: Department" in block
    assert "window_months (relative_window, required)" in block

    # The base prompt is present and is the SOLE system message. A hand-assembled
    # harness inherits `base_system_prompt=None` from the Layer-1 default and
    # would run promptless with nothing to signal it.
    system_messages = harness.system_messages(0)
    assert len(system_messages) == 1
    assert system_messages[0].strip()


def test_case_01_single_intent_request_does_not_initialize_state(harness_factory) -> None:
    """E.3's FALSE-POSITIVE check.

    Detection RATE is meaningless in A1 (definitionally 1.0 — the numerator fires
    only because the fixture scripts `updateAnalysisState`), but its inverse is
    not: an `analysisState` initialized for a single-intent request burns 03 §E's
    late-init boundary and adds CAS writes for nothing. `multi_intent: false`
    fixtures are what buy this.
    """
    harness = harness_factory("case-01-pre-injection")
    assert harness.case.ground_truth_multi_intent is False
    assert harness.analysis_state() is None
    assert harness.observer.count("loop_analysis_state_initialized") == 0

    observation = metrics.DetectionObservation(
        case_id=harness.case.id,
        multi_intent=harness.case.ground_truth_multi_intent,
        analysis_state_initialized=harness.analysis_state() is not None,
    )
    rate = metrics.multi_intent_detection([observation])
    assert rate.false_positives == 0
    assert rate.rate is None  # no multi-intent ground truth in this sample


# ---------------------------------------------------------------------------
# Case 2 — two blueprint intents complete
# ---------------------------------------------------------------------------


def test_case_02_two_blueprint_intents_complete_on_authoritative_evidence(
    harness_factory,
) -> None:
    """NOT A FIXTURE ECHO: 04 condition 4 requires `authoritative is True` on a
    cited `runBlueprint`, and that marker is set ONLY when the D56 verify block
    comes back clean (`status:"verified"` + `grain_ok` + `signature_ok`). A
    fixture can claim `completed`; it cannot make the executor verify.
    """
    harness = harness_factory("case-02-two-blueprints")
    _assert_state_writes_accepted(harness)
    intents = _assert_all_terminal(harness)

    assert intents["i1"].status == "completed"
    assert intents["i2"].status == "completed"
    for call_id in ("b1", "b2"):
        entry = harness.entry(call_id)
        assert entry.tool_name == "runBlueprint"
        assert entry.status == "ok"
        assert entry.authoritative is True, f"{call_id} did not earn the D56 marker"
    assert intents["i1"].evidence_tool_call_id == "b1"
    assert intents["i2"].evidence_tool_call_id == "b2"

    # Finalization was never refused: both intents were terminal when the batched
    # `answerWithTable` ran, which is the 05 §G interaction — the state update and
    # the finalization in ONE response, closing the last intent without spending an
    # extra round.
    assert harness.observer.count("loop_finalization_refused") == 0
    assert harness.observer.count("loop_enforcement_exhausted") == 0
    assert harness.outcomes[-1]["status"] == "done"

    completed = [p["evidence_tool_name"] for p in harness.observer.payloads("loop_intent_completed")]
    assert completed == ["runBlueprint", "runBlueprint"]


# ---------------------------------------------------------------------------
# Case 3 — blueprint + ad-hoc residual: re-derivation is INTENT-scoped
# ---------------------------------------------------------------------------


def test_case_03_post_blueprint_query_for_a_distinct_intent_is_not_re_derivation(
    harness_factory,
) -> None:
    """The interesting half of case 3, and the one the draft never wrote.

    A `runQuery` DID run after an authoritative `runBlueprint` in the same turn.
    The rejected TURN-scoped predicate flags that; the INTENT-scoped one does not,
    because the query serves a DISTINCT intent — which `prompts.py` explicitly
    permits. Both are asserted, so the test fails if the two ever collapse into
    the same answer and the §C.2 correction quietly stops meaning anything.
    """
    harness = harness_factory("case-03-blueprint-plus-adhoc")
    _assert_state_writes_accepted(harness)
    intents = _assert_all_terminal(harness)
    assert all(i.status == "completed" for i in intents.values())

    trail = harness.trail()
    blueprint = harness.entry("b1")
    query = harness.entry("q1")
    assert blueprint.authoritative is True
    positions = {e.tool_call_id: n for n, e in enumerate(trail)}
    assert positions["q1"] > positions["b1"], "the query must come AFTER the blueprint"
    assert query.tool_name == "runQuery" and query.status == "ok"

    assert metrics.re_derivation_turn_scoped(trail, turn_index=0) is True
    assert (
        metrics.re_derivation(trail, harness.case.serves_intent, turn_index=0) is False
    )
    # The same answer with the mapping RECONSTRUCTED from the trail rather than
    # declared by the fixture — this is the derivation A2 uses, so it is exercised
    # here where the expected value is known.
    derived = metrics.serves_intent_from_trail(trail, 0)
    assert derived == {"b1": {"i1"}, "q1": {"i2"}}
    assert metrics.re_derivation(trail, derived, turn_index=0) is False


# ---------------------------------------------------------------------------
# Case 4 — three intents, none silently dropped
# ---------------------------------------------------------------------------


def test_case_04_partial_update_does_not_drop_the_intents_it_omits(
    harness_factory,
) -> None:
    """NOT A FIXTURE ECHO: round 2 names ONE intent of three. What comes back is a
    state of THREE — merge-by-id, the mechanism that makes the drop evasion
    structurally impossible (03 §C.4). Asserted on the tool RESULT, because that
    result is the only thing the model sees and is what a "full replace" shape
    would have shortened.
    """
    harness = harness_factory("case-04-three-intents")
    _assert_state_writes_accepted(harness)

    partial = harness.entry("s2")
    assert partial.status == "ok"
    assert partial.result_preview is not None
    assert partial.result_preview.row_count == 3, "the omitted intents were dropped"

    intents = _assert_all_terminal(harness)
    assert sorted(intents) == ["i1", "i2", "i3"]
    assert all(i.status == "completed" for i in intents.values())
    assert harness.outcomes[-1]["status"] == "done"

    report = metrics.blocked_intent_report(_turn_records(harness))
    assert report.tracked == 3
    assert report.completed == 3
    assert report.blocked == 0


# ---------------------------------------------------------------------------
# Case 5 — metadata + analytical
# ---------------------------------------------------------------------------


def test_case_05_metadata_intent_completes_on_a_schema_fetch(harness_factory) -> None:
    """The accepted trade (04 §A), plus its mitigation.

    NOT A FIXTURE ECHO on either half: the completion requires `getTableSchema` to
    be in `COMPLETION_EVIDENCE_TOOLS` and the entry to carry no guard marker, and
    `loop_metadata_evidence_completion` is emitted by the TOOL, from the trail
    entry it resolved — not from anything the fixture declared.
    """
    harness = harness_factory("case-05-metadata-plus-analytical")
    _assert_state_writes_accepted(harness)
    intents = _assert_all_terminal(harness)
    assert intents["i1"].evidence_tool_call_id == "m1"
    assert intents["i2"].evidence_tool_call_id == "q1"

    schema_entry = harness.entry("m1")
    assert schema_entry.tool_name == "getTableSchema"
    assert schema_entry.status == "ok"
    # EXACTLY ONE FETCH PER TABLE. A second identical call is not re-dispatched:
    # it is persisted as a data-free entry carrying this marker, which passes
    # conditions 1-4 having fetched nothing and is rejected by condition 5.
    assert schema_entry.error_code is None
    assert not [
        e for e in harness.trail() if e.error_code == IDEMPOTENT_READ_ALREADY_SERVED_CODE
    ]
    assert harness.observer.count("loop_repeated_idempotent_read_guarded") == 0

    metadata_events = harness.observer.payloads("loop_metadata_evidence_completion")
    assert [p["intent_id"] for p in metadata_events] == ["i1"]

    # 06's inferred corpus-gap signal: no `searchBlueprints` ran here, so the
    # ad-hoc completion is NOT evidence the corpus was searched and fell through.
    signal = metrics.corpus_gap_signal(harness.observer.events)
    assert signal.searched is False
    assert signal.fell_through is False


# ---------------------------------------------------------------------------
# Case 6 — a pending intent refuses finalization, exactly once
# ---------------------------------------------------------------------------


def test_case_06_pending_intent_refuses_finalization_then_exhausts(
    harness_factory,
) -> None:
    """NOT A FIXTURE ECHO, and the clearest example of it: the fixture ASKS to
    finalize, twice. The runtime refuses the first attempt with a retryable error
    naming the pending intent, grants exactly ONE forced re-round for the window,
    and on the second attempt records `ENFORCEMENT_EXHAUSTED` and lets the turn
    close.
    """
    harness = harness_factory("case-06-pending-blocks-finalization")
    _assert_state_writes_accepted(harness)

    refusal = harness.entry("a1")
    assert refusal.status == "error"
    assert refusal.error_code == "FINALIZATION_BLOCKED_PENDING_INTENTS"
    assert refusal.denial_detail is not None
    # `denial_detail` is the ONLY channel that reaches the model (`_render_entry`
    # never reads `ToolResult.user_message`), so a message that does not name the
    # pending intent tells the model nothing actionable.
    assert "i2" in refusal.denial_detail

    refusals = harness.observer.payloads("loop_finalization_refused")
    assert [p["exit"] for p in refusals] == ["answer_with_table"]
    assert refusals[0]["pending_count"] == 1
    assert [p["window"] for p in harness.observer.payloads("loop_finalization_block_spent")] == [1]
    assert harness.doc().finalization_blocks == {"0:1": 1}

    assert harness.observer.count("loop_enforcement_exhausted") == 1
    forced = harness.observer.payloads("loop_intent_force_blocked")
    assert [(p["intent_id"], p["reason_code"]) for p in forced] == [
        ("i2", "ENFORCEMENT_EXHAUSTED")
    ]

    intents = _assert_all_terminal(harness)
    assert intents["i1"].status == "completed"
    assert intents["i2"].status == "blocked"
    assert intents["i2"].reason_code == "ENFORCEMENT_EXHAUSTED"
    # A RUNTIME-forced block cites no evidence — that absence is exactly what
    # distinguishes it from a model-declared one in the ledger.
    assert intents["i2"].evidence_tool_call_id is None
    assert harness.outcomes[-1]["status"] == "done"

    report = metrics.blocked_intent_report(_turn_records(harness))
    assert report.buckets["ENFORCEMENT_EXHAUSTED"] == 1
    assert report.buckets["NO_ACCESS"] == 0
    # E.2: derived from `REASON_CODES`, so a code added later cannot go uncounted.
    assert "BUDGET_EXHAUSTED" in report.buckets
    assert "USER_STOPPED" in report.buckets


def test_case_06_forced_block_emits_two_events_but_counts_one_intent(
    harness_factory,
) -> None:
    """E.2's per-event/per-intent distinction, asserted against real emissions.

    One forced block emits BOTH `loop_analysis_state_transition` AND
    `loop_intent_force_blocked`. Counting events would report two blocked intents
    where there is one — which is why the report folds per `intent_id` final state.
    """
    harness = harness_factory("case-06-pending-blocks-finalization")
    forced_events = [
        (name, payload)
        for name, payload in harness.observer.events
        if payload.get("intent_id") == "i2"
        and payload.get("reason_code") == "ENFORCEMENT_EXHAUSTED"
    ]
    assert sorted(name for name, _ in forced_events) == [
        "loop_analysis_state_transition",
        "loop_intent_force_blocked",
    ]
    report = metrics.blocked_intent_report(_turn_records(harness))
    assert report.blocked == 1


# ---------------------------------------------------------------------------
# Case 7 — NO_ACCESS
# ---------------------------------------------------------------------------


def test_case_07_narrowed_scope_drops_a_card_and_a_denial_blocks_the_intent(
    harness_factory,
) -> None:
    """Two different jobs the narrowed `column_scope` does, both asserted.

    NOT A FIXTURE ECHO: the corpus contains BOTH blueprints, and the runtime's
    `uses ⊆ scope` recall pre-filter is what removes one of them. And the block is
    accepted only because `validate_block_evidence` found `status != "ok"` with an
    `error_code` in `NO_ACCESS_ERROR_CODES` — keyed on the CODE, not the outer
    status, which is the 04 §B.1 fix.
    """
    harness = harness_factory("case-07-no-access-block")
    _assert_state_writes_accepted(harness)

    block = rendered_blueprint_block(harness.model_messages(0))
    assert "bp-hires-projection" in block
    assert "bp-average-salary-by-department" not in block, (
        "the out-of-scope card survived the uses ⊆ scope pre-filter"
    )

    denied = harness.entry("q1")
    assert denied.status == "denied"
    assert denied.error_code == "COLUMN_SCOPE_VIOLATION"

    intents = _assert_all_terminal(harness)
    assert intents["i1"].status == "completed"
    assert intents["i2"].status == "blocked"
    assert intents["i2"].reason_code == "NO_ACCESS"
    assert intents["i2"].evidence_tool_call_id == "q1"

    report = metrics.blocked_intent_report(_turn_records(harness))
    assert report.buckets["NO_ACCESS"] == 1
    assert report.buckets["ENFORCEMENT_EXHAUSTED"] == 0
    assert harness.outcomes[-1]["status"] == "done"


def test_case_07_one_denial_cannot_block_a_second_intent(harness_factory) -> None:
    """04 §B.3, asserted end to end rather than at the validator.

    Block evidence must be DISTINCT per intent. Without the rule, one
    `getTableSchema(<scratch_db>, "x")` yields `SCRATCH_SESSION_VIOLATION` and can
    close every tracked intent at once for O(1) calls. Here the second citation of
    `q1` must be refused — and the refusal must leave the earlier, legitimate
    block standing.
    """
    harness = harness_factory("case-07-no-access-block")
    import anyio

    from data_agent.runtime.auth.credentials import RuntimeCredentials
    from data_agent.runtime.composite.analysis_state import UpdateAnalysisStateTool
    from data_agent.runtime.loop.agent_loop import TurnContext

    tool = UpdateAnalysisStateTool(session_store=harness.store)
    result = anyio.run(
        lambda: tool.run(
            {
                "intents": [
                    {
                        "intent_id": "i1",
                        "status": "blocked",
                        "reason_code": "NO_ACCESS",
                        "evidence_tool_call_id": "q1",
                    }
                ]
            },
            RuntimeCredentials(
                session_id="sess-eval", jwt="eval-jwt", column_scope=harness.case.column_scope
            ),
            TurnContext(turn_index=0),
        )
    )
    assert result.status == "error"
    assert result.error_code == ANALYSIS_STATE_INVALID_CODE
    assert "already the evidence" in (result.denial_detail or "")
    assert _intents(harness)["i2"].reason_code == "NO_ACCESS"


# ---------------------------------------------------------------------------
# Case 8 — REQUIRED_DATA_UNAVAILABLE, and the zero-row ratio
# ---------------------------------------------------------------------------


def test_case_08_zero_rows_completes_one_intent_and_blocks_another(
    harness_factory,
) -> None:
    """04 §B.4's open hole, asserted so it is MEASURED rather than implied closed.

    Two structurally IDENTICAL zero-row results: one completes an intent (the
    honest "nobody left" answer), one blocks it as `REQUIRED_DATA_UNAVAILABLE`.
    The runtime accepts both, because they are mechanically indistinguishable. The
    ratio — not the count — is the health signal.
    """
    harness = harness_factory("case-08-required-data-unavailable")
    _assert_state_writes_accepted(harness)

    for call_id in ("q1", "q2"):
        entry = harness.entry(call_id)
        assert entry.status == "ok"
        assert entry.result_preview is not None
        assert entry.result_preview.row_count == 0

    intents = _assert_all_terminal(harness)
    assert intents["i1"].status == "completed"
    assert intents["i2"].status == "blocked"
    assert intents["i2"].reason_code == "REQUIRED_DATA_UNAVAILABLE"

    ratio = metrics.zero_row_ratio(harness.observer.events)
    assert ratio.blocks == 1
    assert ratio.completions == 1
    assert ratio.block_share == 0.5

    report = metrics.blocked_intent_report(_turn_records(harness))
    assert report.buckets["REQUIRED_DATA_UNAVAILABLE"] == 1


# ---------------------------------------------------------------------------
# Case 9 — multi-intent across an askUser pause
# ---------------------------------------------------------------------------


def test_case_09_state_commits_before_the_pause(harness_factory) -> None:
    """03 §E.1's exact shape: `updateAnalysisState` + `askUser` in ONE response.

    NOT A FIXTURE ECHO: `askUser` is scanned for and honoured BEFORE
    `capped_tool_calls` is computed, so without the partition the state write is
    discarded entirely — no trail entry, no tool result, and D22 discards the
    surrounding free text, leaving the model with no record it ever tried. The
    persisted state after the FIRST request is what proves the ordering.
    """
    harness = harness_factory("case-09-multi-intent-across-pause")
    assert harness.outcomes[0]["status"] == "paused_ask_user"

    state = harness.analysis_state()
    assert state is not None
    assert state.turn_index == 0
    assert [i.intent_id for i in state.intents] == ["i1", "i2"]

    committed = [
        e for e in harness.trail() if e.tool_name == "updateAnalysisState" and e.status == "ok"
    ]
    assert committed, "the state call was discarded by the askUser short-circuit"
    assert committed[0].tool_call_id == "s1"
    assert harness.observer.count("loop_analysis_state_initialized") == 1


def test_case_09_block_counter_is_not_reset_by_a_resume(harness_factory) -> None:
    """05 §C.1, the reason the counter is PERSISTED and keyed by budget window.

    `_run_loop_body` is re-entered on every resume while `window_count` stands
    still, so a counter local to that function would reset here and forced
    re-rounds would be unbounded (user-paced, but unbounded). Round 3 spends the
    window's one block; round 5 arrives across a SECOND pause and resume and must
    find it already spent — so it force-blocks instead of granting a re-round.
    """
    harness = harness_factory("case-09-multi-intent-across-pause")
    assert [o["status"] for o in harness.outcomes] == [
        "paused_ask_user",
        "paused_ask_user",
        "done",
    ]

    spent = harness.observer.payloads("loop_finalization_block_spent")
    assert [p["window"] for p in spent] == [1], "the counter was reset by a resume"
    assert harness.doc().finalization_blocks == {"0:1": 1}
    assert harness.observer.count("loop_finalization_refused") == 1
    assert harness.observer.count("loop_enforcement_exhausted") == 1

    intents = _assert_all_terminal(harness)
    assert intents["i1"].status == "completed"
    assert intents["i2"].reason_code == "ENFORCEMENT_EXHAUSTED"


def test_case_09_pauses_are_not_gated_by_enforcement(harness_factory) -> None:
    """05 §G: a pause is NOT finalization. Both `askUser` pauses returned with
    intents legitimately `pending`, and neither burned the nudge or forced a
    block — the refusal came from the `answerWithTable` in round 3, once."""
    harness = harness_factory("case-09-multi-intent-across-pause")
    paused = [o for o in harness.outcomes if o["status"] == "paused_ask_user"]
    assert len(paused) == 2
    assert harness.observer.count("loop_paused_ask_user") == 2
    assert harness.observer.count("loop_finalization_refused") == 1


# ---------------------------------------------------------------------------
# Case 10 — abandoned pause, then a new turn
# ---------------------------------------------------------------------------


def test_case_10_abandoned_turn_is_untouched_by_the_next_turn(harness_factory) -> None:
    """03 §A.1 / 05 §A, end to end.

    Turn 0 pauses with two `pending` intents and is never resumed. Turn 1 is
    unrelated and single-intent. Without `live_analysis_state` the next turn would
    be refused, would burn its nudge, and would write `ENFORCEMENT_EXHAUSTED` onto
    TURN 0's intents.

    NOT A FIXTURE ECHO: nothing in the fixture says "leave turn 0 alone" — the
    turn gate is what does it, and the assertion is over the persisted document.
    """
    harness = harness_factory("case-10-abandoned-pause-new-turn")
    assert [o["status"] for o in harness.outcomes] == ["paused_ask_user", "done"]

    state = harness.analysis_state()
    assert state is not None
    assert state.turn_index == 0, "turn 1 rewrote the abandoned turn's state"
    assert [i.status for i in state.intents] == ["pending", "pending"]
    assert all(i.reason_code is None for i in state.intents)

    # No enforcement fired on turn 1 at all.
    assert harness.observer.count("loop_finalization_refused") == 0
    assert harness.observer.count("loop_enforcement_exhausted") == 0
    assert harness.observer.count("loop_intent_force_blocked") == 0
    assert harness.doc().finalization_blocks in (None, {})


def test_case_10_prior_turn_intent_text_does_not_leak_into_the_next_turn(
    harness_factory,
) -> None:
    """03 §D.1 — the cross-turn leak, which needs BOTH mechanisms.

    `live_analysis_state` suppresses the rendered STATE BLOCK, but every
    `updateAnalysisState` call also leaves a `TrailEntry` whose `args` carry the
    descriptions with `frozenset()` provenance — kept under ANY `column_scope`, in
    EVERY later turn, and replayed verbatim by `_render_entry`. Only
    `_is_stale_model_text_entry`'s widened tool set closes that half.
    """
    harness = harness_factory("case-10-abandoned-pause-new-turn")
    turn_one_messages = harness.model_messages(1)
    blob = "\n".join(str(m.get("content") or "") for m in turn_one_messages)
    assert "Average salary by department" not in blob
    assert "Headcount by department" not in blob
    assert "[Analysis state" not in blob


def test_case_10_pending_sweep_is_scoped_to_terminal_outcomes(harness_factory) -> None:
    """E.1's scoping, asserted against the shape that breaks the unscoped form.

    Turn 0 ends `paused_ask_user` with two `pending` intents — legitimately, as a
    NON-TERMINATED turn. Turn 1 ends `done` and its live state is `None`. An
    unscoped "no intent ends pending" assertion fails here, and against any real
    store, for exactly this reason.
    """
    harness = harness_factory("case-10-abandoned-pause-new-turn")
    records = _turn_records(harness)
    assert [r.status for r in records] == ["paused_ask_user", "done"]
    assert metrics.pending_on_terminal_turns(records) == []

    unscoped = [
        (r.case_id, i.intent_id)
        for r in records
        if r.state is not None
        for i in r.state.intents
        if i.status == "pending"
    ]
    assert unscoped, "this case must be able to break the unscoped form, or it proves nothing"


# ---------------------------------------------------------------------------
# The cross-case sweep (07 §E.1) — cheap, redundant, and scoped
# ---------------------------------------------------------------------------


def test_every_case_leaves_no_pending_intent_on_a_terminal_turn(
    monkeypatch,
) -> None:
    """The teardown sweep over EVERY case, in one place.

    Enforcement is proven at Layer 1 (05 §H); this is the redundant Layer-4 pass
    over whatever turns the cases happened to produce. Scoped to
    `status in {"done", "stopped_hard_ceiling"}` — the real `TurnStatus` values.
    """
    from .conftest import build_harness, drive

    records: list[metrics.TurnRecord] = []
    observations: list[metrics.DetectionObservation] = []
    for case in load_cases():
        harness = build_harness(case, monkeypatch)
        drive(harness)
        records.extend(_turn_records(harness))
        observations.append(
            metrics.DetectionObservation(
                case_id=case.id,
                multi_intent=case.ground_truth_multi_intent,
                analysis_state_initialized=harness.analysis_state() is not None,
            )
        )

    assert metrics.pending_on_terminal_turns(records) == []

    report = metrics.blocked_intent_report(records)
    assert report.tracked > 0
    assert report.buckets["UNKNOWN_REASON_CODE"] == 0
    # `report.pending` is deliberately NOT asserted to be zero. Case 10's turn 0
    # legitimately ends `paused_ask_user` with two `pending` intents, and E.2 is a
    # fold over every tracked intent's final state — not the contract sweep. The
    # contract sweep is the scoped assertion above, and the two must stay
    # different or the scoping is lost again.
    assert report.pending == len(
        [
            intent
            for record in records
            if record.status in metrics.NON_TERMINAL_TURN_STATUSES
            and record.state is not None
            and record.state.turn_index == record.turn_index
            for intent in record.state.intents
            if intent.status == "pending"
        ]
    )

    # E.3 is NOT reported here. In A1 the detection rate is definitionally 1.0 —
    # the numerator fires only because the fixture scripts `updateAnalysisState` —
    # so printing it beside these results would read as a measured quality signal
    # for something no scripted suite can measure. Only the FALSE-POSITIVE side
    # carries information, and it is the single-intent fixtures that buy it.
    rate = metrics.multi_intent_detection(observations)
    assert rate.false_positives == 0
    assert rate.single_intent_seen >= 1


def test_every_fixture_declares_the_ground_truth_the_metrics_need() -> None:
    """A fixture missing `ground_truth` would silently shrink E.3's denominator,
    and a `serves_intent` label pointing at nothing would silently weaken the
    re-derivation predicate to "no evidence, no finding"."""
    cases = load_cases()
    assert len(cases) == 10
    for case in cases:
        assert case.ground_truth_intent_count >= 1, case.id
        declared = {c.id for round_ in case.model_script for c in round_.tool_calls}
        for call_id, intent_id in case.serves_intent.items():
            assert call_id in declared, f"{case.id}: {call_id}"
            assert intent_id.startswith("i"), f"{case.id}: {intent_id}"
