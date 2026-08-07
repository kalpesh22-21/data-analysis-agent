"""Layer-1: the `RunBlueprintTool` runtime tool (runblueprint-design §5, Slice B).

Proofs:
  - `ExecCompleted` → ok `ToolResult` (verified result + preview + provenance);
  - `ExecPaused`    → a `ToolResult` carrying a `ToolPause` (the §2.5 loop seam);
  - `ExecFailed`    → error `ToolResult` (raw-loop fallback), tool_name relabeled;
  - a RAISING executor is contained (B4): `RUN_BLUEPRINT_INTERNAL_ERROR`, no
    `str(exc)` leak, the call returns cleanly (turn survives);
  - invalid args fail-closed BEFORE any execution;
  - `slot_bindings` VALUES are redacted from the telemetry args (D25/§5.5).
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import (
    NOT_FOUND_CODE,
    ExecCompleted,
    ExecFailed,
    ExecPaused,
)
from data_agent.runtime.blueprint.tool import (
    INTERNAL_ERROR_CODE,
    INVALID_ARGS_CODE,
    RunBlueprintTool,
)
from data_agent.runtime.observability.redaction import redact_tool_args
from data_agent.runtime.session.models import ResultPreview


def _creds() -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s", jwt="secret-jwt", column_scope=frozenset())


class _StubExecutor:
    """Returns a pre-set `ExecOutcome` (or raises) — isolates the tool's mapping."""

    def __init__(self, outcome: Any = None, *, raises: bool = False) -> None:
        self._outcome = outcome
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    async def execute(
        self, *, blueprint_id: str, slot_bindings: dict[str, Any], credentials: RuntimeCredentials
    ) -> Any:
        self.calls.append({"id": blueprint_id, "slot_bindings": slot_bindings})
        if self._raises:
            raise RuntimeError("secret internal boom")
        return self._outcome


def _tool(outcome: Any = None, *, raises: bool = False) -> tuple[RunBlueprintTool, _StubExecutor]:
    stub = _StubExecutor(outcome, raises=raises)
    return RunBlueprintTool(executor=stub), stub  # type: ignore[arg-type]


async def test_completed_maps_to_ok_result() -> None:
    completed = ExecCompleted(
        result_full={"blueprint_id": "bp", "status": "verified", "columns": ["d"]},
        preview=ResultPreview(columns=["d"], row_count=1, truncated=False, preview_rows=[["x"]]),
        provenance=frozenset({("dbpcm_warehouse.employee", "Department")}),
    )
    tool, _ = _tool(completed)
    result = await tool.run({"id": "bp", "slot_bindings": {"department": "Sales"}}, _creds())
    assert result.status == "ok"
    assert result.tool_name == "runBlueprint"
    assert result.result_full["status"] == "verified"
    assert result.pause is None
    assert result.provenance == frozenset({("dbpcm_warehouse.employee", "Department")})


async def test_verified_completed_carries_authoritative_marker() -> None:
    # A genuinely D56-verified result (status verified + clean verify block) earns
    # the in-band `authoritative` flag so the model treats it as the trusted answer.
    completed = ExecCompleted(
        result_full={
            "blueprint_id": "bp",
            "status": "verified",
            "columns": ["d"],
            "verify": {"grain_ok": True, "grain_checked": True, "signature_ok": True},
        },
        preview=ResultPreview(columns=["d"], row_count=1, truncated=False, preview_rows=[["x"]]),
        provenance=frozenset(),
    )
    tool, _ = _tool(completed)
    result = await tool.run({"id": "bp", "slot_bindings": {}}, _creds())
    assert result.status == "ok"
    assert result.authoritative is True


async def test_unverified_completed_does_not_carry_authoritative_marker() -> None:
    # A poisoned/legacy ExecCompleted whose verify block reports a grain mismatch
    # (or is missing) must NOT be flagged authoritative — the marker is over-claim-proof.
    grain_mismatch = ExecCompleted(
        result_full={
            "blueprint_id": "bp",
            "status": "verified",
            "columns": ["d"],
            "verify": {"grain_ok": False, "grain_checked": True, "signature_ok": True},
        },
        preview=ResultPreview(columns=["d"], row_count=1, truncated=False, preview_rows=[["x"]]),
        provenance=frozenset(),
    )
    tool, _ = _tool(grain_mismatch)
    result = await tool.run({"id": "bp", "slot_bindings": {}}, _creds())
    assert result.status == "ok"
    assert result.authoritative is False

    no_verify_block = ExecCompleted(
        result_full={"blueprint_id": "bp", "status": "verified", "columns": ["d"]},
        preview=ResultPreview(columns=["d"], row_count=1, truncated=False, preview_rows=[["x"]]),
        provenance=frozenset(),
    )
    tool2, _ = _tool(no_verify_block)
    result2 = await tool2.run({"id": "bp", "slot_bindings": {}}, _creds())
    assert result2.authoritative is False


