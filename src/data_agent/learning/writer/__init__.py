# writer — Slice 7, the write-router `CandidateStage` (Track B).
#
# Runs LAST in the pipeline (generalize → leakage → dedup → **writer**). It reads
# the upstream verdicts (`entity_scan`, `dedup`, `generalization.static_validation`)
# + the candidate `type` and sets the terminal consumer-owned `status`:
#   - clean blueprint            → status=in_review by default (100% human-review sample)
#   - global_knowledge / schema_edit → status=in_review (human pre-gate, D58a/D18)
#   - fail_to_review / dedup conflict / leakage near-miss / sampled → status=in_review
# The review inbox (`learning/inbox/`) is a projection over these `in_review` rows.
from .routing import RoutingDecision, derive_inbox_reason, route_candidate
from .stage import WriterStage

__all__ = [
    "RoutingDecision",
    "WriterStage",
    "derive_inbox_reason",
    "route_candidate",
]
