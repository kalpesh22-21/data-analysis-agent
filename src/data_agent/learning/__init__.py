# learning — the offline learning-loop infra spine (Track B, Slice 1; D96/D95).
#
# Transport + lifecycle ONLY: the idle-detection sweeper, the six-state
# `learning_status` machine, a real Redis Streams queue (+ in-memory fake), a
# no-op consumer, and the D58c kill-switch. No extractor / writers / leakage gate
# / promotion (later slices). See docs/decisions/learning-loop-infra-design.md.
from .config import LearningSettings, get_learning_settings, learning_enabled
from .consumer import ConsumeResult, LearningConsumer
from .memory_queue import InMemoryLearningQueue
from .models import (
    SWEEPABLE_STATUSES,
    VALID_TRANSITIONS,
    LearningJob,
    LearningStatus,
    compute_content_hash,
)
from .queue import DeliveredJob, LearningQueue
from .state_machine import InvalidTransitionError, transition
from .sweeper import LearningSweeper, SweepResult

__all__ = [
    "SWEEPABLE_STATUSES",
    "VALID_TRANSITIONS",
    "ConsumeResult",
    "DeliveredJob",
    "InMemoryLearningQueue",
    "InvalidTransitionError",
    "LearningConsumer",
    "LearningJob",
    "LearningQueue",
    "LearningSettings",
    "LearningStatus",
    "LearningSweeper",
    "SweepResult",
    "compute_content_hash",
    "get_learning_settings",
    "learning_enabled",
    "transition",
]
