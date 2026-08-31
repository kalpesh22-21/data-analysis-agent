"""ADVERSARIAL: the two untrusted boundaries the parameterization judge's verdict crosses.

There are TWO, and the existing suite exercises only the first:

  1. `paramjudge/schema.py::parse_param_assessment` — what a MODEL returned. Well covered by
     `test_param_judge.py` for the shapes a model gets wrong by accident (wrong tool, wrong
     verdict, out-of-range confidence, an unknown finding class).
  2. `audit/judgement.py::ParamAssessment.from_doc` — what a STORE returned. This one has no
     tests at all, and it is the boundary with the weaker upstream: a candidate document is
     hand-editable through cbq, is written by a previous release's code, and is read inside a
     LISTING endpoint that serves every row in the queue. Its own docstring names the stake —
     *"the alternative — raising inside a listing endpoint — turns one bad document into an
     unreadable queue"* — so this module asks whether that claim survives contact.

The house rule these are written against is `untrusted-json: derive the guard from the
downstream read`. Both boundaries feed the SAME three reads: a JSON leaf in a retained
document, an interpolation in a log line, and a field on an HTTP response body.
"""

from __future__ import annotations

import unicodedata

import pytest

from data_agent.learning.audit.judgement import (
    MAX_CRITERION_CHARS,
    MAX_FEEDBACK_CHARS,
    MAX_FINDINGS,
    MAX_NOTE_CHARS,
    ParamAssessment,
    ParamFinding,
    ParamJudgeRecord,
)
from data_agent.learning.paramjudge.schema import _clean, parse_param_assessment
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest

from .helpers import verdict_turn

# A lone high surrogate. Category `Cs`: it is not text, it survives every naive
# `"\n" not in s` check, and it is the one character class that `json.dumps` escapes
# back into safety while a UTF-8 encoder — which is what actually serializes an HTTP
# response — refuses outright.
LONE_SURROGATE = "\ud800"


def _turn(**arguments: object) -> ModelTurnResult:
    return ModelTurnResult(
        tool_calls=[
            ToolCallRequest(id="pj1", name="judge_parameterization", arguments=arguments)
        ]
    )


# --- boundary 1: what a model returned ---------------------------------------


def test_deeply_nested_junk_in_every_string_slot_degrades_rather_than_raising() -> None:
    """A model that answered a DIFFERENT schema's question. Every field that should be a
    string is a container instead, and `_clean` returns `""` for each rather than coercing
    (`str(raw)` would turn `None` into the literal "None" and a dict into its repr, both of
    which read as content)."""
    nested = {"a": [{"b": [{"c": ["deep"] * 5}]}]}
    assessment = parse_param_assessment(
        _turn(
            verdict="revise",
            feedback=nested,
            confidence=0.5,
            findings=[{"class": "A", "criterion": nested, "note": nested, "entry_index": 0}],
        ),
        entry_count=2,
    )
    # `revise` with no usable feedback is read as `ok` — a complaint nobody can act on.
    assert assessment is not None
    assert assessment.verdict == "ok"
    assert assessment.feedback == ""
    assert assessment.findings[0].criterion == ""
    assert assessment.findings[0].note == ""


def test_a_findings_list_of_ten_thousand_is_capped_without_walking_all_of_it() -> None:
    """The cap is a SLICE, not a filter-then-truncate: a runaway generation must not be able
    to make the parse boundary's cost proportional to the size of the generation."""
    assessment = parse_param_assessment(
        _turn(
            verdict="revise",
            feedback="one real complaint",
            confidence=0.5,
            findings=[
                {"class": "A", "criterion": f"c{i}", "note": f"n{i}"} for i in range(10_000)
            ],
        ),
        entry_count=1,
    )
    assert assessment is not None
    assert len(assessment.findings) == MAX_FINDINGS
    # The FIRST ones, not an arbitrary window: a model puts its strongest objection first.
    assert assessment.findings[0].criterion == "c0"


