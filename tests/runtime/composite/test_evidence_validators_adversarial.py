"""Adversarial coverage for the evidence validators (Release 1, doc 04 §E).

The rows a hand-written validator gets wrong, and the ones that document what is
KNOWN-OPEN rather than pretending it is closed:

  - the two "no raise" paths — `result_preview is None` must be tested BEFORE the
    `.row_count` dereference, or a denied entry or a guard marker `AttributeError`s;
  - the `status="error"` blueprint denial, which the first draft rejected and
    which would have meant the primary route could not produce access evidence at
    all in a blueprint-first release;
  - manufactured evidence, asserted as PERMITTED so the hole is measured.
"""

from __future__ import annotations

from data_agent.runtime.composite.analysis_state import (
    validate_block_evidence,
    validate_completion_evidence,
)
from data_agent.runtime.context.assembly import IDEMPOTENT_READ_ALREADY_SERVED_CODE
from data_agent.runtime.session.models import (
    MODEL_REASON_CODES,
    RUNTIME_REASON_CODES,
    ResultPreview,
    TrailEntry,
)

TURN = 5


def _entry(
    tool_call_id: str,
    tool_name: str,
    *,
    status: str = "ok",
    error_code: str | None = None,
    row_count: int | None = 1,
    authoritative: bool = False,
    args: dict | None = None,
) -> TrailEntry:
    preview = (
        None
        if row_count is None
        else ResultPreview(
            columns=["x"], row_count=row_count, truncated=False, preview_rows=[]
        )
    )
    return TrailEntry(
        turn_index=TURN,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        args=args or {},
        status=status,
        error_code=error_code,
        provenance=frozenset(),
        result_preview=preview,
        result_full_ref=None,
        ts="2026-08-11T00:00:00+00:00",
        authoritative=authoritative,
    )


def test_no_access_accepts_a_blueprint_that_surfaced_as_status_error() -> None:
    """THE 04 §B.1 FIX. `runBlueprint` does not go through the dispatcher's
    `MCPToolError` path: an `ExecFailed` becomes `ToolResult(status="error")` with
    the inner code passed through verbatim, so a blueprint that hit
    `COLUMN_SCOPE_VIOLATION` internally persists `status="error"`. Gating on
    `status == "denied"` would mean the PRIMARY ROUTE of a blueprint-first release
    could never produce access evidence."""
    trail = [
        _entry(
            "call_bp",
            "runBlueprint",
            status="error",
            error_code="COLUMN_SCOPE_VIOLATION",
            row_count=None,
        )
    ]
    assert validate_block_evidence("call_bp", "NO_ACCESS", trail, TURN) is None


def test_no_access_rejects_a_successful_call() -> None:
    trail = [_entry("call_1", "runQuery")]
    reason = validate_block_evidence("call_1", "NO_ACCESS", trail, TURN)
    assert reason is not None
    assert "succeeded" in reason


def test_no_access_rejects_a_model_correctable_failure() -> None:
    """`CLICKHOUSE_QUERY_ERROR` is the model's own mistake, not a denial."""
    trail = [
        _entry(
            "call_1",
            "runQuery",
            status="error",
            error_code="CLICKHOUSE_QUERY_ERROR",
            row_count=None,
        )
    ]
    assert validate_block_evidence("call_1", "NO_ACCESS", trail, TURN) is not None


def test_no_access_rejects_database_not_allowed() -> None:
    """Dropped on review (04 §B.1): `DATABASE_NOT_ALLOWED` is `retryable=True` in
    `denial_mapping.py` — the codebase's own "the model got the name wrong"
    bucket — so it is not strong enough to terminate an intent as blocked."""
    trail = [
        _entry(
            "call_1",
            "runQuery",
            status="denied",
            error_code="DATABASE_NOT_ALLOWED",
            row_count=None,
        )
    ]
    assert validate_block_evidence("call_1", "NO_ACCESS", trail, TURN) is not None


def test_required_data_unavailable_rejects_table_not_found() -> None:
    """Also dropped: `TABLE_NOT_FOUND` is BOTH a typo and how a scope denial
    surfaces from `sampleRows` when the caller's column scope hides every column
    of the target — recording "the warehouse lacks this" for what is precisely
    "this user may not see this" inverts the one distinction governance reads."""
    trail = [
        _entry(
            "call_1",
            "sampleRows",
            status="denied",
            error_code="TABLE_NOT_FOUND",
            row_count=None,
        )
    ]
    assert (
        validate_block_evidence("call_1", "REQUIRED_DATA_UNAVAILABLE", trail, TURN) is not None
    )


def test_required_data_unavailable_citing_a_denied_entry_does_not_raise() -> None:
    """NO RAISE. A denied entry carries `result_preview=None`, so the null test
    must precede the `.row_count` dereference."""
    trail = [
        _entry(
            "call_1",
            "runQuery",
            status="denied",
            error_code="COLUMN_SCOPE_VIOLATION",
            row_count=None,
        )
    ]
    reason = validate_block_evidence("call_1", "REQUIRED_DATA_UNAVAILABLE", trail, TURN)
    assert reason is not None


