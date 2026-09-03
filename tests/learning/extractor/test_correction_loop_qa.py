"""The extractor's corrective turn, driven by multi-turn scripts.

WHAT THESE TESTS PROVE, AND WHAT THEY DO NOT. Every model here is a
`ScriptedModelClient`: a fixed sequence of `ModelTurnResult`s that ignores what it is
told. So these prove that the correction is BUILT from the right declines, DELIVERED in
a message history a provider would accept, and that whatever comes back is PARSED,
merged and bounded. They prove nothing whatever about whether a real model reads the
correction and fixes the field — a scripted "model" that repairs itself on turn two is
a fixture asserting the loop's plumbing, not evidence of anything. The only evidence for
the second question is a live run, and it belongs in a probe, not here.

WHY THE LOOP EXISTS. `max_retries` used to fire only on `SchemaMismatchError` — "the
response was not a valid tool call at all". A tool call that parsed but whose CONTENTS
failed validation returned a terminal `Decline`, and the model was never told. So the
extractor could recover from "you did not call the tool" and could not recover from
"your candidate is the wrong shape", which across ten live sessions was the failure that
actually happened, every time.

Slug: PA-shape-correction-loop.
"""

from __future__ import annotations

import pytest

from data_agent.learning.extractor.schema import (
    EXTRACTOR_TOOL_NAME,
    SEARCH_CORPUS_TOOL_NAME,
    SchemaMismatchError,
)
from data_agent.learning.priorart import InMemoryPriorArtIndex
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest

from .helpers import (
    KEEP_VERDICT,
    blueprint_raw,
    make_extractor,
    make_summary,
    malformed_turn,
    scripted_turn,
    search_turn,
)

_RETRIES = 2
_CORRECTIONS = 2
_SEARCHES = 3

# Far more turns than any bound, so a loop that over-runs exhausts the script and fails
# loudly (`ScriptedModelClient` raises) instead of hanging.
_OVERLONG = 30


def _bad_grain(**kwargs) -> dict:
    """The live `gpt-5.5` failure: `result_signature.grain` as prose."""
    return blueprint_raw(
        result_signature={
            "shape": [{"column": "org_unit", "type": "String"}],
            "grain": "one row per organizational unit above the benchmark",
            "invariants": [],
        },
        **kwargs,
    )


def _good_grain(**kwargs) -> dict:
    return blueprint_raw(
        result_signature={
            "shape": [{"column": "org_unit", "type": "String"}],
            "grain": {"columns": ["org_unit"], "verifiable": True},
            "invariants": [],
        },
        **kwargs,
    )


def _tool_replies(recorded_messages: list[dict]) -> list[dict]:
    return [m for m in recorded_messages if m.get("role") == "tool"]


def _correction_text(extractor, turn_index: int = 1) -> str:
    """The correction the extractor put in front of the model on *turn_index*."""
    replies = _tool_replies(extractor._model_client.calls[turn_index].messages)
    assert replies, "no tool reply was appended — the correction was never delivered"
    return replies[-1]["content"]


# --- delivery: the model is actually told, in a history a provider accepts ---------


async def test_a_shape_decline_produces_a_second_turn_that_names_the_field() -> None:
    """The whole point. Before this, a shape decline was terminal and silent."""
    extractor = make_extractor([scripted_turn([_bad_grain()]), scripted_turn([_good_grain()])])

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    assert extractor._model_client.calls_made == 2
    correction = _correction_text(extractor)
    assert "candidate.payload.result_signature.grain" in correction
    assert "columns" in correction
    assert len(result.candidates) == 1
    assert result.declines == ()
    assert result.corrections == 1


async def test_missing_shape_type_correction_names_canonical_field_and_example() -> None:
    bad = blueprint_raw(
        result_signature={
            "shape": [{"column": "projected_month"}],
            "grain": {"columns": ["projected_month"], "verifiable": True},
            "invariants": [],
        }
    )
    good = blueprint_raw(
        result_signature={
            "shape": [{"column": "projected_month", "type": "date"}],
            "grain": {"columns": ["projected_month"], "verifiable": True},
            "invariants": [],
        }
    )
    extractor = make_extractor([scripted_turn([bad]), scripted_turn([good])])

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    correction = _correction_text(extractor)
    assert "result_signature.shape[0].type is required" in correction
    assert '"column": "projected_month", "type": "date"' in correction
    assert "shape[0].semantic_type" not in correction
    assert len(result.candidates) == 1
    assert result.declines == ()


