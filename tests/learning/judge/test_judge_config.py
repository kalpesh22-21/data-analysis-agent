"""`JudgeConfig` — validate what BREAKS a gate, tolerate (loudly) what is a posture.

The same split `ExtractorConfig` makes, and the same reasoning `DedupStage` used for its
inverted-threshold check: a configuration that makes a whole stage silently unreachable
fails at CONSTRUCTION (the composition root, at process start), because the alternative
is a deployment that quietly does nothing for months with no log at the point of use to
explain it.

Slugs:
  * J-config-inverted-band     — an empty band disables the post-extraction judge for
                                 every candidate. Raises.
  * J-config-bar-range         — a negative bar drops on EVERY duplicate verdict
                                 regardless of confidence. Raises.
  * J-config-bar-ordering      — pre < post is wrong by design but not unreachable, so
                                 it WARNS rather than raising: both bars stay meaningful
                                 and an operator running a measured experiment should be
                                 able to express it.
  * J-config-settings-parity   — the settings surface and the dataclass agree.
"""

from __future__ import annotations

import logging

import pytest

from data_agent.learning.config import LearningSettings
from data_agent.learning.judge import JudgeConfig, JudgeConfigError


def test_the_defaults_hold_the_stated_asymmetry() -> None:
    config = JudgeConfig()
    assert config.pre_drop_confidence > config.post_drop_confidence
    assert config.band_low < config.band_high


def test_an_inverted_band_fails_at_construction() -> None:
    with pytest.raises(JudgeConfigError, match="band_low"):
        JudgeConfig(band_low=0.98, band_high=0.70)


def test_an_equal_band_is_allowed() -> None:
    """Degenerate but coherent: exactly one score is judged. Not unreachable, so not an
    error — the check is `>` for that reason, mirroring `DedupStage`'s."""
    assert JudgeConfig(band_low=0.9, band_high=0.9).band_low == 0.9


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pre_drop_confidence": -0.01},
        {"pre_drop_confidence": 1.01},
        {"post_drop_confidence": -1.0},
        {"post_drop_confidence": 2.0},
    ],
)
def test_a_bar_outside_the_confidence_range_fails_at_construction(kwargs) -> None:
    with pytest.raises(JudgeConfigError, match="within"):
        JudgeConfig(**kwargs)


def test_a_lower_pre_bar_warns_but_builds(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        config = JudgeConfig(pre_drop_confidence=0.60, post_drop_confidence=0.90)
    assert config.pre_drop_confidence == 0.60
    assert "less-informed" in caplog.text


def test_the_settings_surface_carries_every_knob_the_dataclass_has() -> None:
    """The knobs live on `LearningSettings` and NOT on `PromotionPolicy`, deliberately:
    `PromotionPolicy` is accepted by all three promotion factories and passed by no
    entrypoint, so a knob added there today is a knob that does not exist in production
    (slice 4's job). A knob that silently cancels extractions must be live from the
    first deploy — so this pins the settings/dataclass parity rather than leaving the
    default to drift."""
    settings = LearningSettings(
        learning_audit_username="", learning_audit_password=""
    )
    default = JudgeConfig()
    assert settings.learning_judge_pre_drop_confidence == default.pre_drop_confidence
    assert settings.learning_judge_post_drop_confidence == default.post_drop_confidence
    assert settings.learning_judge_band_low == default.band_low
    assert settings.learning_judge_band_high == default.band_high
    assert settings.learning_judge_timeout_seconds == default.timeout_seconds
    assert settings.learning_judge_enabled is True
