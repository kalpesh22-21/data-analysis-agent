"""blueprint/verify.py — the D56 deterministic verify assertion (pure).

Every `runBlueprint` result passes a mandatory gate before the user sees it, and the
deterministic half is the TEETH: a wrong-grain number is shape-correct, so only a
code-computed check catches the fan-out double-count. The executor runs the scope-enforced
`SELECT count(), count(DISTINCT <grain cols>)` probe and hands the two numbers here.

Two parts, both computable today:
  1. GRAIN-INTEGRITY — `row_count == distinct_grain_count` against the DECLARED result
     grain. Skipped (vacuously ok) when the grain is empty OR declared unverifiable, since
     there is nothing to check against.
  2. RESULT SIGNATURE — column-shape match against the declared signature.

A FAILED assertion is the executor's signal to fall back to the raw loop; this module only
COMPUTES pass/fail and never returns a suspect answer itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .models import ResultGrain


@dataclass(frozen=True)
class VerifyOutcome:
    """The result of the D56 deterministic gate over one blueprint result."""

    passed: bool  # overall: safe to return (grain ok-or-skipped AND signature ok)
    grain_ok: bool  # the row-count teeth (True when skipped — vacuously ok)
    grain_checked: bool  # False when skipped (empty/unverifiable grain, §4.2)
    signature_ok: bool
    reason: str | None = None  # a stable machine tag for the first failure


def verify_result(
    *,
    result_grain: ResultGrain,
    row_count: int,
    distinct_grain_count: int | None,
    columns: Sequence[str],
    expected_columns: Sequence[str] | None = None,
) -> VerifyOutcome:
    """Compute the D56 deterministic gate. Pure — the executor supplies the probe
    numbers (`row_count`, `distinct_grain_count`) and the actual result `columns`.

    Args:
        result_grain: the blueprint's DECLARED result grain (§1.1).
        row_count: rows in the blueprint's final result.
        distinct_grain_count: `COUNT(DISTINCT <grain cols>)` from the probe;
            may be `None` when the grain check is skipped (no probe issued).
        columns: the actual result column names (order-significant).
        expected_columns: the declared result signature columns, if the blueprint
            stored one; `None` skips the signature-shape check.

    Returns:
        `VerifyOutcome` — `passed=False` on the FIRST failing assertion.
    """
    signature_ok, sig_reason = _check_signature(columns, expected_columns)

    grain_checked = bool(result_grain.columns) and result_grain.verifiable
    if not grain_checked:
        # §4.2 skip: no declared grain, or the blueprint declared its own grain
        # unverifiable — nothing to check the row-count against.
        return VerifyOutcome(
            passed=signature_ok,
            grain_ok=True,
            grain_checked=False,
            signature_ok=signature_ok,
            reason=None if signature_ok else sig_reason,
        )

    if distinct_grain_count is None:
        # A grain is declared+verifiable but no probe number was supplied — the
        # check cannot run, which is fail-CLOSED (never silently pass the teeth).
        return VerifyOutcome(
            passed=False,
            grain_ok=False,
            grain_checked=True,
            signature_ok=signature_ok,
            reason="grain_probe_missing",
        )

    grain_ok = row_count == distinct_grain_count
    reason: str | None = None
    if not grain_ok:
        reason = "grain_mismatch"  # the fan-out double-count canary
    elif not signature_ok:
        reason = sig_reason
    return VerifyOutcome(
        passed=grain_ok and signature_ok,
        grain_ok=grain_ok,
        grain_checked=True,
        signature_ok=signature_ok,
        reason=reason,
    )


def _check_signature(
    columns: Sequence[str], expected_columns: Sequence[str] | None
) -> tuple[bool, str | None]:
    if expected_columns is None:
        return True, None  # no declared signature → the shape check is a no-op
    if list(columns) == list(expected_columns):
        return True, None
    return False, "signature_mismatch"


__all__ = ["VerifyOutcome", "verify_result"]
