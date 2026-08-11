"""The guard on the judge's untrusted response (plan §3b, `judge/schema.py`).

The judge's tool call is NEW untrusted model output, and it carries more authority than
any other model output in the learning loop: a `duplicate` at high confidence discards
an analyst's session before anything is written down. So the guards are derived from
what downstream code READS and the OPERATION it performs — not from the field names,
and not from the English sentence in the prompt. This suite is that derivation, one
test per row of `parse_assessment`'s table.

Slugs:
  * J-guard-verdict-closed-set   — the verdict must be a MEMBER of the stored
                                   vocabulary; an unknown one is not defaulted, because
                                   a fabricated verdict is indistinguishable in the
                                   store from one a judge gave.
  * J-guard-confidence-bool      — `True` passes `isinstance(x, int)` and `True >= 0.90`
                                   is a perfect false positive.
  * J-guard-confidence-range     — out of range is a REJECTION, not a clamp: clamping
                                   5.0 would turn a malfunction into maximal confidence.
  * J-guard-confidence-nan       — NaN fails every comparison AND serializes to invalid
                                   JSON, so it would read as "never drop" while
                                   poisoning the record.
  * J-guard-text-flattened       — `reason` is a log line and a JSON leaf; a newline
                                   forges a log record.
  * J-guard-fail-open            — every rejection returns None, which the caller turns
                                   into "extract exactly as today".
"""

from __future__ import annotations

import math

from data_agent.learning.audit.judgement import COVERAGE_VERDICTS
from data_agent.learning.judge.schema import (
    JUDGE_TOOL_NAME,
    VERDICT_ENUM,
    build_judge_tool,
    parse_assessment,
)
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest

from .helpers import free_text_turn, verdict_turn


def _args(**kwargs) -> ModelTurnResult:
    return verdict_turn(arguments=kwargs)


def test_a_well_formed_call_parses_every_field() -> None:
    parsed = parse_assessment(
        _args(
            verdict="existing-plus-delta",
            covered_by="bp::abc",
            reason="adds a second grouping column",
            confidence=0.62,
        )
    )
    assert parsed is not None
    assert parsed.verdict == "existing-plus-delta"
    assert parsed.covered_by == "bp::abc"
    assert parsed.reason == "adds a second grouping column"
    assert parsed.confidence == 0.62
    # The CALLER fills the tier from the real card; the model never asserts it.
    assert parsed.covered_by_tier == ""


def test_the_prompt_enum_is_derived_from_the_stored_vocabulary() -> None:
    """One definition, not a copy. A model can never be offered a verdict the record
    has no meaning for, and the parity is structural rather than asserted by eyeball —
    the mirror-constant drift this repo keeps re-learning (`NODE_KINDS`, `SLOT_TYPES`,
    `_TABLE_CONSUME_REF`)."""
    assert VERDICT_ENUM == list(COVERAGE_VERDICTS)
    schema = build_judge_tool()
    assert schema["parameters"]["properties"]["verdict"]["enum"] == list(COVERAGE_VERDICTS)


def test_no_tool_call_at_all_fails_open() -> None:
    assert parse_assessment(free_text_turn()) is None


def test_a_different_tool_fails_open() -> None:
    assert parse_assessment(verdict_turn(tool_name="emit_candidates")) is None


def test_non_object_arguments_fail_open() -> None:
    assert parse_assessment(verdict_turn(arguments=["duplicate"])) is None
    assert parse_assessment(verdict_turn(arguments="duplicate")) is None
    # `None` is built directly: the helper treats `arguments=None` as "use the default".
    null_args = ModelTurnResult(
        tool_calls=[ToolCallRequest(id="c", name=JUDGE_TOOL_NAME, arguments=None)]  # type: ignore[arg-type]
    )
    assert parse_assessment(null_args) is None


def test_an_unknown_verdict_is_refused_and_never_defaulted() -> None:
    """Not coerced to `new`. The record IS the dataset that decides whether composable
    blueprints are worth building; a verdict this code invented would be
    indistinguishable there from one a judge actually gave."""
    for bogus in ("dupe", "DUPLICATE", "", None, 1, ["duplicate"]):
        assert parse_assessment(_args(verdict=bogus, covered_by="", reason="", confidence=0.9)) is None


