"""Unit tests for RuntimeSettings (Layer 1 — no infra)."""

from __future__ import annotations

from data_agent.runtime.config import RuntimeSettings, effective_llm_hide


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


def test_telemetry_defaults_toward_reveal() -> None:
    """The 2026-08-10 posture, pinned where a reader looks for the default.

    `otlp_disable_redaction` reads as a double negative and its default now makes the
    config read backwards, so the plain-English claim is asserted rather than left to be
    derived: NOTHING IS REDACTED AT THE DEFAULT, and `effective_llm_hide` is therefore
    False whatever `otlp_hide_llm_content` says. The consequence is that the OTLP
    collector holds real entity values and must be access-controlled like the session
    store — see the field description."""
    settings = RuntimeSettings(_env_file=None)
    assert settings.otlp_disable_redaction is True
    assert effective_llm_hide(settings) is False
    # Hiding the LLM content now takes BOTH settings; the single flag no longer does it.
    assert effective_llm_hide(RuntimeSettings(_env_file=None, otlp_hide_llm_content=True)) is False
    assert (
        effective_llm_hide(
            RuntimeSettings(
                _env_file=None, otlp_hide_llm_content=True, otlp_disable_redaction=False
            )
        )
        is True
    )


def test_the_redaction_opt_out_is_reachable_by_env_var(monkeypatch) -> None:
    """`extra="ignore"` means a typo'd env var is silently dropped and the (now
    revealing) default silently stands — so the opt-out has to be proven reachable by the
    exact name an operator would type, not assumed from the field name."""
    monkeypatch.setenv("OTLP_DISABLE_REDACTION", "false")
    assert RuntimeSettings(_env_file=None).otlp_disable_redaction is False
