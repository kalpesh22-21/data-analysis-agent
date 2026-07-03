"""BlueprintCorpus — the landed-artifact port the S6 hard key looks up (D48).

The corpus is the set of already-LANDED blueprint artifacts (the neo4j blueprint
nodes in Layer 2/3), keyed by `canonical_key`. S6 asks it two things:

  * `get_by_canonical_key` — does an artifact with this exact hard key exist? A hit
    means this candidate is a semantic duplicate.
  * `increment_hit_count` — on a hard-key hit, bump the EXISTING artifact's
    `hit_count` (it lives on the artifact, a cross-session aggregate — NOT on the
    envelope, §11.1). The duplicate candidate is then dropped.

The soft layer additionally reads `list_artifacts()` to embed each artifact's
`intent` for the near-miss comparison.

`InMemoryBlueprintCorpus` is the Layer-1 fake, seedable from
`existing_corpus_keys.json`. It records `increment_hit_count` calls so a test can
assert "one create + one bump" without a warehouse.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Protocol


@dataclass(frozen=True)
class CorpusArtifact:
    """A landed blueprint artifact, as far as S6 dedup needs to see it."""

    id: str
    canonical_key: str
    intent: str
    hit_count: int = 1
    uses_rules: tuple[str, ...] = ()

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> CorpusArtifact:
        return cls(
            id=doc["id"],
            canonical_key=doc["canonical_key"],
            intent=doc.get("intent", ""),
            hit_count=int(doc.get("hit_count", 1)),
            uses_rules=tuple(doc.get("uses_rules", []) or []),
        )


class BlueprintCorpus(Protocol):
    """The landed-artifact lookup the S6 dedup stage depends on."""

    async def get_by_canonical_key(self, canonical_key: str) -> CorpusArtifact | None:
        """The artifact with this exact hard key, or `None` (no hard-key collision)."""
        ...

    async def increment_hit_count(self, canonical_key: str) -> None:
        """Bump the hit_count of the artifact at `canonical_key` (D48 `increment`)."""
        ...

    async def list_artifacts(self) -> list[CorpusArtifact]:
        """All landed artifacts (the soft layer embeds their `intent`)."""
        ...


class InMemoryBlueprintCorpus:
    """Dict-backed `BlueprintCorpus` fake — no I/O, deterministic, Layer-1 only."""

    def __init__(self, artifacts: list[CorpusArtifact] | None = None) -> None:
        self._by_key: dict[str, CorpusArtifact] = {}
        for art in artifacts or []:
            self._by_key[art.canonical_key] = art
        # Audit trail for tests: which keys were incremented, in order.
        self.increment_calls: list[str] = []

    async def get_by_canonical_key(self, canonical_key: str) -> CorpusArtifact | None:
        return self._by_key.get(canonical_key)

    async def increment_hit_count(self, canonical_key: str) -> None:
        self.increment_calls.append(canonical_key)
        art = self._by_key.get(canonical_key)
        if art is None:
            # A hit was adjudicated but the artifact vanished (concurrent retire) —
            # a tolerated no-op, never a crash (fail-soft posture, D52).
            return
        self._by_key[canonical_key] = replace(art, hit_count=art.hit_count + 1)

    async def list_artifacts(self) -> list[CorpusArtifact]:
        return list(self._by_key.values())

    # --- Layer-1 inspection / seeding helpers -------------------------------
    def land(self, artifact: CorpusArtifact) -> None:
        """Simulate S9 landing a validated candidate as a corpus artifact."""
        self._by_key[artifact.canonical_key] = artifact

    def get_sync(self, canonical_key: str) -> CorpusArtifact | None:
        return self._by_key.get(canonical_key)
