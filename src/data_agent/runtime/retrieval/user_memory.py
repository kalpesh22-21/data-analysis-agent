"""UserMemoryProvider — the user-memory pre-injection seam (design §3.2/§7).

03 lists user memory (prefs / entity defaults) in the pre-injection block, but
no user store exists yet (OQ-R4). Slice 1 ships the seam plus a
`NullUserMemoryProvider` that returns `[]` — honest degradation, no fabricated
defaults. The real provider lands when the user store does.

User memory is NEVER a model-facing tool (it is personal, entity-bearing) — it
only ever reaches the model via runtime pre-injection.
"""

from __future__ import annotations

from typing import Protocol

from .models import UserMemoryItem


class UserMemoryProvider(Protocol):
    """The user-memory seam the pipeline depends on."""

    async def fetch(
        self, *, user_id: str | None, column_scope: frozenset[str]
    ) -> list[UserMemoryItem]:
        """Return this user's pre-injectable memory items (may be empty)."""
        ...


class NullUserMemoryProvider:
    """Slice-1 provider — always empty (user store not built, OQ-R4)."""

    async def fetch(
        self, *, user_id: str | None, column_scope: frozenset[str]
    ) -> list[UserMemoryItem]:
        return []


__all__ = ["NullUserMemoryProvider", "UserMemoryProvider"]
