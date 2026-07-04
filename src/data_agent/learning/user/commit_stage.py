"""UserKnowledgeCommitStage — the S8 auto-commit writer stage (`CandidateStage`, D17).

`user_knowledge` is the one target that auto-commits (05 §Write targets): it is
scoped to a single user and surfaced only in that user's context, so it never
needs the human pre-gate the global targets do. This stage runs at the writer
position of the pipeline (§7.1): for a `user_knowledge` candidate it commits the
projected record into the per-user store (scoped to `user_id`) and emits
`control="drop"` so the enriched envelope NEVER reaches the review inbox — the
candidate holding store is not its home; the per-user store is.

Every other candidate type passes straight through (`control="continue"`) — the
stage is a no-op for anything but `user_knowledge`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..candidate.models import CandidateEnvelope, CandidateStatus
from ..stage import StageContext, StageResult
from .models import UserKnowledgeRecord
from .store import UserKnowledgeStore


@dataclass(frozen=True)
class UserKnowledgeCommitStage:
    """The injected S8 auto-commit stage. `store` is the per-user knowledge store
    (its RBAC role is scoped to its own bucket)."""

    store: UserKnowledgeStore
    stage_id: str = "user_knowledge_writer"

    async def process(
        self, env: CandidateEnvelope, ctx: StageContext
    ) -> StageResult:
        if env.type != "user_knowledge":
            return StageResult(envelope=env, control="continue")

        # Scope to the SESSION's authenticated user (ctx.summary.user_id), NEVER the
        # LLM-supplied payload.user_id (R6/D17) — a foreign payload user_id can never
        # write into another user's surface.
        record = UserKnowledgeRecord.from_candidate(env, user_id=ctx.summary.user_id)
        # Auto-commit scoped to that user_id (D17). The commit is the authoritative
        # write; the enriched envelope is dropped, never persisted into the candidate
        # holding store / inbox.
        await self.store.commit(record)
        committed = replace(env, status=CandidateStatus.VALIDATED)
        return StageResult(envelope=committed, control="drop")
