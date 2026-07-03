"""S2-accepted-signal-inference (D99, design §2) — the correctness-critical
first-match decision table + adversarial cases (matrix rows L2–L8).

A false-positive acceptance manufactures a blueprint candidate off an answer the
user never accepted, so the bias is conservative: ambiguous ⇒ None, correction
beats confirmation. `thumbs_up` is NEVER emitted in Phase 1.
"""

from __future__ import annotations

import pytest

from data_agent.learning.summary import load_session_summary
from data_agent.learning.triage import triage
from data_agent.runtime.session.memory_store import InMemorySessionStore

from .helpers import make_doc, make_job, make_message, make_trail_entry


@pytest.fixture
def store() -> InMemorySessionStore:
    return InMemorySessionStore()


async def _signal(store, doc):
    summary = await load_session_summary(doc, store, job=make_job(doc.session_id))
    return summary.accepted_signal


async def _summary(store, doc):
    return await load_session_summary(doc, store, job=make_job(doc.session_id))


def _ok_query_turn(turn: int, tool_call_id: str = "call_ok"):
    """An OK runQuery trail entry in *turn* — makes the turn an 'answer turn'."""
    return make_trail_entry(
        turn_index=turn, tool_call_id=tool_call_id, tool_name="runQuery",
        args={"sql": "SELECT sum(ot) FROM pay"}, status="ok",
    )


# --- Row 1: nothing successfully answered ⇒ None ----------------------------


async def test_no_answer_all_failed_is_none(store):
    doc = make_doc(
        messages=[make_message(0, "user", "show overtime"),
                  make_message(0, "assistant", "I hit an error.")],
        tool_trail=[make_trail_entry(turn_index=0, tool_name="runQuery",
                                     args={"sql": "SELCT 1"}, status="error",
                                     error_code="SYNTAX_ERROR")],
    )
    assert await _signal(store, doc) is None


async def test_chat_only_no_tool_calls_is_none(store):
    doc = make_doc(messages=[make_message(0, "user", "hello"),
                             make_message(0, "assistant", "hi there!")])
    assert await _signal(store, doc) is None


async def test_denied_only_no_success_is_none(store):
    # ADVERSARIAL: the session ended in a DENIED tool call with no success —
    # a failure must NOT be misread as acceptance (clean row-1 case).
    doc = make_doc(
        messages=[make_message(0, "user", "show salaries"),
                  make_message(0, "assistant", "Access denied.")],
        tool_trail=[make_trail_entry(turn_index=0, tool_name="runQuery",
                                     args={"sql": "SELECT salary FROM hr"},
                                     status="denied", error_code="SCOPE_DENIED")],
    )
    assert await _signal(store, doc) is None


# --- Row 4: no_correction (default clean success) ---------------------------


async def test_success_no_trailing_message_is_no_correction(store):
    # L2: successful answer, session went idle (no trailing user message).
    doc = make_doc(
        messages=[make_message(0, "user", "show overtime"),
                  make_message(0, "assistant", "Sales paid $12k overtime.")],
        tool_trail=[_ok_query_turn(0)],
    )
    assert await _signal(store, doc) == "no_correction"


async def test_success_trailing_non_corrective_is_no_correction(store):
    # L3: a trailing user message that is neither confirmation nor correction.
    doc = make_doc(
        messages=[make_message(0, "user", "show overtime"),
                  make_message(0, "assistant", "Sales paid $12k overtime."),
                  make_message(1, "user", "ok let's move on")],
        tool_trail=[_ok_query_turn(0)],
    )
    assert await _signal(store, doc) == "no_correction"


# --- Row 3: explicit_confirm ------------------------------------------------


@pytest.mark.parametrize("confirm_text", [
    "perfect, thanks",
    "yes",
    "that's right",
    "looks right",
    "great, thanks",
    "Correct!",
])
async def test_trailing_confirmation_is_explicit_confirm(store, confirm_text):
    # L4: a trailing confirmation-lexicon match (and no correction) ⇒ confirm.
    doc = make_doc(
        messages=[make_message(0, "user", "show overtime"),
                  make_message(0, "assistant", "Sales paid $12k overtime."),
                  make_message(1, "user", confirm_text)],
        tool_trail=[_ok_query_turn(0)],
    )
    assert await _signal(store, doc) == "explicit_confirm"


# --- Row 2: correction after the final answer ⇒ None ------------------------