@pytest.mark.parametrize(
    "index",
    [-1, -10 ** 6, True, False, 1.0, "0", None, [0], {"i": 0}],
)
def test_an_entry_index_that_is_not_a_real_list_position_drops_to_none(index: object) -> None:
    """`entry_index` is read as `payload["parameterization"][i]` by the card. A negative
    index is the dangerous member of this set — Python would silently accept it and point the
    reviewer's highlight at the WRONG entry, counting from the end — and `bool` is the second,
    since `True` is an `int` and would index position 1."""
    assessment = parse_param_assessment(
        _turn(
            verdict="revise",
            feedback="f",
            confidence=0.5,
            findings=[{"class": "A", "criterion": "c", "note": "n", "entry_index": index}],
        ),
        entry_count=5,
    )
    assert assessment is not None
    assert assessment.findings[0].entry_index is None
    # ...and the FINDING survives, because a miscounted list position says nothing about
    # whether the objection is real. Only the pointer is dropped.
    assert assessment.findings[0].criterion == "c"


def test_a_confidence_too_large_for_a_float_fails_open_rather_than_overflowing() -> None:
    """JSON has arbitrary-precision integers and Python honours them; `float()` on one raises
    `OverflowError` inside a queue worker. The parse boundary already guards this — pinned so
    it stays guarded, and because the STORE boundary below does not."""
    assert parse_param_assessment(_turn(verdict="ok", feedback="", confidence=10**400)) is None


def test_a_lone_surrogate_in_feedback_is_flattened_like_every_other_non_text_class() -> None:
    """⚠ REGRESSION GUARD for a defect this module found. `Cs` was missing from this
    module's strip set while present in BOTH of
    the house implementations this one was modelled on:

        judge/schema.py            ("Cc", "Cf", "Cs", "Zl", "Zp")
        extractor/prior_art.py     ("Cc", "Cf", "Cs", "Zl", "Zp")
        paramjudge/schema.py       ("Cc", "Cf",       "Zl", "Zp")   ← was here
        revise/schema.py           ("Cc", "Cf",       "Zl", "Zp")   ← and here

    `prior_art.py` states the reason it is in that set: *"including `Cs` lone surrogates, which
    JSON encoding happens to escape today, but 'the serializer saves us' is not a property to
    depend on"* — and `tests/learning/extractor/test_prior_art_block_adversarial_qa.py` has
    carried a regression test for it since. On THIS path the serializer does not save us at
    all: the string reaches a FastAPI response body, which is UTF-8 encoded, and a lone
    surrogate is unencodable there. See `test_param_judge_view_d17_qa.py` for the
    endpoint-level effect — a 500 on the whole review queue, not on one row.

    Reachable exactly as the other four categories are: `json.loads('"\\ud800"')` yields the
    lone surrogate without complaint, and that is how `openai_client._safe_json_loads` builds
    the tool arguments.
    """
    assert unicodedata.category(LONE_SURROGATE) == "Cs"
    cleaned = _clean(f"before{LONE_SURROGATE}after", limit=MAX_FEEDBACK_CHARS)
    assert LONE_SURROGATE not in cleaned, (
        "a lone surrogate survived the judge's flattening — it is not text, it is not "
        "encodable as UTF-8, and both sibling implementations strip it"
    )
    assert cleaned == "before after"


def test_a_lone_surrogate_does_not_ride_the_verdict_into_the_store() -> None:
    """The same gap stated where it is observable: on the parsed assessment, which is what
    `ParamJudgeRecord.to_doc` serializes and what the envelope stamp carries."""
    assessment = parse_param_assessment(
        _turn(
            verdict="revise",
            feedback=f"record_type{LONE_SURROGATE} defines the metric",
            confidence=0.9,
            findings=[
                {"class": "A", "criterion": "x", "note": f"note{LONE_SURROGATE}", "entry_index": 0}
            ],
        ),
        entry_count=1,
    )
    assert assessment is not None
    assert LONE_SURROGATE not in assessment.feedback
    assert LONE_SURROGATE not in assessment.findings[0].note


def test_the_other_control_classes_are_flattened_as_documented() -> None:
    """The four that ARE in the set, pinned so a future edit cannot quietly narrow them:
    a NUL (Cc), a bidi override (Cf), a line separator (Zl) and a paragraph separator (Zp)."""
    cleaned = _clean("a\x00b‮c d e", limit=MAX_FEEDBACK_CHARS)
    assert cleaned == "a b c d e"


