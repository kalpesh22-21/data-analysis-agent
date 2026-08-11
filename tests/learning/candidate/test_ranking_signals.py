"""`SessionSignals` / `NoveltyStamp` — the two durable ranking stamps (plan §4).

Both are additive optional envelope fields, both are read back out of a Couchbase document
other things (and humans, via cbq) can write, and both feed arithmetic whose OUTPUT is a
sort key and a threshold comparison. That last fact is what the coercion guards are
derived from, not a remembered list of field names:

    reader                          operation                     ⇒ requirement
    ranking.session_quality         dict lookup on accepted_signal ⇒ str | None
                                    arithmetic on the counts       ⇒ non-negative int
    ranking.review_score            multiplication, then `>=`
                                    against `review_score_cutoff`
                                    and a `sorted()` key           ⇒ float in [0,1],
                                                                     never nan
    inbox.list                      the products are SORTED        ⇒ total ordering

A `nan` is the interesting one: every comparison with it is False, so it does not raise —
it silently makes the queue's order depend on the direction of comparison. It is clamped
rather than trusted.

Slugs:
  * S3-session-signals-stamped-at-extraction
  * S3-signals-round-trip-and-stay-optional
  * S3-signals-coerce-untrusted-json
"""

from __future__ import annotations

import pytest

from data_agent.learning.candidate.models import CandidateEnvelope, build_envelope
from data_agent.learning.candidate.signals import NoveltyStamp, SessionSignals
from data_agent.learning.summary.models import (
    AskUserExchange,
    BlueprintUsage,
    FailedFixedSql,
    SessionSummary,
    TurnSummary,
)

from ..extractor.helpers import KEEP_VERDICT, blueprint_raw, emit_extractor, make_summary


def _turn(i: int) -> TurnSummary:
    return TurnSummary(turn_index=i, user_nl=f"q{i}", assistant_text=f"a{i}", tool_call_refs=())


def _struggling_summary() -> SessionSummary:
    base = make_summary(session_id="s", content_hash="h")
    return SessionSummary(
        session_id=base.session_id,
        user_id=base.user_id,
        scope_ref=base.scope_ref,
        trace_id=base.trace_id,
        content_hash=base.content_hash,
        turns=(_turn(0), _turn(1), _turn(2)),
        tool_calls=base.tool_calls,
        blueprint_usages=(
            BlueprintUsage(
                tool_call_ref="tc-bp", blueprint_id="bp-x", status="ok", outcome="corrected"
            ),
        ),
        askuser_exchanges=(
            AskUserExchange(
                question_tool_call_ref="tc-ask", question="which department?",
                answer="finance", answer_turn_index=1,
            ),
        ),
        failed_fixed_sql=(
            FailedFixedSql(
                failed_tool_call_ref="tc-1", failed_sql="SELECT 1",
                fixed_tool_call_ref="tc-2", fixed_sql="SELECT 2",
            ),
        ),
        accepted_signal="explicit_confirm",
    )


async def test_build_envelope_stamps_the_session_signals():
    """THE only place the stamp can be written: the summary is an in-process value the
    consumer drops as soon as the extraction finishes, so a struggle signal that is not
    captured here does not exist by the time a human opens the inbox."""
    summary = _struggling_summary()
    extracted = await emit_extractor([blueprint_raw()]).extract(summary, KEEP_VERDICT)
    env = build_envelope(
        extracted.candidates[0], summary, candidate_id="candidate::s::0",
        evidence_refs=("ref-1",),
    )

    assert env.session_signals == SessionSignals(
        accepted_signal="explicit_confirm",
        turn_count=3,
        failed_fixed_count=1,
        askuser_count=1,
        corrected_blueprint=True,
    )


async def test_the_stamp_carries_no_transcript_content():
    """It travels to the review UI through `InboxItem`, which is the surface the D17
    redaction protects. Counts, one bool and one enum member carry no entity; a quoted
    question or a fragment of SQL would, and the type has no field one could go in.

    Asserted against a summary whose text is deliberately identifiable."""
    summary = _struggling_summary()
    extracted = await emit_extractor([blueprint_raw()]).extract(summary, KEEP_VERDICT)
    env = build_envelope(
        extracted.candidates[0], summary, candidate_id="candidate::s::0",
        evidence_refs=(),
    )

    rendered = str(env.session_signals.to_doc())
    assert "which department?" not in rendered
    assert "SELECT" not in rendered
    assert "finance" not in rendered