@pytest.mark.parametrize("correction_text", [
    "no, that's wrong",
    "actually I meant net pay",
    "that should be gross pay",
    "incorrect",
    "not the sales team",
    "use headcount instead",
])
async def test_trailing_correction_is_none(store, correction_text):
    # L5: the last answer was corrected and not re-answered.
    doc = make_doc(
        messages=[make_message(0, "user", "show overtime"),
                  make_message(0, "assistant", "Sales paid $12k overtime."),
                  make_message(1, "user", correction_text)],
        tool_trail=[_ok_query_turn(0)],
    )
    assert await _signal(store, doc) is None


# --- Row 5 + ADVERSARIAL: ambiguity / politeness resolve to None ------------


async def test_both_confirmation_and_correction_is_none_correction_first(store):
    # L7 / ADVERSARIAL: a trailing message matching BOTH lexicons ⇒ None
    # (correction beats confirmation — the conservative bias).
    doc = make_doc(
        messages=[make_message(0, "user", "show overtime"),
                  make_message(0, "assistant", "Sales paid $12k overtime."),
                  make_message(1, "user", "yes, but actually that's wrong")],
        tool_trail=[_ok_query_turn(0)],
    )
    assert await _signal(store, doc) is None


async def test_polite_thanks_with_correction_is_none(store):
    # ADVERSARIAL: a "thanks, that ..." politeness must NOT be read as acceptance
    # when a correction is also present.
    doc = make_doc(
        messages=[make_message(0, "user", "show overtime"),
                  make_message(0, "assistant", "Sales paid $12k overtime."),
                  make_message(1, "user", "thanks, that helps but it should be net pay")],
        tool_trail=[_ok_query_turn(0)],
    )
    assert await _signal(store, doc) is None


async def test_correction_in_earlier_trailing_msg_still_none(store):
    # A correction anywhere AFTER the final answer wins, even if a later trailing
    # message is a bare confirmation.
    doc = make_doc(
        messages=[make_message(0, "user", "show overtime"),
                  make_message(0, "assistant", "Sales paid $12k overtime."),
                  make_message(1, "user", "actually that's not right"),
                  make_message(2, "user", "yes")],
        tool_trail=[_ok_query_turn(0)],
    )
    assert await _signal(store, doc) is None


# --- ADVERSARIAL: a later failed/denied turn is not misread as acceptance ----


async def test_success_then_later_unrelated_failure_is_no_correction(store):
    """A genuine success at turn 0, then a NEW question at turn 1 that FAILED. The
    failed turn is not an answer turn (no ok data call), so the final answer stays
    turn 0 and the neutral follow-up is not a correction ⇒ no_correction. The
    later failure is correctly NOT counted as an answer/acceptance of its own."""
    doc = make_doc(
        messages=[make_message(0, "user", "show sales overtime"),
                  make_message(0, "assistant", "Sales paid $12k."),
                  make_message(1, "user", "now show marketing revenue")],
        tool_trail=[
            _ok_query_turn(0, tool_call_id="call_ok"),
            make_trail_entry(turn_index=1, tool_call_id="call_fail", tool_name="runQuery",
                             args={"sql": "SELECT revenue FROM marketng"}, status="error",
                             error_code="UNKNOWN_TABLE"),
        ],
    )
    signal = await _signal(store, doc)
    assert signal == "no_correction"
    # And it must never be a positive confirmation off the failure.
    assert signal != "explicit_confirm"


async def test_success_then_later_failure_with_correction_is_none(store):
    # The same interleaving, but the trailing user message on the failed turn IS a
    # correction ⇒ None (corrections after the final answer are honored).
    doc = make_doc(
        messages=[make_message(0, "user", "show sales overtime"),
                  make_message(0, "assistant", "Sales paid $12k."),
                  make_message(1, "user", "no, that's wrong, I meant base pay")],
        tool_trail=[
            _ok_query_turn(0, tool_call_id="call_ok"),
            make_trail_entry(turn_index=1, tool_call_id="call_fail", tool_name="runQuery",
                             args={"sql": "SELECT x"}, status="error", error_code="E"),
        ],
    )
    assert await _signal(store, doc) is None


# --- L8: thumbs_up is NEVER emitted -----------------------------------------


# --- ADVERSARIAL (reviewer reproductions): HIGH-2 word-boundary + negation -----


async def test_dont_think_looks_right_is_none(store):
    """A CHALLENGED answer: "I don't think that looks right". It must NOT be
    explicit_confirm — "don't think" is a correction (correction-first ⇒ None) and
    the negation guard independently voids the "looks right" confirmation."""
    doc = make_doc(
        messages=[make_message(0, "user", "show overtime"),
                  make_message(0, "assistant", "Sales paid $12k overtime."),
                  make_message(1, "user", "I don't think that looks right")],
        tool_trail=[_ok_query_turn(0)],
    )
    signal = await _signal(store, doc)
    assert signal is None
    assert signal != "explicit_confirm"
    assert signal != "no_correction"