# --- boundary 2: what a store returned ---------------------------------------


def test_a_hand_edited_confidence_too_large_for_a_float_does_not_raise() -> None:
    """⚠ REGRESSION GUARD for the defect with the worst blast radius of the ones found here.

    `ParamAssessment.from_doc`'s own docstring says it normalizes *"rather than trusting"*,
    *"because the alternative — raising inside a listing endpoint — turns one bad document
    into an unreadable queue"*. It then calls `float(confidence)` inside the range test with
    no guard, and JSON integers are arbitrary-precision: one document with a 400-digit
    `confidence` makes `GET /inbox` return 500 for EVERY row, not just that one.

    The parse boundary next door gets this right (`_confidence` wraps the conversion in
    `try/except OverflowError`), which is what makes this an omission rather than a judgement
    call — the same author guarded the same conversion ten files away.
    """
    assessment = ParamAssessment.from_doc({"verdict": "ok", "confidence": 10**400})
    assert assessment.confidence == 0.0
    assert assessment.verdict == "ok"


def test_a_hand_edited_record_with_the_same_confidence_does_not_raise() -> None:
    """The same document read through the audit record, which is the surface the phase-D-1
    measurement is computed over — a reader that dies on one row reports nothing about the
    other 999."""
    record = ParamJudgeRecord.from_doc(
        {
            "record_type": "param_judgement",
            "judgement_ref": "paramjudge::h::c",
            "verdict": "revise",
            "feedback": "f",
            "confidence": 10**400,
            "findings": [],
        }
    )
    assert record is not None
    assert record.assessment.confidence == 0.0


def test_a_rehydrated_note_is_capped_like_a_parsed_one() -> None:
    """⚠ REGRESSION GUARD, second instance. `from_doc` capped `feedback` at
    `MAX_FEEDBACK_CHARS` and
    then caps NEITHER `note` nor `criterion`, for which the module defines bounds two
    constants away (`MAX_NOTE_CHARS`, `MAX_CRITERION_CHARS`).

    The bounds exist for a reason the module states: *"Every one of these is a JSON leaf in a
    retained document and an interpolation in a log line, so each is capped: a runaway
    generation must not be able to inflate the audit bucket one row at a time."* A rehydrated
    document is exactly the shape a runaway generation from a PREVIOUS release left behind,
    and this projection is what puts it back on the wire.
    """
    assessment = ParamAssessment.from_doc(
        {
            "verdict": "revise",
            "feedback": "f" * 10_000,
            "confidence": 0.5,
            "findings": [
                {"class": "A", "criterion": "c" * 10_000, "note": "n" * 10_000}
            ],
        }
    )
    assert len(assessment.feedback) == MAX_FEEDBACK_CHARS  # already correct
    assert len(assessment.findings[0].note) <= MAX_NOTE_CHARS
    assert len(assessment.findings[0].criterion) <= MAX_CRITERION_CHARS


def test_a_rehydrated_note_is_one_line_like_a_parsed_one() -> None:
    """⚠ REGRESSION GUARD, third instance, and the one the parse boundary calls out by name: *"a
    newline in a log line is a forged second log entry."* `_clean` strips control characters
    from a MODEL's note; `ParamFinding.from_doc` passes a STORE's note through `str()`
    untouched, so the same forged log entry arrives by the other door — and the stage's
    `_logger.warning` interpolates a rehydrated assessment's text on the would-discard path.
    """
    finding = ParamFinding.from_doc(
        {"class": "A", "criterion": "c", "note": "real\nWARNING param judge: all clear"}
    )
    assert "\n" not in finding.note


