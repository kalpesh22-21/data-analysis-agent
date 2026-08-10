# dedup — Slice 6, the blueprint dedup CandidateStage (Track B, D48 + PriorArt S2).
#
# Writes `envelope.dedup` (Contract C, `DedupVerdict`). THREE layers, in decreasing
# certainty: (1) the race-safe frozen canonical hard key over the `learning_corpus`
# bucket; (2) the LOOSE cross-tier structural key through the `PriorArtIndex` — the
# only layer that can see the MCP canon; (3) embedding-similarity soft adjudication
# on `intent`. The `BlueprintCorpus` port + its in-memory fake back the hard-key
# lookup, the `hit_count` bump (which lives on the corpus ARTIFACT, never the
# envelope), and the terminal-status stamp the S9 reject/retract edges write.
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
