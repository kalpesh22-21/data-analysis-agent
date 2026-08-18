"""judge — the coverage judge (plan §3b): "does the corpus already do this?".

`judge.py` holds the two-stage judge, `schema.py` the forced tool + the guard on the untrusted
response, `prompt.py` what each stage is shown. The verdict vocabulary and the durable record
deliberately live in `audit/judgement.py` instead, next to the doc shape that persists them —
the verdict is a record first and a branch second, and that placement also keeps
`candidate/models.py` out of an import cycle with this package.
"""

from .judge import (
    DROP_ELIGIBLE_ORIGINS,
    DROP_ELIGIBLE_TIERS,
    OUTCOME_DROPPED,
    OUTCOME_FAILED,
    OUTCOME_PROCEEDED,
    OUTCOME_RECORD_WRITE_FAILED,
    OUTCOME_SKIPPED_ABOVE_BAND,
    OUTCOME_SKIPPED_BELOW_FLOOR,
    OUTCOME_SKIPPED_NO_PRIOR_ART,
    OUTCOME_SKIPPED_UNAVAILABLE,
    CoverageJudge,
    JudgeConfig,
    JudgeConfigError,
    JudgeOutcomeResult,
)
from .schema import JUDGE_TOOL_NAME, VERDICT_ENUM, build_judge_tool, parse_assessment

__all__ = [
    "DROP_ELIGIBLE_ORIGINS",
    "DROP_ELIGIBLE_TIERS",
    "JUDGE_TOOL_NAME",
    "OUTCOME_DROPPED",
    "OUTCOME_FAILED",
    "OUTCOME_PROCEEDED",
    "OUTCOME_RECORD_WRITE_FAILED",
    "OUTCOME_SKIPPED_ABOVE_BAND",
    "OUTCOME_SKIPPED_BELOW_FLOOR",
    "OUTCOME_SKIPPED_NO_PRIOR_ART",
    "OUTCOME_SKIPPED_UNAVAILABLE",
    "VERDICT_ENUM",
    "CoverageJudge",
    "JudgeConfig",
    "JudgeConfigError",
    "JudgeOutcomeResult",
    "build_judge_tool",
    "parse_assessment",
]