def test_a_missing_verdict_key_fails_open() -> None:
    assert parse_assessment(verdict_turn(arguments={"confidence": 0.99})) is None


def test_a_boolean_confidence_is_refused() -> None:
    """`bool` is an `int` subclass, so `True >= 0.90` is True — a perfect false positive
    at the top of the range, and the one that drops work."""
    parsed = parse_assessment(
        _args(verdict="duplicate", covered_by="bp::abc", reason="", confidence=True)
    )
    assert parsed is None


def test_an_out_of_range_confidence_is_refused_not_clamped() -> None:
    for bogus in (5.0, -1.0, 1.0000001, -0.0001):
        assert (
            parse_assessment(
                _args(verdict="duplicate", covered_by="bp::abc", reason="", confidence=bogus)
            )
            is None
        )


def test_the_range_endpoints_are_inclusive() -> None:
    for edge in (0.0, 1.0):
        parsed = parse_assessment(
            _args(verdict="new", covered_by="", reason="", confidence=edge)
        )
        assert parsed is not None and parsed.confidence == edge


def test_nan_and_infinity_are_refused() -> None:
    """NaN fails BOTH range comparisons, so the `0 <= v <= 1` test rejects it with no
    separate isnan check — and it must be rejected, because it fails every `>=` (reading
    as "never drop") while serializing to invalid JSON in the stored record."""
    for bogus in (float("nan"), float("inf"), float("-inf")):
        assert (
            parse_assessment(
                _args(verdict="duplicate", covered_by="bp::abc", reason="", confidence=bogus)
            )
            is None
        )
    assert math.isnan(float("nan"))  # the premise, stated


def test_an_int_too_large_for_a_float_is_refused() -> None:
    """`float(10**400)` raises OverflowError, which would escape into a queue worker."""
    assert (
        parse_assessment(
            _args(verdict="duplicate", covered_by="bp::abc", reason="", confidence=10**400)
        )
        is None
    )


def test_an_integer_confidence_is_accepted() -> None:
    parsed = parse_assessment(
        _args(verdict="duplicate", covered_by="bp::abc", reason="", confidence=1)
    )
    assert parsed is not None and parsed.confidence == 1.0


def test_reason_and_covered_by_degrade_rather_than_reject() -> None:
    """The split is not stylistic: `reason` and `covered_by` are only ever READ, while
    `verdict` and `confidence` are what the drop DECISION is computed from. A junk value
    in the first two must not throw away a usable verdict."""
    parsed = parse_assessment(
        _args(verdict="new", covered_by=None, reason={"a": 1}, confidence=0.4)
    )
    assert parsed is not None
    assert parsed.covered_by == ""
    assert parsed.reason == ""


def test_reason_is_flattened_to_one_line() -> None:
    """A newline in a log record forges a second record; a line/paragraph separator
    survives a naive `"\\n" not in text` check."""
    parsed = parse_assessment(
        _args(
            verdict="new",
            covered_by="",
            reason="line one\nline two line three\r\nfour",
            confidence=0.1,
        )
    )
    assert parsed is not None
    assert "\n" not in parsed.reason
    assert " " not in parsed.reason
    assert parsed.reason == "line one line two line three four"


def test_reason_and_covered_by_are_length_capped() -> None:
    """The audit doc has a TTL but no size limit of its own."""
    parsed = parse_assessment(
        _args(verdict="new", covered_by="x" * 5000, reason="y" * 20_000, confidence=0.1)
    )
    assert parsed is not None
    assert len(parsed.reason) <= 601  # 600 + the ellipsis
    assert len(parsed.covered_by) <= 161


def test_a_second_tool_call_in_the_same_turn_does_not_confuse_the_parser() -> None:
    result = ModelTurnResult(
        tool_calls=[
            ToolCallRequest(id="c0", name="somethingElse", arguments={"x": 1}),
            ToolCallRequest(
                id="c1",
                name=JUDGE_TOOL_NAME,
                arguments={
                    "verdict": "duplicate",
                    "covered_by": "bp::abc",
                    "reason": "same",
                    "confidence": 0.99,
                },
            ),
        ]
    )
    parsed = parse_assessment(result)
    assert parsed is not None and parsed.verdict == "duplicate"