def test_the_stamps_round_trip_through_the_persisted_doc():
    env = CandidateEnvelope(
        candidate_id="c::1", type="blueprint", status="candidate", payload={},
        source_session="s", source_trace="t", evidence_refs=(), extractor_rationale="",
        entity_scan={}, confidence=0.5, proposed_action="new", depends_on=(),
        content_hash="h",
        session_signals=SessionSignals(
            accepted_signal="no_correction", turn_count=2, failed_fixed_count=1,
            askuser_count=0, corrected_blueprint=False,
        ),
        novelty=NoveltyStamp(novelty=0.4, measured=True, compared_against=3),
    )

    back = CandidateEnvelope.from_doc(env.to_doc())

    assert back.session_signals == env.session_signals
    assert back.novelty == env.novelty


def test_a_pre_slice_doc_round_trips_byte_identically():
    """The candidate bucket is durable and is never migrated. An envelope with neither
    stamp must emit neither key — the same additive-and-optional rule `traceparent`,
    `verified`, `last_scanned_at` and `judge` already follow."""
    env = CandidateEnvelope(
        candidate_id="c::1", type="blueprint", status="candidate", payload={},
        source_session="s", source_trace="t", evidence_refs=(), extractor_rationale="",
        entity_scan={}, confidence=0.5, proposed_action="new", depends_on=(),
        content_hash="h",
    )
    doc = env.to_doc()
    assert "session_signals" not in doc
    assert "novelty" not in doc
    assert CandidateEnvelope.from_doc(doc).session_signals is None
    assert CandidateEnvelope.from_doc(doc).novelty is None


@pytest.mark.parametrize("junk", ["a-string", 5, ["a"], True, None])
def test_a_non_dict_stamp_reads_as_nobody_looked_never_raises(junk):
    """Normalize-do-not-trust, the same posture `last_scanned_at` and `judge` take. A
    hand edit must not raise inside a cron scan or an inbox projection."""
    doc = {
        "candidate_id": "c::1", "type": "blueprint", "status": "candidate", "payload": {},
        "entity_scan": {}, "content_hash": "h",
        "session_signals": junk, "novelty": junk,
    }
    env = CandidateEnvelope.from_doc(doc)
    assert env.session_signals is None
    assert env.novelty is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (5, 5),
        (-1, 0),  # a negative count is not a count
        (True, 0),  # bool is an int subclass; `True` is not "one turn"
        ("7", 0),  # never `int("7")` — coercing would invent a value
        (1.9, 0),
        ({}, 0),
        (None, 0),
    ],
)
def test_counts_are_coerced_to_a_non_negative_int(raw, expected):
    signals = SessionSignals.from_doc({"turn_count": raw})
    assert signals.turn_count == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0.5, 0.5),
        (1.0, 1.0),
        (2.5, 1.0),  # clamped — one row must not be able to dominate the whole queue
        (-1.0, 0.0),
        (float("nan"), 0.0),  # the sort-key hazard: every comparison with nan is False
        (float("inf"), 1.0),
        (True, 0.0),
        ("0.5", 0.0),
        (None, 0.0),
    ],
)
def test_novelty_is_clamped_into_the_unit_interval(raw, expected):
    assert NoveltyStamp.from_doc({"novelty": raw}).novelty == expected


def test_a_zero_novelty_is_distinguishable_from_an_unmeasured_one():
    """The distinction the whole `measured` flag exists for. `novelty=0.0, measured=True`
    says an identical artifact is already landed — a real, strong claim. `measured=False`
    says the graph could not be consulted. Collapsing them would let a degraded read
    masquerade as a measurement, which is the failure `PriorArtUnavailableError` prevents
    one layer down."""
    landed_twin = NoveltyStamp(novelty=0.0, measured=True, compared_against=4)
    could_not_look = NoveltyStamp()
    assert landed_twin != could_not_look
    assert NoveltyStamp.from_doc(landed_twin.to_doc()) == landed_twin
    assert NoveltyStamp.from_doc(could_not_look.to_doc()) == could_not_look


def test_from_best_similarity_is_the_complement_and_stays_in_range():
    assert NoveltyStamp.from_best_similarity(0.0, compared_against=0).novelty == 1.0
    assert NoveltyStamp.from_best_similarity(1.0, compared_against=1).novelty == 0.0
    # An out-of-range score from a misbehaving index cannot produce a negative novelty.
    assert NoveltyStamp.from_best_similarity(1.7, compared_against=1).novelty == 0.0
    assert NoveltyStamp.from_best_similarity(-3.0, compared_against=1).novelty == 1.0
