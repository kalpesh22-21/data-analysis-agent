# learning — the offline learning-loop (Track B).
#
# Slice 1 (D96/D95): transport + lifecycle — the idle-detection sweeper, the
# six-state `learning_status` machine, a real Redis Streams queue (+ in-memory
# fake), the consumer, and the D58c kill-switch.
# Slice 2 (D99/D100): the SessionSummary loader + deterministic triage gate + the
# provisioned `learning_audit` store (client stood up, no evidence written yet).
# Slice 3 (D31/D34/D97/D98/D101): the grounded structured-output extractor + the
# `learning_candidates` holding store (first real evidence writes). No writers into
# the recall stores / leakage gate / dedup / promotion (later slices).
from .audit import AuditStore, EvidenceSnapshot, InMemoryAuditStore, mint_evidence_ref
from .candidate import (
    CandidateEnvelope,
    CandidateStatus,
    CandidateStore,
    InMemoryCandidateStore,
)
from .config import LearningSettings, get_learning_settings, learning_enabled
from .consumer import ConsumeResult, LearningConsumer
from .extractor import (
    ExtractedCandidate,
    ExtractionResult,
    ExtractorConfig,
    LearningExtractor,
)
from .factory import (
    LearningWiringError,
    build_learning_consumer,
    build_promotion_plane,
    build_promotion_scheduler,
    build_review_inbox,
)
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
    "CandidateEnvelope",
    "CandidateStatus",
    "CandidateStore",
    "ConsumeResult",
    "DeliveredJob",
    "EvidenceSnapshot",
    "ExtractedCandidate",
    "ExtractionResult",
    "ExtractorConfig",
    "InMemoryAuditStore",
    "InMemoryCandidateStore",
    "InMemoryLearningQueue",
    "InvalidTransitionError",
    "LearningConsumer",
    "LearningExtractor",
    "LearningJob",
    "LearningQueue",
    "LearningSettings",
    "LearningStatus",
    "LearningSweeper",
    "LearningWiringError",
    "SessionSummary",
    "SweepResult",
    "TriageVerdict",
    "build_learning_consumer",
    "build_promotion_plane",
    "build_promotion_scheduler",
    "build_review_inbox",
    "compute_content_hash",
    "get_learning_settings",
    "learning_enabled",
    "load_session_summary",
    "mint_evidence_ref",
    "transition",
    "triage",
]
