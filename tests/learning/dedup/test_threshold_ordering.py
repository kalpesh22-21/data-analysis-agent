"""S6 soft-layer threshold ordering — fail at construction, not at adjudication.

`_soft_layer` tests `sim >= merge_threshold` FIRST and only then `>= conflict_threshold`.
So an operator who sets `conflict > merge` makes the conflict band UNREACHABLE: every
similarity in the intended conflict range is stamped `merge` instead. Both verdicts route
to the review inbox, so nothing lands wrongly — which is exactly what makes the bug nasty.
It is SILENT: no exception, no log, no wrong write, just every near-miss mislabelled for
the life of the deployment.

The thresholds became operator-tunable settings in this slice
(`LEARNING_DEDUP_{MERGE,CONFLICT}_THRESHOLD`), which is what turned a pair of module
constants that could only be wrong by a code edit into a pair of env vars that can be
wrong by a typo. The guard is in `DedupStage.__init__` — i.e. at composition, at process
start — rather than in a pydantic validator, matching this codebase's convention of
enforcing cross-field invariants at the composition root (`config.py` has no validator
precedent).
"""

from __future__ import annotations

import pytest

from data_agent.learning.config import LearningSettings
from data_agent.learning.dedup import DedupStage, InMemoryBlueprintCorpus, ThresholdConfigError


class _Embedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


def _stage(**kwargs: float) -> DedupStage:
    return DedupStage(InMemoryBlueprintCorpus(), _Embedder(), **kwargs)  # type: ignore[arg-type]


def test_an_inverted_pair_is_refused_at_construction() -> None:
    with pytest.raises(ThresholdConfigError) as exc:
        _stage(merge_threshold=0.80, conflict_threshold=0.90)
    # The message must name both numbers and the consequence, since the operator's only
    # other signal would have been silence.
    assert "0.9" in str(exc.value) and "0.8" in str(exc.value)
    assert "unreachable" in str(exc.value)


def test_equal_thresholds_are_allowed() -> None:
    """A degenerate-but-coherent posture: the conflict band is empty, every near-match at
    or above the line is a `merge`. Deliberate, not inverted — allow it."""
    _stage(merge_threshold=0.9, conflict_threshold=0.9)


def test_the_ordered_defaults_construct() -> None:
    _stage()
    _stage(merge_threshold=0.95, conflict_threshold=0.83)


def test_the_settings_defaults_are_ordered() -> None:
    """The shipped defaults must satisfy the invariant they are checked against —
    otherwise every unconfigured deployment fails to start."""
    settings = LearningSettings(_env_file=None)
    assert settings.learning_dedup_conflict_threshold <= settings.learning_dedup_merge_threshold


def test_the_composition_root_surfaces_the_error(monkeypatch) -> None:
    """End-to-end: an inverted pair in the ENV must abort `build_learning_consumer` at
    process start rather than build a stage that mis-adjudicates forever."""
    from data_agent.learning import factory as factory_module

    settings = LearningSettings(
        _env_file=None,
        learning_dedup_merge_threshold=0.50,
        learning_dedup_conflict_threshold=0.99,
    )
    with pytest.raises(ThresholdConfigError):
        factory_module.build_learning_consumer(
            settings,
            session_store=object(),  # type: ignore[arg-type]
            queue=object(),  # type: ignore[arg-type]
            model_client=object(),
            audit_store=object(),  # type: ignore[arg-type]
            candidate_store=object(),  # type: ignore[arg-type]
            blueprint_corpus=InMemoryBlueprintCorpus(),
            user_store=object(),  # type: ignore[arg-type]
            catalog_schema={},
        )
