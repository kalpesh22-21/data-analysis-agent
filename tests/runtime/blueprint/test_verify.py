"""Layer-1: the D56 deterministic verify assertion (runblueprint-design §4).

`D56-wrong-grain-falls-back` (unit half): a wrong-grain result ⇒ assertion FAIL
(the fan-out canary). Pass / fail / skip cases; the grain_verifiable:false skip.
"""

from __future__ import annotations

from data_agent.runtime.blueprint.models import ResultGrain
from data_agent.runtime.blueprint.verify import verify_result


def _grain(*cols: str, verifiable: bool = True) -> ResultGrain:
    return ResultGrain(columns=tuple(cols), verifiable=verifiable)


def test_matching_grain_passes() -> None:
    out = verify_result(
        result_grain=_grain("Department"),
        row_count=5,
        distinct_grain_count=5,
        columns=["department", "overtime_pay"],
    )
    assert out.passed and out.grain_ok and out.grain_checked


def test_wrong_grain_fails_the_fanout_canary() -> None:
    out = verify_result(
        result_grain=_grain("Department"),
        row_count=10,  # fan-out double-count: more rows than distinct grain
        distinct_grain_count=5,
        columns=["department"],
    )
    assert out.passed is False
    assert out.grain_ok is False
    assert out.reason == "grain_mismatch"


def test_empty_grain_is_skipped_but_signature_still_checked() -> None:
    out = verify_result(
        result_grain=_grain(),  # no declared grain → row-count teeth skipped
        row_count=3,
        distinct_grain_count=None,
        columns=["x"],
    )
    assert out.passed is True
    assert out.grain_checked is False
    assert out.grain_ok is True


def test_grain_verifiable_false_skips_row_count_teeth() -> None:
    # A blueprint that declares its OWN result grain unverifiable (§4.2 skip rule).
    out = verify_result(
        result_grain=_grain("EmployeeCode", verifiable=False),
        row_count=99,
        distinct_grain_count=1,  # would fail if checked
        columns=["x"],
    )
    assert out.passed is True
    assert out.grain_checked is False


def test_declared_grain_but_no_probe_number_fails_closed() -> None:
    out = verify_result(
        result_grain=_grain("Department"),
        row_count=5,
        distinct_grain_count=None,  # probe absent for a verifiable grain
        columns=["x"],
    )
    assert out.passed is False
    assert out.reason == "grain_probe_missing"


def test_signature_mismatch_fails() -> None:
    out = verify_result(
        result_grain=_grain("Department"),
        row_count=5,
        distinct_grain_count=5,
        columns=["department", "overtime_pay"],
        expected_columns=["department", "overtime_pay", "headcount"],
    )
    assert out.passed is False
    assert out.signature_ok is False
    assert out.reason == "signature_mismatch"


def test_signature_match_passes() -> None:
    out = verify_result(
        result_grain=_grain("Department"),
        row_count=5,
        distinct_grain_count=5,
        columns=["department", "overtime_pay"],
        expected_columns=["department", "overtime_pay"],
    )
    assert out.passed is True and out.signature_ok is True
