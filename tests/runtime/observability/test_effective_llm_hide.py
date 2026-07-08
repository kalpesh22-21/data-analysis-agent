"""`config.effective_llm_hide` — the pure resolver for the OpenAI-LLM-content
hide, folding the D25 `otlp_hide_llm_content` default with the master telemetry
debug switch `otlp_disable_redaction`.

Reviewer Suggestion 1: the LLM-content REVEAL direction (disable_redaction forcing
content visible) was previously exercised only implicitly via app wiring. These
tests pin the full 4-row truth table, and confirm the derived value drives whether
`configure_tracing` installs the `LLMExceptionEventScrubber`.

Tag: otlp-disable-redaction-reveals-llm-content
"""

from __future__ import annotations

import pytest

from data_agent.runtime.config import RuntimeSettings, effective_llm_hide
from data_agent.runtime.observability import tracing


@pytest.mark.parametrize(
    ("hide_llm_content", "disable_redaction", "expected_hidden"),
    [
        (True, False, True),  # D25 default: content hidden
        (True, True, False),  # debug switch forces the reveal
        (False, False, False),  # explicit LLM-content reveal
        (False, True, False),  # debug switch: revealed regardless
    ],
)
def test_effective_llm_hide_truth_table(
    hide_llm_content: bool, disable_redaction: bool, expected_hidden: bool
) -> None:
    settings = RuntimeSettings(
        _env_file=None,
        otlp_hide_llm_content=hide_llm_content,
        otlp_disable_redaction=disable_redaction,
    )
    assert effective_llm_hide(settings) is expected_hidden


def test_only_hidden_row_is_the_d25_default() -> None:
    """The ONLY combination that hides content is the D25 default (hide=True,
    disable_redaction=False) — the other three all reveal."""
    hidden = [
        (h, d)
        for h in (True, False)
        for d in (True, False)
        if effective_llm_hide(
            RuntimeSettings(
                _env_file=None, otlp_hide_llm_content=h, otlp_disable_redaction=d
            )
        )
    ]
    assert hidden == [(True, False)]


def _scrubber_count(provider) -> int:  # noqa: ANN001
    processors = provider._active_span_processor._span_processors
    return sum(1 for p in processors if isinstance(p, tracing.LLMExceptionEventScrubber))


def test_configure_tracing_installs_scrubber_only_when_hiding() -> None:
    """The derived `effective_llm_hide` value drives the LLMExceptionEventScrubber:
    installed when hiding, absent when revealing (so a revealed run keeps the
    content-bearing exception event)."""
    hidden = tracing.configure_tracing(
        otlp_endpoint="", service_name="test", hide_llm_content=True
    )
    revealed = tracing.configure_tracing(
        otlp_endpoint="", service_name="test", hide_llm_content=False
    )
    assert _scrubber_count(hidden) == 1
    assert _scrubber_count(revealed) == 0
