"""The parameterization judge's PLACEMENT in the frozen write-router order (design §D.1).

`tests/learning/inbox/test_fail_to_review_wiring.py` pins the order for both callers WITHOUT
the judge. Nothing pins it WITH one, and the design says position — not presence — is the thing
to hold:

    GeneralizeStage → [ParameterizationJudgeStage] → LeakageGateStage → DedupStage → … → Writer

  * AFTER generalize, because every finding the judge can make is about the REWRITTEN TEMPLATE
    and its relationship to the intent. That artifact does not exist before the AST rewrite, so
    a judge placed earlier would be judging the S3 plan — a different object, about which none
    of its finding classes can be stated.
  * BEFORE leakage, because the phase-D-2 repair loop MUTATES the payload, and settling an
    entity scan and then changing what it was settled about is the exact bug
    `inbox/completion.py::_still_declined` had to add a re-settle to fix. D-1 mutates nothing;
    the position is what D-2 must not have to move, and moving it is a one-line edit that
    breaks nothing else.

Both callers are covered because `include_target_specific=False` — the human-completion path —
is the one a reader would assume does not need a judge. Design §D.1 says it does: *"a
human-completed form should face the same bar as an extracted one"*, and a judge wired into only
one of the two makes the phase-D-1 dataset a biased sample of the population it is measuring.
"""

from __future__ import annotations

import pytest

from data_agent.learning.audit.memory_audit_store import InMemoryAuditStore
from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.config import LearningSettings
from data_agent.learning.factory import build_param_judge, build_write_router_stages
from data_agent.learning.paramjudge import (
    ParameterizationJudge,
    ParameterizationJudgeStage,
    ParamJudgeConfig,
)


def _settings(**overrides: object) -> LearningSettings:
    return LearningSettings(_env_file=None, **overrides)  # type: ignore[arg-type]


class _Client:
    async def send_turn(self, messages: list[dict], tools: list[dict]):  # pragma: no cover
        raise AssertionError("the wiring tests never call the model")


def _judge() -> ParameterizationJudge:
    return ParameterizationJudge(
        model_client=_Client(),  # type: ignore[arg-type]
        audit_store=InMemoryAuditStore(),
        config=ParamJudgeConfig(model="m", timeout_seconds=1.0),
    )


def _stages(*, judge, include_target_specific: bool):
    kwargs: dict[str, object] = {
        "candidate_store": InMemoryCandidateStore(),
        "blueprint_corpus": object(),
        "catalog_schema": {},
        "embedder": object(),
        "param_judge": judge,
        "include_target_specific": include_target_specific,
    }
    if include_target_specific:
        kwargs["user_store"] = object()
    return build_write_router_stages(_settings(), **kwargs)  # type: ignore[arg-type]


# --- position ------------------------------------------------------------------


@pytest.mark.parametrize("include_target_specific", [True, False])
def test_the_judge_lands_between_generalize_and_leakage(
    include_target_specific: bool,
) -> None:
    ids = [s.stage_id for s in _stages(judge=_judge(), include_target_specific=include_target_specific)]

    assert "param_judge" in ids
    # Stated as ORDERING facts rather than as one list equality, so a future stage added
    # elsewhere in the pipeline does not have to be reflected here to keep this claim true.
    assert ids.index("generalize") < ids.index("param_judge") < ids.index("leakage")
    # ...and it is IMMEDIATELY after generalize, which is the stronger claim: nothing may be
    # inserted between the rewrite and the judgement of the rewrite.
    assert ids[ids.index("generalize") + 1] == "param_judge"