async def test_the_correction_rides_on_a_tool_reply_so_no_call_is_left_dangling() -> None:
    """A provider rejects a follow-up whose history has an unanswered tool call, so the
    corrective turn cannot simply append a user message: the `emit_candidates` call the
    model just made has to be echoed and answered. Same rule `_serve_search_calls`
    follows, for the same reason."""
    extractor = make_extractor([scripted_turn([_bad_grain()]), scripted_turn([_good_grain()])])
    await extractor.extract(make_summary(), KEEP_VERDICT)

    history = extractor._model_client.calls[1].messages
    assistant = [m for m in history if m.get("role") == "assistant"]
    assert len(assistant) == 1
    call_ids = {c["id"] for c in assistant[0]["tool_calls"]}
    replied_to = {m["tool_call_id"] for m in _tool_replies(history)}
    assert replied_to == call_ids


async def test_a_search_call_beside_the_emit_is_also_answered_on_the_corrective_turn():
    """The emit WINS over a concurrent search (`_is_search_only`), so the search is
    never served — but it still has to be answered, or the history the correction rides
    on is the invalid kind."""
    turn = ModelTurnResult(
        tool_calls=[
            ToolCallRequest(id="s1", name=SEARCH_CORPUS_TOOL_NAME, arguments={"query": "q"}),
            ToolCallRequest(
                id="e1", name=EXTRACTOR_TOOL_NAME, arguments={"candidates": [_bad_grain()]}
            ),
        ]
    )
    index = InMemoryPriorArtIndex([])
    extractor = make_extractor(
        [turn, scripted_turn([_good_grain()])], prior_art=index
    )
    await extractor.extract(make_summary(), KEEP_VERDICT)

    replies = {m["tool_call_id"]: m["content"] for m in _tool_replies(
        extractor._model_client.calls[1].messages
    )}
    assert set(replies) == {"s1", "e1"}
    assert "not served" in replies["s1"]
    assert "result_signature.grain" in replies["e1"]
    assert len(index.search_calls) == 1  # the mandatory pre-fetch only


async def test_the_correction_says_omit_rather_than_invent() -> None:
    """The one instruction that keeps a corrective turn from becoming coercion. A model
    that cannot express its analysis in the required shape must be allowed to drop the
    candidate; the alternative is a shape-valid candidate whose content the model does
    not stand behind."""
    extractor = make_extractor([scripted_turn([_bad_grain()]), scripted_turn([])])
    await extractor.extract(make_summary(), KEEP_VERDICT)

    correction = _correction_text(extractor)
    assert "omit" in correction
    assert "Do not change your analysis" in correction


async def test_the_correction_never_quotes_the_offending_value() -> None:
    """It is prompt text and it is recorded on the decline, so it obeys the same
    entity-free rule as the declines it is built from."""
    extractor = make_extractor([scripted_turn([_bad_grain()]), scripted_turn([])])
    await extractor.extract(make_summary(), KEEP_VERDICT)
    assert "organizational unit" not in _correction_text(extractor)


# --- partial success: the accepted candidates are kept, not re-asked ---------------


async def test_a_good_candidate_is_kept_and_the_correction_asks_only_for_the_bad_one():
    """PARTIAL SUCCESS, the decision: keep the good, correct only the bad.

    Re-emitting the whole array to fix one sibling puts work that already cleared every
    gate back at risk — a model told it made a mistake will restructure things nobody
    complained about — and the trade is asymmetric: a duplicate from a model that
    re-sends anyway is a review-queue nuisance S6 dedup already handles, while a
    regressed good candidate is silent loss."""
    good = _good_grain(intent="headcount by department as of a period")
    bad = _bad_grain(intent="total earnings for a department in a year")
    extractor = make_extractor(
        [scripted_turn([good, bad]), scripted_turn([_good_grain()])]
    )

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    correction = _correction_text(extractor)
    assert "candidate 2" in correction  # 1-based, the position in the emitted array
    assert "candidate 1" not in correction
    assert "Do NOT repeat the 1 candidate" in correction
    assert len(result.candidates) == 2  # the kept one plus the corrected one
    assert result.declines == ()


