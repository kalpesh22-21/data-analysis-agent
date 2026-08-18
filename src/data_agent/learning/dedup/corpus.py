"""BlueprintCorpus — the landed-artifact port the S6 hard key looks up (D48).

Artifacts are keyed by `canonical_key`. `get_by_canonical_key` answers the hard-key duplicate
question and is deliberately NOT status-filtered (see `CorpusArtifact.is_terminal` for why
the hard and soft layers differ); `increment_hit_count` bumps the cross-session count on the
ARTIFACT, not the envelope (§11.1); `increment_recurrence_count` bumps the dormant paraphrase
counter; `set_status` lets the S9 TERMINAL transitions stamp an artifact dead so it stops
surfacing as live prior art. `InMemoryBlueprintCorpus` is the Layer-1 fake.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Protocol

from ..priorart.models import TERMINAL_STATUSES as TERMINAL_ARTIFACT_STATUSES


@dataclass(frozen=True)
class CorpusArtifact:
    """A landed blueprint artifact, as far as S6 dedup needs to see it."""

    id: str
    canonical_key: str
    intent: str
    hit_count: int = 1
    uses_rules: tuple[str, ...] = ()
    # --- Lifecycle/provenance. Added as schema-only in PriorArtIndex Slice 1; WIRED in
    # Slice 2. Before that an artifact was write-once — S6 seeded it at first sighting
    # and incremented it forever — so a candidate the scheduler REJECTED left an
    # artifact indistinguishable from a live one, still accruing hits and still
    # surfacing as prior art. The scheduler's terminal transitions (reject/retire) now
    # stamp `status` through `BlueprintCorpus.set_status`.
    # `source` is the tier the artifact came from (learning staging vs. MCP canon).
    # Both default to the values every pre-existing seeded artifact implicitly had.
    status: str = "extracted"
    source: str = "learning"
    # --- the SOFT recurrence counter (plan §4). A sibling of `hit_count`, deliberately
    # NOT a replacement for it, and it counts a different event:
    #
    #   hit_count        — a session minted the BYTE-IDENTICAL canonical key. Exact
    #                      normalized-AST equality over four inputs; race-safe; the input
    #                      to the promotion corroboration gate today.
    #   recurrence_count — a session's INTENT came within
    #                      `recurrence_similarity_threshold` cosine of this artifact's
    #                      intent without producing its key. "Somebody asked this again,
    #                      in different SQL."
    #
    # The hard count is the reason nothing has ever been corroborated: two analysts
    # asking one business question through slightly different SQL mint different keys and
    # never see each other. This counter is the loose version of the same evidence.
    #
    # It is DORMANT at `recurrence_weight = 0.0` (the shipped default) and is being
    # accrued anyway, on purpose: a counter that starts counting the day someone decides
    # to use it has no history behind it, so its first month of readings are all zeros
    # and indistinguishable from "this never recurs".
    recurrence_count: int = 0

    @property
    def is_terminal(self) -> bool:
        """True iff a human killed this artifact (rejected/retired).

        Mirrors `priorart.models.TERMINAL_STATUSES` — the corpus bucket and the graph must agree
        about what "dead prior art" means, and a parity test pins the two lists together. THE TWO
        DEDUP LAYERS TREAT THIS DIFFERENTLY ON PURPOSE: the SOFT layer SKIPS a terminal artifact,
        because a near-match to a rejected idea is not the same idea and resurfacing it would turn a
        settled decision into recurring noise; the HARD layer does NOT, because a byte-identical
        canonical key IS the same idea the human said no to. `DedupStage.process` tags that
        increment with `matched_status` so the two stay separable in telemetry.
        """
        return self.status in TERMINAL_ARTIFACT_STATUSES

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> CorpusArtifact:
        """Rehydrate a persisted doc.

        `status`/`source` are read with the SAME defaults as the dataclass, so a doc written before
        those keys existed loads as the live-learning artifact it has always been. The corpus bucket
        is durable and is never migrated, so tolerating the older shape is required, not polite.
        """
        return cls(
            id=doc["id"],
            canonical_key=doc["canonical_key"],
            intent=doc.get("intent", ""),
            hit_count=int(doc.get("hit_count", 1)),
            uses_rules=tuple(doc.get("uses_rules", []) or []),
            status=str(doc.get("status") or "extracted"),
            source=str(doc.get("source") or "learning"),
            # Every doc written before plan §4 lacks this key entirely, and 0 is the
            # honest reading: no soft recurrence has been recorded for it.
            recurrence_count=int(doc.get("recurrence_count") or 0),
        )


class BlueprintCorpus(Protocol):
    """The landed-artifact lookup the S6 dedup stage depends on."""

    async def get_by_canonical_key(self, canonical_key: str) -> CorpusArtifact | None:
        """The artifact with this exact hard key, or `None` (no hard-key collision)."""
        ...

    async def seed_artifact(self, artifact: CorpusArtifact) -> None:
        """Register a NEW artifact at `hit_count=1` on its FIRST sighting (D48 §11.1).

        Called on `action="insert"` so the count-based promotion threshold can accrue from the
        candidate stage — the artifact must exist BEFORE it lands, or the count could never
        converge. Idempotent: a no-op if an artifact already exists at the key.
        """
        ...

    async def increment_hit_count(self, canonical_key: str) -> None:
        """Bump the hit_count of the artifact at `canonical_key` (D48 `increment`)."""
        ...

    async def increment_recurrence_count(self, canonical_key: str) -> None:
        """Bump the SOFT recurrence counter of the artifact at `canonical_key` (plan §4).

        A paraphrase sighting, as opposed to `increment_hit_count`'s byte-identical one; a vanished
        artifact is a tolerated no-op either way. Deliberately its OWN method rather than a flag:
        the two counts feed the SAME gate at different weights, and one call site that could write
        either would be one place to conflate them.
        """
        ...

    async def set_status(self, canonical_key: str, status: str) -> None:
        """Stamp the artifact's lifecycle `status` (PriorArtIndex Slice 2).

        Written by the S9 scheduler's TERMINAL transitions only (reject, retract). A NARROW write on
        that one field: it must not disturb `hit_count` (a full upsert would reset an accrued
        counter) and must not RESURRECT a vanished artifact, so a missing key is a tolerated no-op.
        Without it the corpus has no concept of a dead artifact, and a rejected candidate's kept
        surfacing as live prior art forever.
        """
        ...

    async def list_artifacts(self) -> list[CorpusArtifact]:
        """All landed artifacts (the fallback soft layer embeds their `intent`)."""
        ...


class InMemoryBlueprintCorpus:
    """Dict-backed `BlueprintCorpus` fake — no I/O, deterministic, Layer-1 only."""

    def __init__(self, artifacts: list[CorpusArtifact] | None = None) -> None:
        self._by_key: dict[str, CorpusArtifact] = {}
        for art in artifacts or []:
            self._by_key[art.canonical_key] = art
        # Audit trail for tests: which keys were incremented / seeded, in order.
        self.increment_calls: list[str] = []
        self.recurrence_calls: list[str] = []
        self.seed_calls: list[str] = []
        self.status_calls: list[tuple[str, str]] = []

    async def get_by_canonical_key(self, canonical_key: str) -> CorpusArtifact | None:
        return self._by_key.get(canonical_key)

    async def seed_artifact(self, artifact: CorpusArtifact) -> None:
        # First sighting: register at hit_count=1. Idempotent — never clobber an
        # already-present artifact (a concurrent seed / an existing landed row).
        if artifact.canonical_key in self._by_key:
            return
        self.seed_calls.append(artifact.canonical_key)
        self._by_key[artifact.canonical_key] = artifact

    async def increment_hit_count(self, canonical_key: str) -> None:
        self.increment_calls.append(canonical_key)
        art = self._by_key.get(canonical_key)
        if art is None:
            # A hit was adjudicated but the artifact vanished (concurrent retire) —
            # a tolerated no-op, never a crash (fail-soft posture, D52).
            return
        self._by_key[canonical_key] = replace(art, hit_count=art.hit_count + 1)

    async def increment_recurrence_count(self, canonical_key: str) -> None:
        self.recurrence_calls.append(canonical_key)
        art = self._by_key.get(canonical_key)
        if art is None:
            return  # a vanished artifact — tolerated no-op, mirroring hit_count
        self._by_key[canonical_key] = replace(
            art, recurrence_count=art.recurrence_count + 1
        )

    async def set_status(self, canonical_key: str, status: str) -> None:
        self.status_calls.append((canonical_key, status))
        art = self._by_key.get(canonical_key)
        if art is None:
            # Never resurrect a vanished artifact (parity with the durable store's
            # sub-document REPLACE semantics) — a tolerated no-op, never a create.
            return
        self._by_key[canonical_key] = replace(art, status=status)

    async def list_artifacts(self) -> list[CorpusArtifact]:
        return list(self._by_key.values())

    # --- Layer-1 inspection / seeding helpers -------------------------------
    def land(self, artifact: CorpusArtifact) -> None:
        """Simulate S9 landing a validated candidate as a corpus artifact."""
        self._by_key[artifact.canonical_key] = artifact

    def get_sync(self, canonical_key: str) -> CorpusArtifact | None:
        return self._by_key.get(canonical_key)
