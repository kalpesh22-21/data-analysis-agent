"""LLM-assisted parameterization editing (design §C).

A reviewer types a sentence; the reviser proposes `parameterization` entries. It writes
NOTHING — the proposal is applied through the existing `complete` route, so it faces the same
`to_candidate` re-validation and the same write-router stages a hand-typed array faces.

The invariant that makes it safe: THE MODEL EDITS ENTRIES, NEVER THE SQL. See `schema.py`.
"""

from .diff import EntryDiff, diff_parameterization
from .engine import (
    BlueprintReviser,
    ReviseProposal,
    ReviserUnavailableError,
)
from .schema import ForbiddenTemplateEditError, build_revise_tool

__all__ = [
    "BlueprintReviser",
    "EntryDiff",
    "ForbiddenTemplateEditError",
    "ReviseProposal",
    "ReviserUnavailableError",
    "build_revise_tool",
    "diff_parameterization",
]
