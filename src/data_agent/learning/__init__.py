# learning — the offline learning-loop (Track B).
#
# Slice 1 (D96/D95): transport + lifecycle — the idle-detection sweeper, the
# six-state `learning_status` machine, a real Redis Streams queue (+ in-memory
# fake), the consumer, and the D58c kill-switch.
# Slice 2 (D99/D100): the SessionSummary loader + deterministic triage gate + the
# provisioned `learning_audit` store (client stood up, no evidence written yet).
# No extractor / writers / leakage gate / promotion (later slices).
from .audit import AuditStore, EvidenceSnapshot, InMemoryAuditStore, mint_evidence_ref
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
from .summary import SessionSummary, load_session_summary
from .sweeper import LearningSweeper, SweepResult
from .triage import TriageVerdict, triage

__all__ = [
    "SWEEPABLE_STATUSES",
    "VALID_TRANSITIONS",
    "AuditStore",
    "ConsumeResult",
    "DeliveredJob",
    "EvidenceSnapshot",
    "InMemoryAuditStore",
    "InMemoryLearningQueue",
    "InvalidTransitionError",
    "LearningConsumer",
    "LearningJob",
    "LearningQueue",
    "LearningSettings",
    "LearningStatus",
    "LearningSweeper",
    "SessionSummary",
    "SweepResult",
    "TriageVerdict",
    "compute_content_hash",
    "get_learning_settings",
    "learning_enabled",
    "load_session_summary",
    "mint_evidence_ref",
    "transition",
    "triage",
]
