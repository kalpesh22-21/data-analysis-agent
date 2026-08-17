"""The promotion policy is WIRED FROM CONFIG, not from dataclass defaults (plan §4).

The defect: `PromotionPolicy` was accepted by all three promotion factories and passed by
NO entrypoint, so every knob on it was a hardcoded dataclass default. An operator setting
an env var changed nothing, and nothing failed — the knob simply did not exist.

Slugs:
  * S9-policy-built-from-settings — every field maps to a `LearningSettings` field.
  * S9-factory-defaults-the-policy — omitting `policy=` at a composition root yields the
    CONFIGURED policy, not `PromotionPolicy()`.
  * S9-explicit-policy-still-wins — an injected policy is never overridden.
  * S9-inbox-shares-the-scheduler-policy — the routing threshold and the review cutoff
    cannot be configured independently.
"""

from __future__ import annotations

from dataclasses import fields

from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.config import LearningSettings
from data_agent.learning.factory import (
    build_promotion_plane,
)
from data_agent.learning.promotion import PromotionPolicy, policy_from_settings

from .helpers import FakeHitCountReader, FakeWarehouseProbe, promotion_policy


def _settings(monkeypatch=None, **env: str) -> LearningSettings:
    """`LearningSettings` built from ENV VARS, not from constructor kwargs.

    Constructor kwargs would bypass the very mechanism under test — the whole defect was
    that a setting existed and never reached the policy, so the test has to travel the
    operator's actual path. `_env_file=None` keeps it hermetic: a developer's `.env` must
    not decide what the shipped defaults are asserted to be."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return LearningSettings(_env_file=None)


def test_every_policy_field_is_reachable_from_an_env_var(monkeypatch):
    """THE anti-regression for the defect itself: a knob added to `PromotionPolicy` and
    not wired to a setting is a knob that does not exist in production. This asserts the
    mapping is TOTAL by driving every field to a non-default value through settings and
    checking each one arrives — a new unwired field fails here rather than shipping as
    silent decoration."""
    tuned = _settings(
        monkeypatch,
        LEARNING_PROMOTION_ROUTING_THRESHOLD="7",
        LEARNING_PROMOTION_RECURRENCE_WEIGHT="0.25",
        LEARNING_PROMOTION_SCAN_LIMIT="11",
        LEARNING_PROMOTION_INTERVAL_SECONDS="13.5",
        LEARNING_DRIFT_FRESHNESS_SECONDS="1000",
        LEARNING_REPLAY_RECHECK_INTERVAL_SECONDS="500",
        LEARNING_REVIEW_SCORE_CUTOFF="0.4",
    )
    policy = policy_from_settings(tuned)

    assert policy == PromotionPolicy(
        blueprint_hit_threshold=7,
        recurrence_weight=0.25,
        drift_freshness_seconds=1000.0,
        replay_recheck_interval_seconds=500.0,
        scan_limit=11,
        promotion_interval_seconds=13.5,
        review_score_cutoff=0.4,
    )
    # ...and every field genuinely moved off its dataclass default, so the equality above
    # cannot pass by accident for a field the mapping forgot.
    default = PromotionPolicy()
    for f in fields(PromotionPolicy):
        assert getattr(policy, f.name) != getattr(default, f.name), (
            f"{f.name} did not change: it is either not wired to a setting, or the "
            "value chosen here happens to equal the default"
        )


def test_the_shipped_defaults_are_the_plan_4_posture():
    """The shipped numbers, asserted rather than assumed. The threshold is the one that
    matters: at 3 no candidate ever reached a human, and 1 is only safe because the auto
    path stops at `in_review`."""
    policy = policy_from_settings(_settings())
    assert policy.blueprint_hit_threshold == 1
    assert policy.recurrence_weight == 0.0  # the soft counter is DORMANT
    assert policy.review_score_cutoff == 0.0  # no cutoff — a human skims the whole queue


def test_the_recheck_interval_default_sits_inside_the_trust_window():
    """The invariant `PromotionPolicy` documents, checked against the SHIPPED values (the
    scheduler also enforces it at the point of use, so this is about the config being
    coherent rather than about safety)."""
    policy = policy_from_settings(_settings())
    assert policy.replay_recheck_interval_seconds <= policy.drift_freshness_seconds


def test_a_factory_with_no_policy_uses_the_configured_one_not_the_dataclass_default(
    monkeypatch,
):
    """The defect, restated as the fix. `build_promotion_plane(settings, ...)` with no
    `policy=` must resolve settings — before plan §4 it fell through to
    `PromotionScheduler`'s own `PromotionPolicy()`, which is exactly how an env var came
    to have no effect."""
    scheduler, _inbox = build_promotion_plane(
        _settings(monkeypatch, LEARNING_PROMOTION_ROUTING_THRESHOLD="9"),
        candidate_store=InMemoryCandidateStore(),
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader(),
    )
    assert scheduler.policy.blueprint_hit_threshold == 9


def test_an_explicitly_injected_policy_still_wins(monkeypatch):
    """Tests and demos pass a policy directly; settings must not override it."""
    pinned = promotion_policy(blueprint_hit_threshold=42)
    scheduler, _inbox = build_promotion_plane(
        _settings(monkeypatch, LEARNING_PROMOTION_ROUTING_THRESHOLD="9"),
        candidate_store=InMemoryCandidateStore(),
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader(),
        policy=pinned,
    )
    assert scheduler.policy is pinned


def test_the_inbox_shares_the_schedulers_policy(monkeypatch):
    """The routing threshold and the review-score cutoff are two ends of ONE decision
    about how much a reviewer is asked to look at. If the plane built the inbox from a
    fresh `PromotionPolicy()`, a deployment that configured a cutoff would route work into
    a queue whose own filter silently ignored the setting."""
    settings = _settings(monkeypatch, LEARNING_REVIEW_SCORE_CUTOFF="0.3")
    store = InMemoryCandidateStore()
    scheduler, inbox = build_promotion_plane(
        settings,
        candidate_store=store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader(),
    )
    assert scheduler.policy.review_score_cutoff == 0.3
    assert inbox._policy is scheduler.policy  # noqa: SLF001 - the shared-object invariant


def test_a_directly_constructed_inbox_inherits_its_schedulers_policy(monkeypatch):
    """The SAME defect one level down, and it is the level a caller actually hits.

    `build_promotion_plane` passes the policy explicitly, but `ReviewInbox(store,
    scheduler=...)` is a supported construction — both demo scripts and every test in this
    suite use it. With a `policy or PromotionPolicy()` default, a caller who built a
    correctly-configured scheduler and handed it over silently got cutoff 0.0: a knob
    turned in the environment and ignored at the surface it governs, which is exactly what
    this slice was written to remove. Correctness must not depend on the caller
    remembering to pass it twice."""
    from data_agent.learning.inbox import ReviewInbox

    store = InMemoryCandidateStore()
    scheduler, _plane_inbox = build_promotion_plane(
        _settings(monkeypatch, LEARNING_REVIEW_SCORE_CUTOFF="0.35"),
        candidate_store=store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader(),
    )

    inbox = ReviewInbox(store, scheduler=scheduler)

    assert inbox._policy is scheduler.policy  # noqa: SLF001 - the shared-object invariant
    assert inbox._policy.review_score_cutoff == 0.35  # noqa: SLF001


def test_an_unwired_inbox_still_gets_a_default_policy():
    """The fix must not require a scheduler: an inbox built with neither argument falls
    through to its own default scheduler's default policy (cutoff 0.0 — show everything),
    which is byte-identical to the pre-fix behaviour for that case."""
    from data_agent.learning.inbox import ReviewInbox

    inbox = ReviewInbox(InMemoryCandidateStore())
    assert inbox._policy.review_score_cutoff == 0.0  # noqa: SLF001


def test_the_judge_knobs_are_not_duplicated_onto_the_promotion_policy():
    """Plan §3b put the coverage-judge thresholds on `LearningSettings` because a knob
    that can silently CANCEL an extraction had to be live from its first deploy, and
    `PromotionPolicy` was not. Plan §4 was told to move them or leave them, never both:
    two sources of truth for one threshold means a reader tunes the copy nothing reads.

    Pinned as an absence, because that is the only way a duplicate gets noticed."""
    policy_field_names = {f.name for f in fields(PromotionPolicy)}
    assert not any("judge" in name or "prior_art" in name for name in policy_field_names)
    # ...and they are still reachable where §3b put them.
    settings = _settings()
    assert settings.learning_judge_pre_drop_confidence == 0.90
    assert settings.learning_judge_band_low == 0.70