async def test_failed_and_paused_are_never_authoritative() -> None:
    failed = ExecFailed(NOT_FOUND_CODE, "not available", retryable=True)
    tool, _ = _tool(failed)
    assert (await tool.run({"id": "bp", "slot_bindings": {}}, _creds())).authoritative is False

    paused = ExecPaused(
        reason="blueprint_slot",
        pending_question={"question": "Which department?", "options": ["Sales"]},
        blueprint_id="bp",
        slot_bindings_json='{"department": "?"}',
    )
    tool2, _ = _tool(paused)
    assert (await tool2.run({"id": "bp", "slot_bindings": {}}, _creds())).authoritative is False


async def test_paused_maps_to_toolpause_seam() -> None:
    paused = ExecPaused(
        reason="blueprint_slot",
        pending_question={"question": "Which department?", "options": ["Sales"]},
        blueprint_id="bp",
        slot_bindings_json='{"department": "?"}',
    )
    tool, _ = _tool(paused)
    result = await tool.run({"id": "bp", "slot_bindings": {}}, _creds())
    assert result.status == "ok"
    assert result.pause is not None
    assert result.pause.reason == "blueprint_slot"
    assert result.pause.blueprint_id == "bp"
    assert result.pause.pending_question["question"] == "Which department?"


async def test_failed_maps_to_error_result() -> None:
    failed = ExecFailed(NOT_FOUND_CODE, "not available", retryable=True, provenance=None)
    tool, _ = _tool(failed)
    result = await tool.run({"id": "bp", "slot_bindings": {}}, _creds())
    assert result.status == "error"
    assert result.tool_name == "runBlueprint"
    assert result.error_code == NOT_FOUND_CODE
    assert result.retryable is True


async def test_raising_executor_is_contained_no_str_exc_leak() -> None:
    tool, _ = _tool(raises=True)
    result = await tool.run({"id": "bp", "slot_bindings": {}}, _creds())
    assert result.status == "error"
    assert result.error_code == INTERNAL_ERROR_CODE
    # The raw exception text never reaches the model/user.
    assert result.user_message is not None
    assert "boom" not in result.user_message


async def test_invalid_args_fail_closed_before_execution() -> None:
    tool, stub = _tool(None)
    r1 = await tool.run({"slot_bindings": {}}, _creds())  # missing id
    r2 = await tool.run({"id": "  ", "slot_bindings": {}}, _creds())  # blank id
    r3 = await tool.run({"id": "bp", "slot_bindings": ["not", "a", "dict"]}, _creds())
    for r in (r1, r2, r3):
        assert r.status == "error"
        assert r.error_code == INVALID_ARGS_CODE
    assert stub.calls == []  # the executor was never reached


async def test_missing_slot_bindings_defaults_to_empty() -> None:
    tool, stub = _tool(ExecFailed(NOT_FOUND_CODE, "x", retryable=True))
    await tool.run({"id": "bp"}, _creds())  # no slot_bindings key
    assert stub.calls == [{"id": "bp", "slot_bindings": {}}]


# ---------------------------------------------------------------------------
# Redaction (§5.5 / D25): slot_bindings VALUES never reach telemetry
# ---------------------------------------------------------------------------


def test_redact_tool_args_masks_slot_binding_values() -> None:
    args = {"id": "bp-x", "slot_bindings": {"department": "Warehouse", "period": "May 2026"}}
    redacted = redact_tool_args("runBlueprint", args)
    # The slot NAMES are kept (structural); every VALUE is a placeholder.
    assert set(redacted["slot_bindings"].keys()) == {"department", "period"}
    assert set(redacted["slot_bindings"].values()) == {"<redacted>"}
    # The blueprint id (structural, non-PII) is kept for debuggability.
    assert redacted["id"] == "bp-x"
    # No raw value substring survives anywhere in the telemetry args.
    import json

    blob = json.dumps(redacted)
    assert "Warehouse" not in blob
    assert "May 2026" not in blob
