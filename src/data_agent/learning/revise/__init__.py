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

`KnowledgeReviser` (`knowledge.py`) is the SIBLING for the `global_knowledge` target
(`docs/decisions/knowledge-edit-and-user-promotion-design.md` §C.1). Same mechanics, same
write-nothing split — the proposal is applied through `apply_knowledge`, which re-runs intake
validation and the leakage scan — and one inversion worth knowing before reading it: the
blueprint reviser is REFUSED on a candidate whose scan did not clear, while this one is OFFERED
precisely then, so its withholding rule sits on the OUTPUT instead of the input.
"""

from .diff import EntryDiff, diff_parameterization
from .engine import (
    SQL_REWRITE_CAUTION,
    BlueprintReviser,
    ReviseProposal,
    ReviserUnavailableError,
)
from .knowledge import (
    KnowledgeFieldDiff,
    KnowledgeProposal,
    KnowledgeReviser,
    KnowledgeScanner,
    knowledge_diff,
)
from .knowledge_schema import (
    KNOWLEDGE_SURFACES,
    ForbiddenKnowledgeEditError,
    build_knowledge_tool,
)
from .schema import (
    ForbiddenTemplateEditError,
    SqlRewriteUnsupportedError,
    build_revise_tool,
)

__all__ = [
    "KNOWLEDGE_SURFACES",
    "SQL_REWRITE_CAUTION",
    "BlueprintReviser",
    "EntryDiff",
    "ForbiddenKnowledgeEditError",
    "ForbiddenTemplateEditError",
    "KnowledgeFieldDiff",
    "KnowledgeProposal",
    "KnowledgeReviser",
    "KnowledgeScanner",
    "ReviseProposal",
    "ReviserUnavailableError",
    "SqlRewriteUnsupportedError",
    "build_knowledge_tool",
    "build_revise_tool",
    "diff_parameterization",
    "knowledge_diff",
]