async def test_a_substantive_decline_beside_a_shape_one_is_not_re_asked() -> None:
    """Three candidates, three fates in one turn: one accepted, one declined on CONTENT
    (no evidence — a judgement), one declined on SHAPE. Only the third is corrected, and
    the second survives into the result untouched."""
    extractor = make_extractor(
        [
            scripted_turn([_good_grain(), blueprint_raw(evidence=[]), _bad_grain()]),
            scripted_turn([_good_grain()]),
        ]
    )

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    correction = _correction_text(extractor)
    assert "candidate 3" in correction
    assert "candidate 2" not in correction
    assert [d.reason for d in result.declines] == ["no_evidence"]
    assert result.declines[0].corrections_attempted == 0  # never re-asked, and it shows
    assert len(result.candidates) == 2


async def test_two_bad_candidates_are_corrected_in_one_turn() -> None:
    """One corrective turn carries every shape decline from the batch, not one turn
    each — the correction is per-BATCH, so the budget is not a function of how many
    candidates the model happened to emit."""
    extractor = make_extractor(
        [scripted_turn([_bad_grain(), _bad_grain()]), scripted_turn([_good_grain()])]
    )
    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    correction = _correction_text(extractor)
    assert "candidate 1" in correction and "candidate 2" in correction
    assert "2 candidates of the 2 you emitted" in correction
    assert result.corrections == 1
    assert len(result.candidates) == 1


# --- when to stop, and what the final decline records ------------------------------


async def test_a_model_that_fails_the_same_way_three_times_still_declines() -> None:
    """The stopping rule. 1 emit + 2 corrections, then the decline stands."""
    extractor = make_extractor([scripted_turn([_bad_grain()]) for _ in range(3)])

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    assert extractor._model_client.calls_made == 1 + _CORRECTIONS
    assert result.candidates == ()
    assert len(result.declines) == 1
    assert result.declines[0].reason == "malformed_candidate"


async def test_the_final_decline_records_that_correction_was_attempted_and_what_was_said():
    """"The model could not produce a valid candidate" has to be distinguishable from
    "the model was never asked twice" — otherwise every shape decline in the inbox
    reads the same and nobody can tell a stubborn model from a disabled budget."""
    extractor = make_extractor([scripted_turn([_bad_grain()]) for _ in range(3)])
    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    decline = result.declines[0]
    assert decline.corrections_attempted == _CORRECTIONS
    assert len(decline.correction_history) == _CORRECTIONS
    assert all("result_signature.grain" in message for message in decline.correction_history)
    assert result.corrections == _CORRECTIONS


async def test_the_final_decline_carries_the_payload_it_was_judged_on() -> None:
    """Fail-to-review needs the model's LAST attempt, and this is the only place both
    halves exist at once: `pending` holds each decline's index into the array the model
    last emitted, and that array is gone by the time the consumer sees the result.

    The pairing is by INDEX, so it is asserted against the second candidate of a
    two-candidate batch — an off-by-one that always picked the first would pass a
    single-candidate test forever."""
    first = _bad_grain(intent="first candidate")
    second = _bad_grain(intent="second candidate")
    extractor = make_extractor([scripted_turn([first, second]) for _ in range(3)])

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    assert len(result.declines) == 2
    assert [d.raw_payload["payload"]["intent"] for d in result.declines] == [
        "first candidate",
        "second candidate",
    ]
    # A COPY, not the emitted dict: the raw array belongs to the parse and nothing
    # downstream may mutate it through the decline.
    assert result.declines[0].raw_payload is not first


async def test_an_unasked_substantive_decline_carries_no_payload() -> None:
    """A candidate that was never re-asked has no "last attempt" distinct from its
    first, and the fail-to-review route must not be able to treat the two alike — a
    merit-failed decline is supposed to die, and it dies with nothing attached."""
    extractor = make_extractor([scripted_turn([blueprint_raw(evidence=[])])])
    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    assert [d.reason for d in result.declines] == ["no_evidence"]
    assert result.declines[0].raw_payload is None


async def test_the_payload_survives_the_model_going_silent_after_a_correction() -> None:
    """The SECOND `_finish` call site — the asymmetric exit where the model stops
    returning parseable calls after a correction. It is the run whose last attempt is
    most worth keeping, and it is reached by a different path, so it is asserted
    separately rather than assumed to share the first one's behaviour."""
    extractor = make_extractor(
        [scripted_turn([_bad_grain(intent="the last attempt")])]
        + [malformed_turn() for _ in range(_RETRIES + 1)]
    )

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    assert len(result.declines) == 1
    assert result.declines[0].raw_payload["payload"]["intent"] == "the last attempt"


# --- the withdrawn candidate: the sanctioned omit still leaves a trace -------------


