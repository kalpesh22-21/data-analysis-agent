"""The parameterization judge (design §D), phase D-1: OBSERVE ONLY.

It asks one question the five deterministic S4 checks cannot reach — *are these literal ROLES
right?* — and does nothing with the answer except record it and stamp the reviewer card. There
is no discard path in this package. See `judge.py` for why that is the deliverable rather than a
staged version of a bigger one.
"""

from ..audit.judgement import (
    FINDING_CLASSES,
    PARAM_VERDICTS,
    ParamAssessment,
    ParamFinding,
    ParamJudgeRecord,
    param_judgement_ref,
)
from .judge import ParameterizationJudge, ParamJudgeOutcome
from .models import ParamJudgeConfig
from .stage import ParameterizationJudgeStage

__all__ = [
    "FINDING_CLASSES",
    "PARAM_VERDICTS",
    "ParamAssessment",
    "ParamFinding",
    "ParamJudgeConfig",
    "ParamJudgeOutcome",
    "ParamJudgeRecord",
    "ParameterizationJudge",
    "ParameterizationJudgeStage",
    "param_judgement_ref",
]