@pytest.mark.parametrize("include_target_specific", [True, False])
def test_the_rest_of_the_frozen_order_is_undisturbed(include_target_specific: bool) -> None:
    """The insertion must be exactly that — an insertion. `build_write_router_stages` was
    restructured (a list literal became a list plus an `extend`) to make room for it, which is
    the kind of edit that drops a stage without any test noticing."""
    ids = [s.stage_id for s in _stages(judge=_judge(), include_target_specific=include_target_specific)]

    expected = ["generalize", "param_judge", "leakage", "dedup"]
    if include_target_specific:
        expected += ["schema_edit_writer", "user_knowledge_writer"]
    expected += ["writer"]
    assert ids == expected


@pytest.mark.parametrize("include_target_specific", [True, False])
def test_the_stage_is_absent_when_no_judge_is_passed(include_target_specific: bool) -> None:
    """⚠ NOT BUILT, not built-and-disabled. Design §D.0 rejects a dormant branch behind a
    flag: *"a stage that is not built cannot act, which is a stronger guarantee than a stage
    that is built and told not to."*"""
    ids = [s.stage_id for s in _stages(judge=None, include_target_specific=include_target_specific)]

    assert "param_judge" not in ids
    expected = ["generalize", "leakage", "dedup"]
    if include_target_specific:
        expected += ["schema_edit_writer", "user_knowledge_writer"]
    expected += ["writer"]
    assert ids == expected


def test_the_stage_carries_the_judge_it_was_handed() -> None:
    """One judge instance, not a rebuilt one: the judge holds the audit store the D-1 rows
    land in, and a stage that constructed its own would write them somewhere else."""
    judge = _judge()
    stages = _stages(judge=judge, include_target_specific=False)
    stage = next(s for s in stages if s.stage_id == "param_judge")
    assert isinstance(stage, ParameterizationJudgeStage)
    assert stage.judge is judge


# --- the three build preconditions --------------------------------------------


def test_the_judge_is_not_built_when_the_switch_is_off() -> None:
    """The default, and the production posture. A measurement nobody asked for must not start
    running because its dependencies happen to be present."""
    assert (
        build_param_judge(
            _settings(), model_client=_Client(), audit_store=InMemoryAuditStore()
        )
        is None
    )


def test_the_judge_is_not_built_without_a_model_client() -> None:
    assert (
        build_param_judge(
            _settings(learning_param_judge_enabled=True),
            model_client=None,
            audit_store=InMemoryAuditStore(),
        )
        is None
    )


def test_the_judge_is_not_built_without_an_audit_store() -> None:
    """The durable row is this phase's ONLY output, so running without somewhere to put it
    would burn model calls and produce nothing."""
    assert (
        build_param_judge(
            _settings(learning_param_judge_enabled=True),
            model_client=_Client(),
            audit_store=None,
        )
        is None
    )


def test_shadow_is_hard_wired_true_whatever_the_setting_says() -> None:
    """⚠ Design §D.0: *"Do not implement a discard path in D-1 — a dormant discard branch
    guarded by a flag is how a bar nobody validated ends up live after a config change."*

    `learning_param_judge_shadow_mode` exists so D-2 does not have to invent it, and its
    documented D-1 behaviour is that setting it False changes nothing. Asserted, because the
    setting's mere presence is what would make a future reader believe flipping it does
    something — and `LearningSettings` is `extra="ignore"`, so nothing else would complain.
    """
    judge = build_param_judge(
        _settings(learning_param_judge_enabled=True, learning_param_judge_shadow_mode=False),
        model_client=_Client(),
        audit_store=InMemoryAuditStore(),
    )
    assert judge is not None
    assert judge.config.shadow is True


def test_an_empty_model_id_falls_back_to_the_extractors() -> None:
    """Documented as `learning_judge_model` behaves. Pinned because the record carries the
    model id and a dataset that silently mixes two judges cannot be read."""
    judge = build_param_judge(
        _settings(
            learning_param_judge_enabled=True,
            learning_param_judge_model="",
            learning_extractor_model="gpt-extractor",
        ),
        model_client=_Client(),
        audit_store=InMemoryAuditStore(),
    )
    assert judge is not None
    assert judge.config.model == "gpt-extractor"