async def test_a_re_emit_that_omits_everything_still_declines_what_was_asked_for() -> None:
    """The live loss (session s8e36f59b...), reduced to the two candidates the correction
    named: there the re-emit was `{"candidates": []}` and every named candidate vanished.
    No decline, no audit, `decline_count=0` on the span, and "could not produce a valid
    candidate" became indistinguishable from "was never proposed", which is the ONE
    distinction the whole correction record exists to keep. Omitting is a sanctioned exit
    ("omit that candidate entirely"), not a licence to erase the ask."""
    first = _bad_grain(intent="first candidate")
    second = _bad_grain(intent="second candidate")
    extractor = make_extractor([scripted_turn([first, second]), scripted_turn([])])

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    correction = _correction_text(extractor)
    assert result.candidates == ()
    assert result.corrections == 1
    assert len(result.declines) == 2
    assert all(d.corrections_attempted == 1 for d in result.declines)
    assert all(d.correction_history == (correction,) for d in result.declines)
    # Paired against the array the correction POINTED AT, not the (empty) re-emit.
    assert [d.raw_payload["payload"]["intent"] for d in result.declines] == [
        "first candidate",
        "second candidate",
    ]


async def test_a_partial_re_emit_keeps_the_fix_and_declines_the_omission() -> None:
    """The mixed answer, and the pairing rule that reads it: the correction asked for
    "ONLY the corrected N listed above" in order, so re-emitted position i answers
    pending[i] and positions past the end were omitted. One fix comes back, so the
    withdrawn one is the SECOND — asserted by payload, because an implementation that
    always blamed the first would pass a count-only test forever."""
    first = _bad_grain(intent="first candidate")
    second = _bad_grain(intent="second candidate")
    extractor = make_extractor(
        [scripted_turn([first, second]), scripted_turn([_good_grain()])]
    )

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    assert len(result.candidates) == 1
    assert len(result.declines) == 1
    withdrawn = result.declines[0]
    assert withdrawn.raw_payload["payload"]["intent"] == "second candidate"
    assert withdrawn.corrections_attempted == 1
    assert withdrawn.reason == "malformed_candidate"  # its ORIGINAL reason, unchanged


async def test_the_withdrawn_payload_is_found_by_emitted_index_not_by_pending_position():
    """The off-by-one this pairing can actually have. `pending` holds `(index, decline)`
    where the index is the position in the EMITTED array, and those two coincide for
    every batch whose bad candidates start at 0 — so a lookup by position in `pending`
    passes all of those and fails the live shape, where a good candidate sits in front.
    Here `pending == [(1, first), (2, second)]`: the withdrawn one is `second`, at index
    2, and a position-based implementation would report `first` instead."""
    first = _bad_grain(intent="first candidate")
    second = _bad_grain(intent="second candidate")
    extractor = make_extractor(
        [
            scripted_turn([_good_grain(intent="kept"), first, second]),
            scripted_turn([_good_grain(intent="fixed first")]),
        ]
    )

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    assert len(result.candidates) == 2  # the one accepted up front plus the fix
    assert len(result.declines) == 1
    assert result.declines[0].raw_payload["payload"]["intent"] == "second candidate"


async def test_a_withdrawal_survives_a_later_correction_and_the_model_going_silent():
    """Both losing paths at once. The first correction is answered partially — one
    candidate withdrawn — the survivor earns a SECOND correction, and then the model
    stops returning parseable tool calls, which exits through the other `_finish` call
    site. The withdrawal happened two turns and one rebind of `raw_candidates` ago, so
    if it is not banked at the moment of withdrawal it is gone."""
    first = _bad_grain(intent="first candidate")
    second = _bad_grain(intent="second candidate")
    extractor = make_extractor(
        [
            scripted_turn([first, second]),
            scripted_turn([_bad_grain(intent="the last attempt")]),
        ]
        + [malformed_turn() for _ in range(_RETRIES + 1)]
    )

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    assert result.corrections == _CORRECTIONS
    assert result.candidates == ()
    # Withdrawn first, then the one that outlived the budget (`_finish`'s documented order).
    assert [d.raw_payload["payload"]["intent"] for d in result.declines] == [
        "second candidate",
        "the last attempt",
    ]
    # The withdrawn one was asked ONCE — it was gone before the second correction was
    # written — while the survivor was asked both times. `result.corrections` is the run
    # total; `corrections_attempted` is per candidate, and here they differ.
    assert [d.corrections_attempted for d in result.declines] == [1, _CORRECTIONS]
    assert [len(d.correction_history) for d in result.declines] == [1, _CORRECTIONS]


