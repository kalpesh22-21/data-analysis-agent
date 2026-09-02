"""LLM-assisted parameterization editing (design §C).

A reviewer types a sentence; the reviser proposes `parameterization` entries. It writes
NOTHING — the proposal is applied through the existing `complete` route, so it faces the same
`to_candidate` re-validation and the same write-router stages a hand-typed array faces.

The invariant that makes it safe: THE MODEL EDITS ENTRIES, NEVER THE TEMPLATE. See `schema.py`.

§C.5: with the reviewer's explicit `allow_sql` opt-in it may also return a COMPLETE replacement
ACCEPTED SQL. That is a different claim — the template is still derived — and it is safe for
`learning/mint`'s reason rather than for the provenance one: the result is stamped
`authored=True`, faces the same completer and the stricter totality walk, and can never
auto-land. The reviewer is shown a caution saying so.
"""

from .diff import EntryDiff, diff_parameterization
from .engine import (
    SQL_REWRITE_CAUTION,
    BlueprintReviser,
    ReviseProposal,
    ReviserUnavailableError,
)
from .schema import (
    ForbiddenTemplateEditError,
    SqlRewriteUnsupportedError,
    build_revise_tool,
)

__all__ = [
    "SQL_REWRITE_CAUTION",
    "BlueprintReviser",
    "EntryDiff",
    "ForbiddenTemplateEditError",
    "ReviseProposal",
    "ReviserUnavailableError",
    "SqlRewriteUnsupportedError",
    "build_revise_tool",
    "diff_parameterization",
]
