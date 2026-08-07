"""Unit tests for RuntimeSettings (Layer 1 — no infra)."""

from __future__ import annotations

from data_agent.runtime.config import RuntimeSettings


def test_locked_defaults() -> None:
    """The orchestrator-locked tunables must match exactly (no drift)."""
    settings = RuntimeSettings(_env_file=None)
    assert settings.session_ttl_seconds == 604_800
    assert settings.preview_row_count == 20
    assert settings.history_token_budget_ratio == 0.20
    assert settings.max_loop_iterations == 15
    assert settings.max_wall_clock_seconds == 60
    assert settings.max_budget_windows == 3
    assert settings.max_tool_calls_per_iteration == 8  # S3 hardening default
    assert settings.discovery_emulation_enabled is True


def test_history_token_budget_derivation() -> None:
    settings = RuntimeSettings(_env_file=None, model_context_window=100_000)
    assert settings.history_token_budget() == 20_000


def test_request_token_budget_reserve_and_headroom() -> None:
    """The total-request fit budget = (window - reserve) * 0.8 headroom (the 0.8
    covers the chars/4 estimator under-counting JSON/SQL-dense content)."""
    settings = RuntimeSettings(
        _env_file=None, model_context_window=128_000, response_token_reserve=16_000
    )
    # (128000 - 16000) * 0.8 = 89_600
    assert settings.request_token_budget() == 89_600
    # Defaults are the same values.
    assert settings.response_token_reserve == 16_000
    assert settings.max_tool_result_tokens == 4_000


def test_request_token_budget_clamped_to_at_least_one() -> None:
    # A misconfiguration where the reserve meets/exceeds the window can never
    # yield a non-positive budget the fit walk would read as "drop everything".
    settings = RuntimeSettings(
        _env_file=None, model_context_window=1_000, response_token_reserve=5_000
    )
    assert settings.request_token_budget() == 1


def test_env_var_override(monkeypatch) -> None:
    monkeypatch.setenv("SESSION_TTL_SECONDS", "3600")
    monkeypatch.setenv("MCP_URL", "http://mcp.internal:9000/mcp")
    settings = RuntimeSettings(_env_file=None)
    assert settings.session_ttl_seconds == 3600
    assert settings.mcp_url == "http://mcp.internal:9000/mcp"