@pytest.mark.parametrize(
    "doc",
    [
        {},
        {"verdict": "APPROVE", "confidence": 0.9},
        {"verdict": "ok", "confidence": "0.9"},
        {"verdict": "ok", "confidence": True},
        {"verdict": "ok", "confidence": float("nan")},
        {"verdict": "ok", "confidence": float("inf")},
        {"verdict": "ok", "confidence": -1.0},
        {"verdict": "ok", "confidence": 5.0},
        {"verdict": ["ok"], "confidence": 0.5},
        {"verdict": "ok", "confidence": 0.5, "findings": "not a list"},
        {"verdict": "ok", "confidence": 0.5, "findings": [1, "two", None, []]},
        {"verdict": "ok", "confidence": 0.5, "findings": [{"class": {"nested": True}}]},
        {"verdict": "ok", "confidence": 0.5, "feedback": ["a", "list"]},
    ],
)
def test_every_damaged_document_reads_as_an_inert_verdict(doc: dict) -> None:
    """The listing endpoint's contract with the store: one bad row is a boring row.

    `ok` + `0.0` is the inert reading — the verdict that authorizes nothing and the confidence
    that clears no bar — so a document nobody can trust can never be mistaken for a judgement
    that said something.
    """
    assessment = ParamAssessment.from_doc(doc)
    assert assessment.verdict in ("ok", "revise", "reject")
    assert 0.0 <= assessment.confidence <= 1.0
    assert not assessment.has_class_a or assessment.verdict != "ok"


def test_an_unrecognised_class_in_a_stored_document_reads_as_the_weakest() -> None:
    """The down-cast is asserted at the PARSE boundary already; asserted here at the STORE
    boundary because `A` is the class that authorizes a phase-D-2 discard, and a document is
    the cheaper of the two ways to introduce one."""
    for raw in ("Z", "a", "", None, 1, True, ["A"], {"class": "A"}):
        finding = ParamFinding.from_doc({"class": raw, "criterion": "c", "note": "n"})
        assert finding.finding_class == "C", raw


def test_a_document_that_is_not_a_param_judgement_reads_as_absent() -> None:
    """`None` means "no verdict on file, judge it again" — the safe direction, since the cost
    is doing the work twice rather than trusting a row that could not be read."""
    assert ParamJudgeRecord.from_doc({"record_type": "judgement"}) is None
    assert ParamJudgeRecord.from_doc({"record_type": "param_judgement"}) is None  # no ref
    assert ParamJudgeRecord.from_doc({"record_type": "param_judgement", "judgement_ref": ""}) is None
    assert ParamJudgeRecord.from_doc("not a dict") is None  # type: ignore[arg-type]


# --- round trip ---------------------------------------------------------------


def test_a_verdict_survives_the_store_round_trip_unchanged() -> None:
    """`to_doc` → `from_doc` is an identity on well-formed data. Asserted because the two
    halves spell the key differently on purpose (`finding_class` in Python, `class` on the
    wire, since `class` is a keyword), and a one-sided rename would be silent."""
    original = ParamAssessment(
        verdict="reject",
        feedback="the intent names a metric the template does not compute",
        confidence=0.77,
        findings=(
            ParamFinding(finding_class="A", criterion="intent_mismatch", note="n", entry_index=2),
            ParamFinding(finding_class="B", criterion="slot_naming", note="x1", entry_index=None),
        ),
    )
    assert ParamAssessment.from_doc(original.to_doc()) == original
    assert original.has_class_a is True


def test_the_record_round_trips_through_its_document() -> None:
    record = ParamJudgeRecord(
        judgement_ref="paramjudge::h1::c1",
        candidate_id="c1",
        session_id="s1",
        content_hash="h1",
        trace_id="t1",
        assessment=ParamAssessment(verdict="revise", feedback="f", confidence=0.5),
        template="SELECT 1 WHERE d = {dept}",
        model="m",
        judged_at="2026-08-28T00:00:00+00:00",
        would_discard=False,
        shadow=True,
    )
    assert ParamJudgeRecord.from_doc(record.to_doc()) == record


def test_a_well_formed_verdict_is_not_disturbed_by_the_guards() -> None:
    """The complement of every test above: normal output passes through intact, so none of
    the hardening can be satisfied by a boundary that rejects everything."""
    assessment = parse_param_assessment(verdict_turn(), entry_count=2)
    assert assessment is not None
    assert assessment.verdict == "revise"
    assert assessment.confidence == 0.9
    assert assessment.findings[0].finding_class == "A"
    assert assessment.findings[0].entry_index == 1