async def test_a_re_emit_longer_than_the_correction_asked_for_withdraws_nothing() -> None:
    """The other side of the pairing rule, and the one an off-by-one gets wrong. Two
    candidates are named and THREE come back — two repairs and an uninvited extra. There
    is no withdrawal to record (`pending[len(re_emitted):]` is empty when the re-emit is
    longer), and the extra is not special: it is validated like any other candidate in
    the batch, and when it fails it earns the NEXT correction rather than a withdrawal.
    An implementation that indexed the re-emit by `pending` instead of the other way
    round would either lose the extra or blame it for a sibling's fault."""
    extractor = make_extractor(
        [
            scripted_turn(
                [_bad_grain(intent="first candidate"), _bad_grain(intent="second candidate")]
            ),
            scripted_turn(
                [
                    _good_grain(intent="first, repaired"),
                    _good_grain(intent="second, repaired"),
                    _bad_grain(intent="an extra nobody asked for"),
                ]
            ),
            scripted_turn([_bad_grain(intent="the extra, still wrong")]),
        ]
    )

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    assert result.corrections == _CORRECTIONS
    assert sorted(c.payload.intent for c in result.candidates) == [
        "first, repaired",
        "second, repaired",
    ]
    # ONE decline — the extra, carried through the normal correction path. Two would
    # mean a withdrawal was invented for a candidate the model actually re-emitted.
    assert len(result.declines) == 1
    assert result.declines[0].raw_payload["payload"]["intent"] == "the extra, still wrong"
    assert result.declines[0].corrections_attempted == _CORRECTIONS


async def test_a_full_re_emit_that_is_still_bad_withdraws_nothing_and_banks_nothing_twice():
    """The stubborn model, which is the case the withdrawn bookkeeping most easily
    double-counts: every candidate comes back every time, so nothing is ever withdrawn,
    and the two declines at the end must be the two candidates — not four, and not the
    first turn's payloads. `withdrawn` accumulates ACROSS corrections while `pending` is
    rebound each round, so an implementation that banked the pending tail unconditionally
    (or forgot to rebind) would show its arithmetic here and nowhere else."""
    extractor = make_extractor(
        [
            scripted_turn(
                [_bad_grain(intent=f"first, attempt {n}"), _bad_grain(intent=f"second, attempt {n}")]
            )
            for n in (1, 2, 3)
        ]
    )

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    assert result.corrections == _CORRECTIONS
    assert result.candidates == ()
    assert len(result.declines) == 2
    # The LAST attempt, because none of these was withdrawn: they all survived.
    assert [d.raw_payload["payload"]["intent"] for d in result.declines] == [
        "first, attempt 3",
        "second, attempt 3",
    ]
    assert all(d.corrections_attempted == _CORRECTIONS for d in result.declines)


async def test_a_malformed_turn_between_the_correction_and_the_omit_keeps_one_trace() -> None:
    """The correction and the answer to it need not be adjacent. A malformed turn in
    between spends a PARSE attempt and appends its own nudge, leaving `pending` and the
    array it indexes into untouched — so the re-emit two turns later is still the answer
    to the correction, and the omission is still a withdrawal. Asserted as an exact list
    because the failure mode of a banking step in the wrong place is a DUPLICATE, which
    a `len(declines) >= 2` test would happily accept."""
    extractor = make_extractor(
        [
            scripted_turn(
                [_bad_grain(intent="first candidate"), _bad_grain(intent="second candidate")]
            ),
            malformed_turn(),
            scripted_turn([]),
        ]
    )

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    correction = _correction_text(extractor)
    assert extractor._model_client.calls_made == 3
    assert result.corrections == 1
    assert [d.raw_payload["payload"]["intent"] for d in result.declines] == [
        "first candidate",
        "second candidate",
    ]
    assert all(d.corrections_attempted == 1 for d in result.declines)
    assert all(d.correction_history == (correction,) for d in result.declines)


