# candidate — the learning_candidates holding store (Track B, Slice 3; D101).
# CouchbaseCandidateStore is imported directly by the entrypoint (guarded on the
# couchbase SDK), so it is NOT re-exported here to keep Layer-1 imports light.
from .decline import DeclineBlock, EvidencePointer, ValidationSnapshot
from .generalization import (
    BlueprintGeneralization,
    NodeTemplate,
    ResultGrainStamp,
    StaticValidation,
)
from .memory_candidate_store import InMemoryCandidateStore
from .models import (
    CandidateEnvelope,
    CandidateStatus,
    JudgeRetryState,
    build_declined_envelope,
    build_envelope,
    mint_candidate_id,
    mint_review_candidate_id,
)
from .signals import NoveltyStamp, SessionSignals
from .store import CandidateStore
from .verdicts import DedupVerdict, DriftStamp, EntityHit, LeakageVerdict

__all__ = [
    "BlueprintGeneralization",
    "CandidateEnvelope",
    "CandidateStatus",
    "CandidateStore",
    "JudgeRetryState",
    "DeclineBlock",
    "DedupVerdict",
    "DriftStamp",
    "EntityHit",
    "EvidencePointer",
    "InMemoryCandidateStore",
    "LeakageVerdict",
    "NodeTemplate",
    "NoveltyStamp",
    "ResultGrainStamp",
    "SessionSignals",
    "StaticValidation",
    "ValidationSnapshot",
    "build_declined_envelope",
    "build_envelope",
    "mint_candidate_id",
    "mint_review_candidate_id",
]
