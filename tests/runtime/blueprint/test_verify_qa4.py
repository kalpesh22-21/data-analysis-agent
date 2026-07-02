"""QA4 Layer-1: D56 verify-gate edge cases (runblueprint-design §4).

Adversarial extensions of `test_verify.py`: 0-row / 1-row / duplicate-row grain
outcomes, the grain-columns-not-in-result gap (the gate trusts the probe number —
pinned), signature-vs-grain failure precedence, and a large-count perf smoke. The
gate is pure — it only compares the probe counts + column shape; it is the TEETH,
so a wrong-grain (fan-out) count must always FAIL closed.

ADD-only; does not modify the reviewer-owned `test_verify.py`.
"""

from __future__ import annotations

from data_agent.runtime.blueprint.models import ResultGrain
from data_agent.runtime.blueprint.verify import verify_result


def _grain(*cols: str, verifiable: bool = True) -> ResultGrain:
    return ResultGrain(columns=tuple(cols), verifiable=verifiable)


def test_zero_rows_and_zero_distinct_passes() -> None:
    out = verify_result(
        result_grain=_grain("Department"),
        row_count=0,
        distinct_grain_count=0,
        columns=["department"],
    )
    assert out.passed is True and out.grain_ok is True and out.grain_checked is True


def test_single_row_single_distinct_passes() -> None:
    out = verify_result(
        result_grain=_grain("Department"),
        row_count=1,
        distinct_grain_count=1,
        columns=["department"],
    )
    assert out.passed is True


def test_duplicate_rows_fail_the_fanout_canary() -> None:
    # 3 rows but only 1 distinct grain value = a duplicated/fanned-out result.
    out = verify_result(
        result_grain=_grain("Department"),
        row_count=3,
        distinct_grain_count=1,
        columns=["department"],
    )
    assert out.passed is False and out.reason == "grain_mismatch"


def test_distinct_greater_than_rowcount_also_fails() -> None:
    # An impossible/degenerate probe (distinct > rows) must still fail closed, not
    # silently pass — the gate asserts strict equality.
    out = verify_result(
        result_grain=_grain("Department"),
        row_count=2,
        distinct_grain_count=5,
        columns=["department"],
    )
    assert out.passed is False and out.grain_ok is False


def test_grain_column_absent_from_result_is_not_caught_by_the_pure_gate() -> None:
    # PIN (Slice B): `verify_result` does NOT assert the declared grain columns are
    # present in `columns` — it trusts the executor's probe number. Here the grain
    # is "EmployeeCode" but the result columns are only ["department"]; with a
    # matching probe count the gate PASSES. In Slice B the probe SQL referencing a
    # missing grain column would error and fall back, but the pure gate itself does
    # not defend this. Flagged as a Slice-B execution-side invariant.
    out = verify_result(
        result_grain=_grain("EmployeeCode"),
        row_count=5,
        distinct_grain_count=5,
        columns=["department"],
    )
    assert out.passed is True


def test_grain_mismatch_takes_precedence_over_signature_mismatch() -> None:
    out = verify_result(
        result_grain=_grain("Department"),
        row_count=10,
        distinct_grain_count=5,  # grain fails
        columns=["department"],
        expected_columns=["department", "extra"],  # signature also fails
    )
    assert out.passed is False
    assert out.reason == "grain_mismatch"  # grain reported first (it is the teeth)


def test_signature_reported_when_grain_ok_but_shape_wrong() -> None:
    out = verify_result(
        result_grain=_grain("Department"),
        row_count=5,
        distinct_grain_count=5,
        columns=["department"],
        expected_columns=["department", "headcount"],
    )
    assert out.passed is False
    assert out.grain_ok is True
    assert out.reason == "signature_mismatch"


def test_column_order_is_significant_in_signature() -> None:
    out = verify_result(
        result_grain=_grain(),
        row_count=1,
        distinct_grain_count=None,
        columns=["b", "a"],
        expected_columns=["a", "b"],  # same set, different order → mismatch
    )
    assert out.passed is False and out.reason == "signature_mismatch"


def test_large_count_equality_perf_smoke() -> None:
    out = verify_result(
        result_grain=_grain("Department"),
        row_count=5_000_000,
        distinct_grain_count=5_000_000,
        columns=["department"],
    )
    assert out.passed is True


def test_unverifiable_grain_skips_even_a_mismatching_probe() -> None:
    out = verify_result(
        result_grain=_grain("EmployeeCode", "pay_period", verifiable=False),
        row_count=100,
        distinct_grain_count=1,  # would be a gross mismatch if checked
        columns=["x"],
    )
    assert out.passed is True and out.grain_checked is False