def test_required_data_unavailable_citing_a_guard_marker_does_not_raise() -> None:
    """NO RAISE, second path: the idempotent-read guard marker is `status="ok"`
    AND `result_preview=None`, so it passes the status gate and would dereference
    `None` if the marker clause and the null test were both missing."""
    trail = [
        _entry(
            "call_repeat",
            "getTableSchema",
            error_code=IDEMPOTENT_READ_ALREADY_SERVED_CODE,
            row_count=None,
        )
    ]
    reason = validate_block_evidence("call_repeat", "REQUIRED_DATA_UNAVAILABLE", trail, TURN)
    assert reason is not None
    assert "duplicate" in reason


def test_a_zero_row_verified_blueprint_is_valid_for_both_validators() -> None:
    """The 04 §B.4 ambiguity, asserted so it is MEASURED rather than assumed away.

    An empty result set is simultaneously valid completion evidence ("nobody left
    last month") and valid block evidence (`REQUIRED_DATA_UNAVAILABLE`), so the
    model chooses — and `blocked` is cheaper: no prose, no table, no
    `answerWithTable`. That is not manufacture; it fires on honest work, and it
    makes coverage under-report on exactly the questions whose answer is "none".
    The mitigation is prompt plus the `loop_zero_row_block` /
    `loop_zero_row_completion` ratio, not a validator rule."""
    trail = [_entry("call_1", "runBlueprint", authoritative=True, row_count=0)]
    assert validate_completion_evidence("call_1", trail, TURN) is None
    assert validate_block_evidence("call_1", "REQUIRED_DATA_UNAVAILABLE", trail, TURN) is None


def test_runtime_forced_codes_are_never_model_declarable() -> None:
    """An ALLOWLIST, not a blocklist. 05's force-block path writes these directly
    and bypasses this validator entirely; routing them through here must fail."""
    trail = [
        _entry(
            "call_1",
            "runQuery",
            status="denied",
            error_code="COLUMN_SCOPE_VIOLATION",
            row_count=None,
        )
    ]
    for code in RUNTIME_REASON_CODES:
        reason = validate_block_evidence("call_1", code, trail, TURN)
        assert reason is not None, code
        assert "not a reason you may declare" in reason


def test_cut_reasons_and_empty_codes_are_rejected() -> None:
    """`NO_GROUNDED_SEMANTICS`, `NO_APPLICABLE_TOOL` and
    `USER_DECLINED_CLARIFICATION` were cut at Lead review — each proved an ATTEMPT
    rather than an outcome. The allowlist rejects them, plus `None`/`""`/junk, for
    free."""
    trail = [_entry("call_1", "runQuery", row_count=0)]
    for code in (
        "NO_GROUNDED_SEMANTICS",
        "NO_APPLICABLE_TOOL",
        "USER_DECLINED_CLARIFICATION",
        "",
        None,
        "no_access",
    ):
        assert validate_block_evidence("call_1", code, trail, TURN) is not None  # type: ignore[arg-type]


def test_only_two_codes_are_model_declarable() -> None:
    """Structural, not a comment: the split is what stops a FUTURE runtime code
    becoming model-declarable the day it lands."""
    assert MODEL_REASON_CODES == frozenset({"NO_ACCESS", "REQUIRED_DATA_UNAVAILABLE"})
    assert len(RUNTIME_REASON_CODES) == 3
    assert not (MODEL_REASON_CODES & RUNTIME_REASON_CODES)


def test_manufactured_no_access_via_a_scratch_schema_fetch_is_permitted_today() -> None:
    """KNOWN-OPEN, asserted rather than hidden (04 §B.4).

    `getTableSchema(<scratch_db>, <anything>)` fails closed with
    `SCRATCH_SESSION_VIOLATION` on any foreign or session-less name. It is a
    METADATA call: it touches no data, needs no knowledge of the user's scope, and
    does not lock the late-init boundary. So `NO_ACCESS` costs exactly one call.

    This test exists so the hole is visible in CI and this row FAILS the day
    someone believes they closed it without saying so."""
    trail = [
        _entry(
            "call_scratch",
            "getTableSchema",
            status="denied",
            error_code="SCRATCH_SESSION_VIOLATION",
            row_count=None,
            args={"database": "scratch_other", "table": "x"},
        )
    ]
    assert validate_block_evidence("call_scratch", "NO_ACCESS", trail, TURN) is None


def test_manufactured_required_data_unavailable_via_where_1_equals_0_is_permitted_today() -> None:
    """KNOWN-OPEN, the second route: `SELECT ... WHERE 1=0` yields `row_count == 0`
    and therefore a valid block. Evidence-backed blocking stops the model
    ASSERTING an unfalsifiable reason; it does not stop it PRODUCING a falsifiable
    one."""
    trail = [
        _entry(
            "call_empty",
            "runQuery",
            row_count=0,
            args={"sql": "SELECT 1 WHERE 1=0"},
        )
    ]
    assert validate_block_evidence("call_empty", "REQUIRED_DATA_UNAVAILABLE", trail, TURN) is None