async def test_a_search_between_the_correction_and_the_omit_keeps_one_trace() -> None:
    """The same interleaving through the OTHER `continue` in the loop. A search-only turn
    after a correction is served and costs a search, not a correction — and it is the one
    branch that rebuilds `messages` wholesale, so it is the likeliest place for the
    correction's pending set to be dropped on the floor. The re-emit after it still
    answers the correction, and the omission is still recorded exactly once.

    Reachable, unlike a search AT the emit: `_is_search_only` only lets the emit win when
    the SAME turn carries both, and nothing stops a corrected model from asking first."""
    index = InMemoryPriorArtIndex([])
    extractor = make_extractor(
        [
            scripted_turn(
                [_bad_grain(intent="first candidate"), _bad_grain(intent="second candidate")]
            ),
            search_turn("how has this been modelled before"),
            scripted_turn([]),
        ],
        prior_art=index,
    )

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    assert len(index.search_calls) == 2  # the mandatory pre-fetch plus the served one
    assert result.corrections == 1
    assert [d.raw_payload["payload"]["intent"] for d in result.declines] == [
        "first candidate",
        "second candidate",
    ]
    assert all(d.corrections_attempted == 1 for d in result.declines)


async def test_each_withdrawal_pairs_with_the_array_that_named_it_not_the_first_one():
    """WHY the pairing is resolved at withdrawal time rather than at `_finish`. Two
    corrections, one candidate dropped by each: the first withdrawal points into the
    ORIGINAL emit, the second into the FIRST RE-EMIT — an array that no longer exists by
    the time the result is assembled, and that a `_finish`-side implementation could only
    reach for by keeping the wrong one. The second decline's payload is therefore the
    model's own second attempt, which is the attempt a reviewer needs to see.

    The correction RECORD is resolved at the same moment and for the same reason, and it
    is PER CANDIDATE: the one dropped after the first correction was asked ONCE and says
    so, while the one that survived to be dropped after the second says 2. A run-total
    would claim the first candidate had been asked twice, which is the exact "was it
    actually re-asked?" question the field exists to answer, and which inbox.html renders
    to a reviewer as "after N corrective rounds"."""
    extractor = make_extractor(
        [
            scripted_turn(
                [_bad_grain(intent="first candidate"), _bad_grain(intent="second candidate")]
            ),
            scripted_turn([_bad_grain(intent="a lone second attempt")]),  # drops the second
            scripted_turn([]),  # drops what is left
        ]
    )

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    assert result.corrections == _CORRECTIONS
    assert result.candidates == ()
    assert [d.raw_payload["payload"]["intent"] for d in result.declines] == [
        "second candidate",  # named in the original emit
        "a lone second attempt",  # named in the first RE-emit
    ]
    # What each was ASKED, not what the run spent: dropped after correction 1 vs 2.
    assert [d.corrections_attempted for d in result.declines] == [1, 2]
    assert [len(d.correction_history) for d in result.declines] == [1, 2]
    # The earlier record is a PREFIX of the later one — same run, read at two moments.
    assert result.declines[1].correction_history[:1] == result.declines[0].correction_history


async def test_a_withdrawn_entry_that_was_never_a_dict_degrades_to_no_payload() -> None:
    """The honest degrade, on the path that reaches it. `parse_candidates` checks that
    `candidates` is a LIST and nothing about its elements, so a model can put a bare
    string in the array; it declines `malformed_candidate`, which is CORRECTABLE, so it
    is named in a correction like any other — and when the model then omits it, the
    withdrawal has to pair a decline with an entry that was never a payload.

    The cost of getting this wrong is not a bad review item, it is a `TypeError` inside a
    queue worker taking the whole session with it, which is why the guard is a type check
    and not just a bounds check. A decline with no payload costs one review item; a crash
    costs the session, and every candidate beside it — asserted here by the sibling, which
    keeps its own payload intact."""
    extractor = make_extractor(
        [
            scripted_turn([_bad_grain(intent="a real candidate"), "not a dict at all"]),
            scripted_turn([]),
        ]
    )

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    assert result.corrections == 1
    assert [d.reason for d in result.declines] == ["malformed_candidate"] * 2
    assert result.declines[0].raw_payload["payload"]["intent"] == "a real candidate"
    assert result.declines[1].raw_payload is None
    # Still STAMPED, though: the correction record is what says it was asked for, and
    # that is true whether or not there was a payload to bring back.
    assert all(d.corrections_attempted == 1 for d in result.declines)
