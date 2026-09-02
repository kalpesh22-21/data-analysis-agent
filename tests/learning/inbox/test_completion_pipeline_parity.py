"""The completion plane and the consumer must assemble ONE pipeline, not two that agree.

`build_write_router_stages` has been shared since the completion plane was built, so the stage
ORDER could not fork. The COLLABORATORS could, and did: `inbox/service.py::_build_completer`
called the shared builder without `prior_art` and without a coverage `judge`, so every candidate
that reaches the store through a human — a completed form, a minted blueprint, and now an
assistant SQL REWRITE — was deduped on strictly less evidence than one the loop mined:

  * no `prior_art` ⇒ `dedup/stage.py`'s CROSS-TIER layer returns early. The MCP canon and the
    landed learning tier are invisible, and the candidate is compared only against the
    `learning_corpus` bucket, which this loop seeds itself;
  * no `judge` ⇒ S6's layer-3b judged near-miss never runs, so a soft-similar artifact is
    adjudicated on thresholds alone.

⚠ THAT ASYMMETRY POINTS THE WRONG WAY. The hand-authored path is where SQL enters that no
warehouse ever answered; it is the path that needs MORE evidence, not less. These tests pin the
collaborators as part of the parity, because "same order" was true the whole time the gap
existed and would have been true for as long as nobody looked.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.config import LearningSettings
from data_agent.learning.factory import build_write_router_stages
from data_agent.learning.inbox.service import _build_completer
from tests._catalog_fixture import fixture_catalog

_CATALOG = fixture_catalog()

# The two the completion path deliberately omits — see `build_write_router_stages`'s
# `include_target_specific`. `needs_parameterization` is a blueprint-only status, so neither
# would do anything except demand collaborators this process has no other use for.
_TARGET_SPECIFIC = ("schema_edit_writer", "user_knowledge_writer")


def _settings() -> LearningSettings:
    return LearningSettings(_env_file=None)


class _Runtime:
    def __init__(self, path: Any) -> None:
        self._path = path

    def catalog_fixture_file(self) -> Any:
        return self._path


class _FakePriorArt:
    """A stand-in with identity, which is the whole point: the test asserts the completion
    plane holds THE object it was given, not merely something of the right shape."""


class _FakeJudge:
    pass


def _completer(tmp_path, **collaborators: Any):
    path = tmp_path / "catalog_export.json"
    path.write_text(json.dumps({"catalog": _CATALOG}), encoding="utf-8")
    return _build_completer(
        _settings(),
        _Runtime(path),
        candidate_store=InMemoryCandidateStore(),
        corpus=object(),
        embedding_client=object(),
        **collaborators,
    )


def _stage(stages, stage_id: str):
    return next(s for s in stages if s.stage_id == stage_id)


# --- the order (already shared; pinned so it stays that way) ------------------


def test_the_completion_pipeline_is_the_consumers_minus_the_two_target_specific_stages(
    tmp_path,
) -> None:
    """ONE builder, TWO callers, and the difference between them is declared rather than
    incidental. "Leakage after dedup in one process and before it in another" fails nothing
    until a leak lands."""
    consumer = build_write_router_stages(
        _settings(),
        candidate_store=InMemoryCandidateStore(),
        blueprint_corpus=object(),  # type: ignore[arg-type]
        catalog_schema={},
        embedder=object(),  # type: ignore[arg-type]
        user_store=object(),  # type: ignore[arg-type]
    )
    completion = _completer(tmp_path).stages

    assert [s.stage_id for s in completion] == [
        s.stage_id for s in consumer if s.stage_id not in _TARGET_SPECIFIC
    ]
    assert [s.stage_id for s in completion] == ["generalize", "leakage", "dedup", "writer"]


# --- the collaborators (the gap this closes) ---------------------------------


def test_the_completion_dedup_stage_holds_the_prior_art_index_it_was_given(
    tmp_path,
) -> None:
    """⚠ THE CROSS-TIER LAYER IS OFF WITHOUT THIS. A completed, minted or SQL-REWRITTEN
    blueprint deduped against the `learning_corpus` bucket alone is deduped against what this
    loop already minted and nothing else — the MCP canon and the landed learning tier, which are
    exactly where an already-owned blueprint would be found, are invisible."""
    index = _FakePriorArt()
    dedup = _stage(_completer(tmp_path, prior_art=index).stages, "dedup")
    assert dedup._prior_art is index


def test_the_completion_dedup_stage_holds_the_coverage_judge_it_was_given(
    tmp_path,
) -> None:
    """S6 layer-3b DECIDES rather than observes: it separates a genuine duplicate from a
    neighbour. Without it a human-finished blueprint is adjudicated on similarity thresholds
    alone, which is less evidence than a mined one gets."""
    judge = _FakeJudge()
    dedup = _stage(_completer(tmp_path, judge=judge).stages, "dedup")
    assert dedup._judge is judge


def test_the_completion_leakage_stage_holds_the_scanner_it_was_given(tmp_path) -> None:
    """Passed through even though nothing wires a real one on EITHER side today. The parity
    has to be structural, not a coincidence of two `None`s that a later slice would break on
    one side only."""
    scanner = object()
    leakage = _stage(_completer(tmp_path, semantic_scanner=scanner).stages, "leakage")
    assert leakage.semantic_scanner is scanner


def test_the_same_collaborators_produce_the_same_stage_shape_on_both_sides(
    tmp_path,
) -> None:
    """The parity stated as one assertion: hand the two builders the same objects and the
    stages that read them agree, field by field, on the blueprint half."""
    index, judge, scanner = _FakePriorArt(), _FakeJudge(), object()
    consumer = build_write_router_stages(
        _settings(),
        candidate_store=InMemoryCandidateStore(),
        blueprint_corpus=object(),  # type: ignore[arg-type]
        catalog_schema={},
        embedder=object(),  # type: ignore[arg-type]
        prior_art=index,
        judge=judge,
        semantic_scanner=scanner,
        include_target_specific=False,
    )
    completion = _completer(
        tmp_path, prior_art=index, judge=judge, semantic_scanner=scanner
    ).stages

    assert [s.stage_id for s in completion] == [s.stage_id for s in consumer]
    for stage_id, attr in (("dedup", "_prior_art"), ("dedup", "_judge")):
        assert getattr(_stage(completion, stage_id), attr) is getattr(
            _stage(consumer, stage_id), attr
        )
    assert (
        _stage(completion, "leakage").semantic_scanner
        is _stage(consumer, "leakage").semantic_scanner
    )


# --- the degrade is legal, and loud ------------------------------------------


@pytest.mark.parametrize("missing", ["prior_art", "judge"])
def test_a_missing_collaborator_degrades_loudly_rather_than_refusing(
    tmp_path, caplog: pytest.LogCaptureFixture, missing: str
) -> None:
    """LEGAL, because a deployment with no graph must still be able to complete a form — a
    completion that re-validates is worth more than one refused for want of dedup evidence.
    LOUD, because the symptom of a missing cross-tier layer is a duplicate blueprint landing
    weeks later, which points nowhere near the cause."""
    given = {"prior_art": _FakePriorArt(), "judge": _FakeJudge()}
    given.pop(missing)
    with caplog.at_level("WARNING", logger="data_agent.learning.inbox.service"):
        completer = _completer(tmp_path, **given)
    assert completer is not None  # it still builds
    assert any(
        missing.replace("_", "-") in record.message
        or {"prior_art": "prior-art", "judge": "coverage judge"}[missing] in record.message
        for record in caplog.records
    ), caplog.text


def test_the_fully_wired_case_says_so_at_info(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """The complement, and it is worth a line: an operator reading these logs must be able to
    tell "the full evidence set" from "nobody logged anything"."""
    with caplog.at_level("INFO", logger="data_agent.learning.inbox.service"):
        _completer(tmp_path, prior_art=_FakePriorArt(), judge=_FakeJudge())
    assert any("FULL write-router evidence set" in r.message for r in caplog.records)


# --- the two judges share their collaborators --------------------------------


def test_the_two_completion_judges_share_one_model_client_and_audit_store(
    tmp_path,
) -> None:
    """⚠ ONE OF EACH, in a process that needs one of each.

    Both completion-path judges used to construct their own model client and their own
    `CouchbaseAuditStore`. A deployment with both switched on therefore opened two audit
    connections, and — the part that is not merely wasteful — wrote its two kinds of verdict
    about the SAME candidate through two different store objects. The consumer has always shared
    them; this is the same shape at the second composition root.

    Asserted by IDENTITY through the public builders, with a fake pair injected, because that is
    the only thing that distinguishes "shared" from "two objects that happen to be equivalent".
    """
    from data_agent.learning.inbox.service import (
        _build_completion_coverage_judge,
        _build_completion_param_judge,
        _JudgeDeps,
    )

    deps = _JudgeDeps(model_client=object(), audit_store=object())
    settings = _settings()
    # Both switches ON, so both builders get past their kill-switch and reach `deps`.
    object.__setattr__(settings, "learning_param_judge_enabled", True)
    object.__setattr__(settings, "learning_judge_enabled", True)

    param = _build_completion_param_judge(settings, deps)
    coverage = _build_completion_coverage_judge(settings, _FakePriorArt(), deps)

    assert param is not None and coverage is not None
    # The two classes name their fields differently — `ParameterizationJudge` is a dataclass
    # with `model_client`/`audit_store`, `CoverageJudge` keeps privates — so the assertion goes
    # through each one's own accessor rather than a shared shape neither promises.
    assert param.model_client is deps.model_client
    assert param.audit_store is deps.audit_store
    assert coverage._model_client is deps.model_client
    assert coverage._audit is deps.audit_store


def test_nothing_is_constructed_when_both_judges_are_off() -> None:
    """LAZY, and it matters: the default posture has both off, and a `_JudgeDeps` built anyway
    would open a Couchbase connection and an OpenAI client for two objects nobody will use."""
    from data_agent.learning.inbox.service import _judge_deps_if_needed

    settings = _settings()
    object.__setattr__(settings, "learning_param_judge_enabled", False)
    object.__setattr__(settings, "learning_judge_enabled", False)

    assert _judge_deps_if_needed(settings) is None