async def test_yesterday_does_not_match_yes_word_boundary(store):
    """HIGH-2: an incidental substring ("yesterday" contains "yes") must NOT
    produce a false explicit_confirm — matching is word-boundary."""
    doc = make_doc(
        messages=[make_message(0, "user", "show overtime today"),
                  make_message(0, "assistant", "Today: $12k."),
                  make_message(1, "user", "what about yesterday?")],
        tool_trail=[_ok_query_turn(0)],
    )
    signal = await _signal(store, doc)
    assert signal != "explicit_confirm"          # no false confirm off "yesterday"
    assert signal == "no_correction"             # a neutral follow-up, answer stands


async def test_challenged_total_is_none_and_not_a_k1_keep(store):
    """HIGH-3: "that can't be right, the total seems too high" is a correction ⇒
    None, and triage must NOT KEEP this session via K1 (no accepted blueprint off
    a challenged answer)."""
    doc = make_doc(
        messages=[make_message(0, "user", "total overtime?"),
                  make_message(0, "assistant", "Total overtime is $1.2M."),
                  make_message(1, "user", "that can't be right, the total seems too high")],
        tool_trail=[_ok_query_turn(0)],
    )
    summary = await _summary(store, doc)
    assert summary.accepted_signal is None
    verdict = triage(summary)
    # No K1 keep — K1 requires accepted_signal != None.
    assert not (verdict.decision == "keep" and verdict.reason == "K1")
    assert verdict.decision == "skip"
    assert verdict.reason == "skip_no_acceptance"


async def test_thanks_but_thats_wrong_is_none(store):
    # Both-phrases / politeness: "thanks, but that's wrong" ⇒ None (correction).
    doc = make_doc(
        messages=[make_message(0, "user", "show overtime"),
                  make_message(0, "assistant", "Sales paid $12k."),
                  make_message(1, "user", "thanks, but that's wrong")],
        tool_trail=[_ok_query_turn(0)],
    )
    assert await _signal(store, doc) is None


async def test_multiclause_correction_then_confirm_is_none(store):
    """A multi-clause trailing message where a correction clause coexists with an
    un-negated confirmation clause ("that's wrong, but this looks right"): a
    correction is present ⇒ None regardless (conservative), even though the
    clause-scoped negation guard would NOT suppress the second clause on its own."""
    doc = make_doc(
        messages=[make_message(0, "user", "show overtime"),
                  make_message(0, "assistant", "Sales paid $12k."),
                  make_message(1, "user", "that's wrong, but this looks right")],
        tool_trail=[_ok_query_turn(0)],
    )
    assert await _signal(store, doc) is None


async def test_pure_unnegated_confirmation_still_works(store):
    """Guard against over-suppression: a genuinely un-negated confirmation with NO
    correction ("this looks right") must still score explicit_confirm."""
    doc = make_doc(
        messages=[make_message(0, "user", "show overtime"),
                  make_message(0, "assistant", "Sales paid $12k."),
                  make_message(1, "user", "this looks right")],
        tool_trail=[_ok_query_turn(0)],
    )
    assert await _signal(store, doc) == "explicit_confirm"


async def test_thumbs_up_is_never_emitted_across_the_battery(store):
    """The loader's output range is {no_correction, explicit_confirm, None} — the
    declared `thumbs_up` enum value is never row-selected (no capture surface)."""
    docs = [
        make_doc("s1", messages=[make_message(0, "user", "q"),
                                 make_message(0, "assistant", "a")],
                 tool_trail=[_ok_query_turn(0)]),
        make_doc("s2", messages=[make_message(0, "user", "q"),
                                 make_message(0, "assistant", "a"),
                                 make_message(1, "user", "perfect")],
                 tool_trail=[_ok_query_turn(0)]),
        make_doc("s3", messages=[make_message(0, "user", "q"),
                                 make_message(0, "assistant", "a"),
                                 make_message(1, "user", "no, wrong")],
                 tool_trail=[_ok_query_turn(0)]),
        make_doc("s4", messages=[make_message(0, "user", "hi")]),
    ]
    for doc in docs:
        s = InMemorySessionStore()
        signal = (await load_session_summary(doc, s, job=make_job(doc.session_id))).accepted_signal
        assert signal in ("no_correction", "explicit_confirm", None)
        assert signal != "thumbs_up"