async def test_a_substantive_decline_beside_a_withdrawn_one_is_still_unstamped() -> None:
    """The distinction the stamp exists to keep, at its narrowest: two declines out of one
    batch, one of which was re-asked and one of which must never look as though it was. The
    merit-failed candidate keeps a zero and a `None` payload — it is supposed to die, and a
    reader that saw a correction record on it would route it to a human to complete a form
    whose content the extractor already judged unfit."""
    extractor = make_extractor(
        [
            scripted_turn(
                [blueprint_raw(evidence=[]), _bad_grain(intent="the one that was re-asked")]
            ),
            scripted_turn([]),
        ]
    )

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    # `_finish`'s documented order: substantive first, then withdrawn.
    assert [d.reason for d in result.declines] == ["no_evidence", "malformed_candidate"]
    unasked, withdrawn = result.declines
    assert unasked.corrections_attempted == 0
    assert unasked.correction_history == ()
    assert unasked.raw_payload is None
    assert withdrawn.corrections_attempted == 1
    assert withdrawn.raw_payload["payload"]["intent"] == "the one that was re-asked"

async def test_a_disabled_budget_declines_with_a_zero_that_means_never_asked() -> None:
    """The other side of the same distinction, and the pre-slice behaviour: with
    `max_shape_corrections=0` the shape decline is terminal, one turn is spent, and the
    record says plainly that nothing was tried."""
    extractor = make_extractor([scripted_turn([_bad_grain()])], max_shape_corrections=0)
    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    assert extractor._model_client.calls_made == 1
    assert result.corrections == 0
    assert result.declines[0].correctable is True  # it COULD have been corrected
    assert result.declines[0].corrections_attempted == 0  # it simply never was
    assert result.declines[0].correction_history == ()


@pytest.mark.parametrize("budget", [0, -1])
async def test_a_non_positive_correction_budget_never_takes_the_branch(budget: int) -> None:
    """A configuration edge, matching `max_search_calls`: a negative budget is a typo
    that degrades safely, not an outage. Validating it would turn a harmless
    misconfiguration into a dead extractor at process start."""
    extractor = make_extractor(
        [scripted_turn([_bad_grain()])], max_shape_corrections=budget
    )
    result = await extractor.extract(make_summary(), KEEP_VERDICT)
    assert extractor._model_client.calls_made == 1
    assert len(result.declines) == 1


async def test_a_second_correction_fires_for_a_second_independent_fault() -> None:
    """WHY the default is 2 rather than 1: `to_candidate` returns on the FIRST problem
    it finds, so a candidate with two independent shape faults needs two corrections to
    surface both. Here the model fixes the grain and the parameterization shape is
    still wrong underneath it."""
    two_faults = _bad_grain()
    two_faults["payload"]["parameterization"] = {"department": {"role": "slot"}}

    extractor = make_extractor(
        [
            scripted_turn([two_faults]),
            scripted_turn([_bad_grain()]),  # parameterization fixed, grain still prose
            scripted_turn([_good_grain()]),
        ]
    )
    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    assert result.corrections == 2
    assert len(result.candidates) == 1
    first, second = (
        _correction_text(extractor, 1),
        _correction_text(extractor, 2),
    )
    # In `_blueprint_payload` order: `parameterization` is read before
    # `result_signature`, so the first correction can only mention the first fault.
    assert "parameterization" in first
    assert "result_signature.grain" not in first
    assert "result_signature.grain" in second


# --- termination: the three budgets, and the total bound ---------------------------


_TOTAL_BOUND = _SEARCHES + (_RETRIES + 1) + _CORRECTIONS


async def test_a_correction_turn_does_not_consume_a_parse_attempt() -> None:
    """Stated as an equality. The retry budget is for MALFORMED output; spending it on
    a candidate that parsed perfectly well and merely had a field of the wrong type
    would make the retry contract depend on how sloppy the model is. After one
    correction the model must still get its FULL `max_retries + 1` attempts."""
    script = [scripted_turn([_bad_grain()])] + [
        malformed_turn() for _ in range(_RETRIES + 1)
    ]
    extractor = make_extractor(script)
    await extractor.extract(make_summary(), KEEP_VERDICT)
    assert extractor._model_client.calls_made == 1 + (_RETRIES + 1)


async def test_a_malformed_response_does_not_consume_a_correction() -> None:
    """The mirror. A model that cannot call the tool must not be able to eat the budget
    meant for a model that called it with a mis-shaped argument."""
    script = (
        [malformed_turn() for _ in range(_RETRIES)]
        + [scripted_turn([_bad_grain()]) for _ in range(1 + _CORRECTIONS)]
    )
    extractor = make_extractor(script)
    result = await extractor.extract(make_summary(), KEEP_VERDICT)
    assert result.corrections == _CORRECTIONS
    assert extractor._model_client.calls_made == _RETRIES + 1 + _CORRECTIONS


