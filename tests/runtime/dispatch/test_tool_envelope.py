"""Layer-1: the two fail-closed properties of `RuntimeToolBase` itself.

Proofs:
  - a `_on_guarded_exception` that RAISES lands in THIS tool's internal-error arm
    (Python never consults a sibling `except` for an exception raised inside another,
    so without the explicit re-entry the raise escapes to the loop's outer guard and
    the model gets the wrong error code);
  - a concrete subclass that leaves `tool_name`/`_INTERNAL_ERROR_*` empty fails at
    class-definition (import) time rather than shipping a nameless error shape.
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.dispatch.tool_dispatcher import ToolResult
from data_agent.runtime.dispatch.tool_envelope import RuntimeToolBase


class _SentinelError(Exception):
    pass


def _creds() -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s", jwt="secret-jwt", column_scope=frozenset())


class _RaisingHookTool(RuntimeToolBase):
    tool_name = "sentinelTool"
    _INTERNAL_ERROR_CODE = "SENTINEL_TOOL_INTERNAL_ERROR"
    _INTERNAL_ERROR_MESSAGE = "Sentinel tool hit an internal error."
    _GUARDED_EXCEPTIONS = (_SentinelError,)

    def _span_args(self, model_args: dict[str, Any]) -> dict[str, Any]:
        return {}

    def _on_guarded_exception(self, exc: Exception, model_args: dict[str, Any]) -> ToolResult:
        raise RuntimeError("secret hook boom")

    async def _execute(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials, turn: Any
    ) -> ToolResult:
        raise _SentinelError("secret domain boom")


async def test_raising_guarded_hook_lands_in_this_tools_internal_error_arm() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    tool = _RaisingHookTool(observer=lambda name, payload: events.append((name, payload)))

    result = await tool.run({}, _creds())

    assert result.status == "error"
    assert result.error_code == "SENTINEL_TOOL_INTERNAL_ERROR"
    assert result.retryable is False
    assert "boom" not in (result.user_message or "")
    assert [name for name, _ in events] == ["tool_dispatch_start", "tool_dispatch_error"]


def test_concrete_subclass_without_identity_fails_at_definition_time() -> None:
    with pytest.raises(TypeError, match="tool_name"):

        class _Nameless(RuntimeToolBase):
            _INTERNAL_ERROR_CODE = "X"
            _INTERNAL_ERROR_MESSAGE = "y"

            def _span_args(self, model_args: dict[str, Any]) -> dict[str, Any]:
                return {}

            async def _execute(
                self, model_args: dict[str, Any], credentials: RuntimeCredentials, turn: Any
            ) -> ToolResult:
                raise AssertionError("never runs")
