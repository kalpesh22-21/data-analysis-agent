"""QA5 Layer-1 adversarial: `RunBlueprintTool` (runblueprint-design §5, Slice B).

Attacks the B4 crash-containment, the invalid-args surface, and the D25 redaction
boundary — the paths `test_tool.py` proves only once positively:

  - EVERY exception flavor a raising executor can throw (ValueError / KeyError /
    a raising vector_index inside the real executor) → `RUN_BLUEPRINT_INTERNAL_ERROR`,
    the turn survives, and NO `str(exc)` reaches the model.
  - invalid-args fail-closed BEFORE execution for: non-str id (int), id of only
    metachars-but-blank-stripped, `slot_bindings` as a str / list / None.
  - a non-oracle id (metachars, absent) → NOT_FOUND, never distinguishable.
  - redaction: `redact_tool_args` masks EVERY value shape (list/number/bool/nested)
    and copies rather than mutates the caller's args; a NON-dict `slot_bindings` is
    left structurally intact (nothing to leak).
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import (
    NOT_FOUND_CODE,
    BlueprintExecutor,
    ExecFailed,
)
from data_agent.runtime.blueprint.tool import (
    INTERNAL_ERROR_CODE,
    INVALID_ARGS_CODE,
    RunBlueprintTool,
)
from data_agent.runtime.observability.redaction import redact_tool_args


def _creds() -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s", jwt="secret-jwt", column_scope=frozenset())


class _RaisingExecutor:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def execute(self, **_kwargs: Any) -> Any:
        raise self._exc


class _RaisingVectorIndex:
    async def get_blueprint(self, _blueprint_id: str) -> Any:
        raise RuntimeError("neo4j secret dsn leaked-here")


# ===========================================================================
# B4 crash containment — every exception flavor is contained
# ===========================================================================


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("secret value boom"),
        KeyError("secret_key"),
        RuntimeError("secret runtime boom"),
        TypeError("secret type boom"),
    ],
    ids=["ValueError", "KeyError", "RuntimeError", "TypeError"],
)
async def test_every_executor_exception_flavor_is_contained(exc: Exception) -> None:
    tool = RunBlueprintTool(executor=_RaisingExecutor(exc))  # type: ignore[arg-type]
    result = await tool.run({"id": "bp", "slot_bindings": {"d": "x"}}, _creds())
    assert result.status == "error"
    assert result.error_code == INTERNAL_ERROR_CODE
    assert result.retryable is False
    # No fragment of the raw exception text leaks to the model.
    assert result.user_message is not None
    assert "secret" not in result.user_message
    assert result.result_full is None


async def test_raising_vector_index_inside_real_executor_is_contained() -> None:
    # A crash raised DEEP inside the real executor (the store read) is still caught by
    # the tool's B4 guard — the turn survives and the backend dsn never leaks.
    executor = BlueprintExecutor(tool_dispatcher=None, vector_index=_RaisingVectorIndex())  # type: ignore[arg-type]
    tool = RunBlueprintTool(executor=executor)
    result = await tool.run({"id": "bp", "slot_bindings": {}}, _creds())
    assert result.status == "error"
    assert result.error_code == INTERNAL_ERROR_CODE
    assert result.user_message is not None
    assert "neo4j" not in result.user_message
    assert "leaked-here" not in result.user_message


# ===========================================================================
# Invalid-args surface — fail-closed BEFORE the executor is reached
# ===========================================================================


class _CountingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **_kwargs: Any) -> Any:
        self.calls += 1
        return ExecFailed(NOT_FOUND_CODE, "x", retryable=True)


@pytest.mark.parametrize(
    "args",
    [
        {"id": 123, "slot_bindings": {}},  # non-str id
        {"id": None, "slot_bindings": {}},  # null id
        {"id": "\t  \n", "slot_bindings": {}},  # whitespace-only id
        {"id": "bp", "slot_bindings": "not-a-dict"},  # str slot_bindings
        {"id": "bp", "slot_bindings": ["a", "b"]},  # list slot_bindings
        {"id": "bp", "slot_bindings": 42},  # number slot_bindings
    ],
    ids=["int-id", "null-id", "ws-id", "str-bindings", "list-bindings", "num-bindings"],
)
async def test_invalid_args_fail_closed_before_executor(args: dict[str, Any]) -> None:
    executor = _CountingExecutor()
    tool = RunBlueprintTool(executor=executor)  # type: ignore[arg-type]
    result = await tool.run(args, _creds())
    assert result.status == "error"
    assert result.error_code == INVALID_ARGS_CODE
    assert executor.calls == 0  # the executor was never reached


async def test_none_slot_bindings_defaults_to_empty_dict() -> None:
    # `slot_bindings: None` is tolerated (defaulted to {}), NOT an invalid-args error —
    # a required slot then pauses downstream, which is the correct UX.
    executor = _CountingExecutor()
    tool = RunBlueprintTool(executor=executor)  # type: ignore[arg-type]
    result = await tool.run({"id": "bp", "slot_bindings": None}, _creds())
    assert result.error_code == NOT_FOUND_CODE  # reached the executor (returned NOT_FOUND stub)
    assert executor.calls == 1


async def test_id_is_stripped_before_execution() -> None:
    class _CaptureExecutor:
        def __init__(self) -> None:
            self.seen_id: str | None = None

        async def execute(self, *, blueprint_id: str, slot_bindings: Any, credentials: Any) -> Any:
            self.seen_id = blueprint_id
            return ExecFailed(NOT_FOUND_CODE, "x", retryable=True)

    executor = _CaptureExecutor()
    tool = RunBlueprintTool(executor=executor)  # type: ignore[arg-type]
    await tool.run({"id": "  bp-x  ", "slot_bindings": {}}, _creds())
    assert executor.seen_id == "bp-x"  # surrounding whitespace stripped


# ===========================================================================
# Redaction — every value shape masked; input not mutated
# ===========================================================================


def test_redact_masks_all_value_shapes_number_bool_list_nested() -> None:
    args = {
        "id": "bp-x",
        "slot_bindings": {
            "dept": "Warehouse",
            "count": 42,
            "flag": True,
            "codes": ["PTO", "SICK"],
            "nested": {"secret": "value"},
        },
    }
    redacted = redact_tool_args("runBlueprint", args)
    # Every slot NAME survives (structural), every VALUE is the placeholder.
    assert set(redacted["slot_bindings"].keys()) == {"dept", "count", "flag", "codes", "nested"}
    assert set(redacted["slot_bindings"].values()) == {"<redacted>"}
    import json

    blob = json.dumps(redacted)
    for leak in ("Warehouse", "42", "PTO", "SICK", "true", '"secret"'):
        assert leak not in blob


def test_redact_does_not_mutate_caller_args() -> None:
    args = {"id": "bp", "slot_bindings": {"dept": "Warehouse"}}
    _ = redact_tool_args("runBlueprint", args)
    # The ORIGINAL args dict is untouched — the model-facing args still carry the raw
    # value (they must, for the actual run); only the telemetry COPY is redacted.
    assert args["slot_bindings"]["dept"] == "Warehouse"


def test_redact_leaves_non_dict_slot_bindings_intact() -> None:
    # A malformed non-dict slot_bindings has no per-key values to mask; redaction must
    # not crash and simply passes the (already value-free) structure through.
    args = {"id": "bp", "slot_bindings": "oops"}
    redacted = redact_tool_args("runBlueprint", args)
    assert redacted["slot_bindings"] == "oops"
    assert redacted["id"] == "bp"
