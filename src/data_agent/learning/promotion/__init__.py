"""promotion — the S9 cron-scanned promotion scheduler + golden replay (D29/§7.2).

A STANDALONE background process (NOT a `CandidateStage`) that reads
`learning_candidates` by `status`, runs golden replay (reusing the D56
`verify_result` gate + the runtime template binder) + the D43 drift probes, and
advances `status` + stamps `drift` (Contract E). See
`docs/decisions/learning-loop-s9-promotion-design.md`.
"""

from .drift import (
    CATALOG_CONFORMANCE,
    GRAIN_INTEGRITY,
    LIVE_PROBES,
    RULE_CURRENCY,
    STUBBED_PROBES,
    drift_from_replay,
    silent_eligible,
    user_correction_stamp,
)
from .models import (
    BLUEPRINT_TYPE,
    HUMAN_GATED_TYPES,
    CandidateDecision,
    DependencyResolver,
    HitCountReader,
    ProbeResult,
    PromotionPolicy,
    PromotionSweep,
    WarehouseProbe,
)
from .replay import ReplayOutcome, golden_replay
from .scheduler import PromotionScheduler

__all__ = [
    "BLUEPRINT_TYPE",
    "CATALOG_CONFORMANCE",
    "GRAIN_INTEGRITY",
    "HUMAN_GATED_TYPES",
    "LIVE_PROBES",
    "RULE_CURRENCY",
    "STUBBED_PROBES",
    "CandidateDecision",
    "DependencyResolver",
    "HitCountReader",
    "ProbeResult",
    "PromotionPolicy",
    "PromotionScheduler",
    "PromotionSweep",
    "ReplayOutcome",
    "WarehouseProbe",
    "drift_from_replay",
    "golden_replay",
    "silent_eligible",
    "user_correction_stamp",
]
