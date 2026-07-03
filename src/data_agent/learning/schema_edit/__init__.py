# schema_edit — the schema-edit PR bot (Track B, Slice 8; D53/D18).
# A `CandidateStage` that opens a branch + YAML-patch PR (via an INJECTED git
# client) and routes to human review — NEVER auto-commits to the catalog.
from .client import AllPassChecks, GitPullRequestClient, SchemaEditChecks
from .models import (
    CheckResult,
    PullRequestResult,
    PullRequestSpec,
    SchemaEditPatch,
)
from .pr_stage import SchemaEditPRStage

__all__ = [
    "AllPassChecks",
    "CheckResult",
    "GitPullRequestClient",
    "PullRequestResult",
    "PullRequestSpec",
    "SchemaEditChecks",
    "SchemaEditPRStage",
    "SchemaEditPatch",
]
