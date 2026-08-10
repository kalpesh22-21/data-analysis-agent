# dedup — Slice 6, the D48 two-layer blueprint dedup CandidateStage (Track B).
#
# Writes `envelope.dedup` (Contract C, `DedupVerdict`). Layer 1 = the race-safe
# canonical hard key; Layer 2 = embedding-similarity soft adjudication on `intent`.
# The `BlueprintCorpus` port + its in-memory fake back the hard-key lookup and the
# `hit_count` bump (which lives on the corpus ARTIFACT, never the envelope).
from .canonical_key import compute_canonical_key
from .corpus import BlueprintCorpus, CorpusArtifact, InMemoryBlueprintCorpus
from .stage import DedupStage, ThresholdConfigError

__all__ = [
    "BlueprintCorpus",
    "CorpusArtifact",
    "DedupStage",
    "InMemoryBlueprintCorpus",
    "ThresholdConfigError",
    "compute_canonical_key",
]
