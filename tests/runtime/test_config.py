"""Unit tests for RuntimeSettings (Layer 1 — no infra)."""

from __future__ import annotations

import pytest

from data_agent.runtime.config import RuntimeSettings, effective_llm_hide
from data_agent.runtime.prompts import AGENT_SYSTEM_PROMPT


def test_locked_defaults() -> None:
    """The orchestrator-locked tunables must match exactly (no drift).

    The two loop budgets were RAISED on live measurement (2026-08-12): every
    observed failure was the wall clock, with turns capping at ~61-73s while
    iterations sat at ~8 of 15. The iteration ceiling moved with it so it does not
    become the next binding constraint at 180s. The other two are unchanged and
    that is a decision, not an omission — the largest tool-call batch observed was
    3 of a possible 8, and `max_budget_windows` bounds `continue` GRANTS, each of
    which is now worth three times what it was.

    `max_window_token_spend` joined them the same day: the loop's token ceiling had
    no field and borrowed `model_context_window`, comparing a per-window SPEND sum
    against a single-request OCCUPANCY limit. Pinned here because it is what makes
    the 25 above reachable — see the field comment for the measured derivation.
    """
    settings = RuntimeSettings(_env_file=None)
    assert settings.session_ttl_seconds == 604_800
    assert settings.preview_row_count == 20
    assert settings.max_loop_iterations == 25
    assert settings.max_wall_clock_seconds == 180
    assert settings.max_budget_windows == 3
    assert settings.max_window_token_spend == 1_000_000
    assert settings.max_tool_calls_per_iteration == 8  # S3 hardening default
    assert settings.discovery_emulation_enabled is True


def test_window_token_spend_ceiling_is_independent_of_the_context_window() -> None:
    """The defect this field fixed: spend was measured against the context window.

    They are different units and must move independently — shrinking the model's
    context window (an occupancy fact) must not shrink how much a window may spend,
    and vice versa.
    """
    settings = RuntimeSettings(_env_file=None, model_context_window=32_000)
    assert settings.max_window_token_spend == 1_000_000
    assert settings.request_token_budget() == int((32_000 - 16_000) * 0.8)
    # And the spend ceiling is env-overridable on its own.
    assert (
        RuntimeSettings(_env_file=None, max_window_token_spend=250_000).max_window_token_spend
        == 250_000
    )


def test_base_agent_prompt_is_wired_as_the_default() -> None:
    """The prompt reaches the model only through this default.

    `agent_system_prompt_enabled` gates it and `agent_system_prompt` carries the
    text, so a rewrite that lands in `prompts.py` but not in the settings default
    ships nothing. Asserted rather than assumed because it is the whole delivery
    path for Release 1's P1 fix, and because the constant must stay non-empty: an
    empty string is falsy and would assemble a prompt-less loop that still looks
    enabled.
    """
    settings = RuntimeSettings(_env_file=None)
    assert settings.agent_system_prompt_enabled is True
    assert settings.agent_system_prompt == AGENT_SYSTEM_PROMPT
    assert AGENT_SYSTEM_PROMPT.strip() != ""


def test_history_token_budget_knob_is_gone_and_its_env_var_is_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L1 — `history_token_budget_ratio` + `history_token_budget()` were DELETED.

    Their only reader (`ContextAssembler(history_token_budget=...)`) went in Tier 2;
    replayed history is bounded at the send seam by `request_token_budget()` now. The
    pins for both were removed with them — this is the counter-pin, so a re-added knob
    has to justify itself against a real reader.

    The env var half is the MIGRATION posture, executable: `RuntimeSettings` is
    `extra="ignore"`, so a deployment still exporting `HISTORY_TOKEN_BUDGET_RATIO`
    boots unchanged and the value is silently dropped (inert by absence). Worth pinning
    because the same `extra="ignore"` is what makes a MISSPELLED knob silently inert
    too — here that behaviour is the feature, and nobody should discover it by finding
    a stale env var still "set" in a running pod.
    """
    monkeypatch.setenv("HISTORY_TOKEN_BUDGET_RATIO", "0.9")
    settings = RuntimeSettings(_env_file=None)
    assert not hasattr(settings, "history_token_budget_ratio")
    assert not hasattr(settings, "history_token_budget")
    assert "history_token_budget_ratio" not in settings.model_dump()


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
    # C5b: the COLUMNS SECTION of a getTableSchema has its own, larger budget —
    # the generic per-result cap above governs every OTHER tool result. Pinned
    # here because the two are easy to confuse and a merge that collapsed them
    # would silently re-truncate `rules`/`ambiguities`.
    assert settings.schema_columns_token_budget == 6_000


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
