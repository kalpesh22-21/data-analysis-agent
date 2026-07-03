# candidate — the learning_candidates holding store (Track B, Slice 3; D101).
# CouchbaseCandidateStore is imported directly by the entrypoint (guarded on the
# couchbase SDK), so it is NOT re-exported here to keep Layer-1 imports light.
from .generalization import (
    BlueprintGeneralization,
    NodeTemplate,
    ResultGrainStamp,
    StaticValidation,
)
from .memory_candidate_store import InMemoryCandidateStore
from .models import CandidateEnvelope, CandidateStatus, build_envelope, mint_candidate_id
from .store import CandidateStore
from .verdicts import DedupVerdict, DriftStamp, EntityHit, LeakageVerdict

__all__ = [
    "BlueprintGeneralization",
    "CandidateEnvelope",
    "CandidateStatus",
    "CandidateStore",
    "DedupVerdict",
    "DriftStamp",
    "EntityHit",
    "InMemoryCandidateStore",
    "LeakageVerdict",
    "NodeTemplate",
    "ResultGrainStamp",
    "StaticValidation",
    "build_envelope",
    "mint_candidate_id",
]