_HOSTILE_SCRIPTS = {
    "always_the_same_bad_shape": [scripted_turn([_bad_grain()]) for _ in range(_OVERLONG)],
    "bad_shape_then_never_a_tool_call": (
        [scripted_turn([_bad_grain()])] + [malformed_turn() for _ in range(_OVERLONG)]
    ),
    "alternating_bad_shape_and_malformed": [
        t
        for _ in range(_OVERLONG)
        for t in (scripted_turn([_bad_grain()]), malformed_turn())
    ],
    "searches_then_bad_shape_forever": (
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id=f"s{i}", name=SEARCH_CORPUS_TOOL_NAME,
                                    arguments={"query": "q"})
                ]
            )
            for i in range(_SEARCHES)
        ]
        + [scripted_turn([_bad_grain()]) for _ in range(_OVERLONG)]
    ),
    "growing_pile_of_bad_shapes": [
        scripted_turn([_bad_grain() for _ in range(n + 1)]) for n in range(_OVERLONG)
    ],
}


@pytest.mark.parametrize("name", sorted(_HOSTILE_SCRIPTS))
async def test_every_hostile_script_terminates_inside_the_total_turn_bound(name: str) -> None:
    """The one property a third `while` branch has to have. Three counters, each
    strictly decreasing on its own branch and each guarding it, bound the loop at
    `max_search_calls + (max_retries + 1) + max_shape_corrections`. That argument is
    spread over four functions, so it is checked here empirically — the same standard
    `test_search_loop_termination_qa.py` set when the search branch was added."""
    extractor = make_extractor(
        list(_HOSTILE_SCRIPTS[name]), prior_art=InMemoryPriorArtIndex([])
    )
    try:
        await extractor.extract(make_summary(), KEEP_VERDICT)
    except SchemaMismatchError:
        pass  # the documented terminal failure for a model that never emits
    assert extractor._model_client.calls_made <= _TOTAL_BOUND, name


async def test_an_unparseable_response_after_a_correction_returns_rather_than_raises():
    """The one asymmetry in the loop, and the reason it is there. Exhausting the retry
    budget raises, which dead-letters the session — correct when nothing was ever
    validated. After a correction it is not: candidates have already been validated and
    held, and an OPTIONAL extra ask must never be able to cost more than it was asked
    for."""
    extractor = make_extractor(
        [scripted_turn([_good_grain(), _bad_grain()])]
        + [malformed_turn() for _ in range(_RETRIES + 1)]
    )

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    assert len(result.candidates) == 1  # the good one survived the model going silent
    assert [d.reason for d in result.declines] == ["malformed_candidate"]
    assert result.declines[0].corrections_attempted == 1


async def test_with_no_correction_issued_a_persistent_malformed_response_still_raises():
    """The pre-slice contract, unchanged: with nothing validated there is nothing to
    save, and the raise is what routes the session to dead-letter for a human."""
    extractor = make_extractor([malformed_turn() for _ in range(_RETRIES + 1)])
    with pytest.raises(SchemaMismatchError):
        await extractor.extract(make_summary(), KEEP_VERDICT)


# --- the untouched path ------------------------------------------------------------


async def test_a_clean_extraction_costs_exactly_one_turn_and_reports_zero_corrections():
    """The overwhelmingly common case, pinned so the correction machinery stays free
    when nothing is wrong — and so `corrections` is a usable prompt-quality signal
    rather than a counter that is always non-zero."""
    extractor = make_extractor([scripted_turn([blueprint_raw()])])
    result = await extractor.extract(make_summary(), KEEP_VERDICT)
    assert extractor._model_client.calls_made == 1
    assert result.corrections == 0
    assert len(result.candidates) == 1


async def test_a_content_judgement_alone_never_triggers_a_corrective_turn() -> None:
    """WHAT NOT TO CORRECT, end to end. An unknown rule id, a slot outside the touched
    columns, no evidence cited — each is a judgement about content, and re-asking risks
    talking a model into a candidate it correctly declined to make. The script has ONE
    turn, so a corrective turn here would fail loudly."""
    extractor = make_extractor([scripted_turn([blueprint_raw(evidence=[])])])
    result = await extractor.extract(make_summary(), KEEP_VERDICT)
    assert extractor._model_client.calls_made == 1
    assert [d.reason for d in result.declines] == ["no_evidence"]
    assert result.corrections == 0
