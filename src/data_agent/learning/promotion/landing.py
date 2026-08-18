"""promotion/landing.py — the S9 corpus-landing writer (S9-activation Slice 2, §3).

On `candidate → validated`, `CorpusLandingWriter.land` MERGE-upserts the validated blueprint
via the reused `runtime/retrieval/corpus_loader.load_corpus`. GOVERNED CORPUS (Phase 2): the
node lands in the `source='learning'` STAGING tier, and agent recall serves ONLY
`source='mcp'`, so a freshly-landed node is NEVER recallable end-to-end —
`status`/`drift_status` govern recall eligibility only WITHIN the mcp partition.

Load-bearing guards: a DETERMINISTIC id derived from the S6 `canonical_key`, so a
re-promotion MERGEs the SAME node; the ENTITY-STRIP defense, this being the LAST gate before
a global recallable write; MODEL-PARITY, which refuses to mix embedding models into one
index; and FAIL-CLOSED BY RAISING — every failure raises, so the scheduler HOLDS and retries
idempotently next cycle. The neo4j driver and embedding client are INJECTED (the same D71
endpoint the online recall path embeds against — parity by construction).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from data_agent.runtime.retrieval.corpus_loader import (
    BlueprintSeed,
    KnowledgeSeed,
    load_corpus,
)

from ..candidate.models import CandidateEnvelope
from ..generalize.mapping import (
    blueprint_seed_from_candidate,
    knowledge_seed_from_candidate,
)

if TYPE_CHECKING:
    from neo4j import AsyncDriver

    from data_agent.runtime.model.embedding_client import EmbeddingClient

_logger = logging.getLogger(__name__)


class LandingEntityError(Exception):
    """A settled entity span leaked into the generalized landing seed — the last-gate D17 defense.

    RAISED so the writer NEVER lands an entity-bearing blueprint into the global, recallable
    corpus; the scheduler then HOLDS `landing_failed`.
    """


# Per-type landing-id prefix (UI Slice 2 §1.1 row 4). A blueprint and a
# global_knowledge chunk could otherwise derive the SAME suffix (a shared
# canonical_key/candidate_id) and collide on one node label vs. the other — the
# TYPE-DRIVEN prefix keeps the two id namespaces disjoint so each MERGEs its own node.
_LANDING_PREFIX: dict[str, str] = {
    "blueprint": "bp::",
    "global_knowledge": "kn::",
}


def landing_id(env: CandidateEnvelope) -> str:
    """The DETERMINISTIC neo4j node id for a landed artifact (§3.2).

    Derived from the S6 `canonical_key` so a re-promotion of the same canonical artifact MERGEs
    the SAME node, falling back to the `candidate_id` when there is no canonical_key (a
    human-approved artifact that never ran S6, OQ-3). The prefix is TYPE-DRIVEN — `bp::` for a
    blueprint, `kn::` for global_knowledge — so the two can NEVER collide on a shared suffix.
    The fallback has a semantic-dupe window (two candidates for the same artifact that never ran
    S6 land as two nodes); the writer WARNs on it.
    """
    prefix = _LANDING_PREFIX.get(env.type, "bp::")
    key = env.dedup.canonical_key if env.dedup is not None else None
    if key:
        return f"{prefix}{key}"
    return f"{prefix}{env.candidate_id}"


# S9-activation Slice 3 — the retraction write-back (design §8.6). MATCH-by-id so a
# node that was never landed (or already removed) matches nothing → the SET runs zero
# times → a safe no-op (idempotent). `RETURN b.id` lets the writer report whether a
# node was actually stamped (observability; a miss is logged, never raised). The recall
# filter (`vector_index._BLUEPRINT_RECALL_QUERY`) reads these two properties, so
# flipping either `status` off `validated` OR `drift_status` to `suspect` makes the
# blueprint un-recallable.
_RETRACT_BLUEPRINT = """
MATCH (b:Blueprint {id: $id})
SET b.status = $status, b.drift_status = $drift_status
RETURN b.id AS id
"""

# The knowledge-side retraction sibling (UI Slice 2 §1.1 row 4). MATCH-by-id so a
# never-landed / already-removed node matches nothing → a safe idempotent no-op. The
# knowledge recall filter (`vector_index._KNOWLEDGE_RECALL_QUERY`) reads `status`, so
# flipping it off `validated` makes the chunk un-recallable. `drift_status` is stamped
# for parity (not read by knowledge recall). `RETURN k.id` reports whether a node was
# actually stamped (observability; a miss is logged, never raised).
_RETRACT_KNOWLEDGE = """
MATCH (k:KnowledgeChunk {id: $id})
SET k.status = $status, k.drift_status = $drift_status
RETURN k.id AS id
"""

# Phase-3 verify write-back (the inbox VERIFY action). MATCH-by-id so a never-landed /
# already-removed node matches nothing → a safe idempotent no-op. Flips ONLY the
# `verified` flag true on the landed learning node (`source` stays `learning` — verify
# does not move the node into the trusted MCP recall partition; the manual-PR reseed
# does). `$label` is interpolated from a FIXED per-type allow-list (never user input),
# so there is no injection surface. `RETURN` reports whether a node was stamped.
_MARK_VERIFIED = """
MATCH (b:{label} {{id: $id}})
SET b.verified = true
RETURN b.id AS id
"""

# The neo4j label per artifact type — a blueprint lands as `:Blueprint`, a
# global_knowledge chunk as `:KnowledgeChunk`. Fixed allow-list (never user input) so
# the `_MARK_VERIFIED` label interpolation carries no injection surface.
_VERIFY_LABEL: dict[str, str] = {
    "global_knowledge": "KnowledgeChunk",
}


def _seed_haystack(seed: BlueprintSeed) -> str:
    """Every text-bearing generalized field of the seed, concatenated for the entity scan.

    The natural-language `intent`, the SQL templates, the slot/resolve/compose/grain structures
    and `uses`/`uses_rules` — the full surface a global write would expose.
    """
    return "\n".join(
        [
            seed.intent,
            seed.slots_summary,
            seed.sql_template or "",
            json.dumps(seed.resolves, default=str),
            json.dumps(seed.slots, default=str),
            json.dumps(seed.composes, default=str),
            json.dumps(seed.uses, default=str),
            json.dumps(seed.uses_rules, default=str),
            json.dumps(seed.result_grain, default=str),
        ]
    )


def _knowledge_haystack(seed: KnowledgeSeed) -> str:
    """Every text-bearing field of a knowledge seed, concatenated for the entity scan.

    The knowledge-side sibling of `_seed_haystack`: the `text` (statement + related terms +
    serialized `structured`) and the `title`.
    """
    return "\n".join([seed.text, seed.title or ""])


def _assert_seed_entity_free(
    candidate_id: str,
    seed: BlueprintSeed | KnowledgeSeed,
    forbidden_spans: tuple[str, ...],
) -> None:
    """Last-gate D17 defense (§3.3): RAISE if any *forbidden_spans* entry appears in the seed.

    `forbidden_spans` are the entity spans S5 identified, captured BEFORE `strip_entity_bearing`
    blanked them, so this fires even though a validated candidate's own `entity_scan` is
    blanked. On the normal path the strip already removed every span, so this is the tripwire
    for a strip REGRESSION. The haystack is routed by seed TYPE.
    """
    if not forbidden_spans:
        return
    haystack = (
        _knowledge_haystack(seed)
        if isinstance(seed, KnowledgeSeed)
        else _seed_haystack(seed)
    )
    leaked = sorted({span for span in forbidden_spans if span in haystack})
    if leaked:
        raise LandingEntityError(
            f"refusing to land candidate {candidate_id}: entity content leaked into "
            f"the generalized seed ({len(leaked)} span(s)) — the entity strip did not "
            "remove it before the landing writer (D17 last gate)"
        )


class CorpusLandingWriter:
    """The real `LandingWriter` (S9 §3.4), wrapping `load_corpus` over an INJECTED driver.

    Stamps `model_id` for read-path parity. `ensure_schema=False`: the neo4j corpus schema is
    provisioned by the seed load, and the scheduler is a WRITER, not a provisioner.
    """

    def __init__(
        self,
        driver: AsyncDriver,
        embedding_client: EmbeddingClient,
        *,
        model_id: str,
        database: str = "neo4j",
    ) -> None:
        self._driver = driver
        self._embedder = embedding_client
        self._model_id = model_id
        self._database = database

    async def land(
        self,
        env: CandidateEnvelope,
        *,
        forbidden_spans: tuple[str, ...] = (),
        verified: bool = False,
    ) -> None:
        """Materialize *env* into the neo4j retrieval corpus (idempotent MERGE).

        Order: map → entity-defense → embed + MERGE. *forbidden_spans* are the spans S5 identified,
        captured by the caller BEFORE the strip; the last-gate defense RAISES if any survives into
        the seed. *verified* is the Phase-3 approval flag stamped onto the seed — `source` stays
        `"learning"` either way. Any failure RAISES, so the scheduler HOLDS `landing_failed` and
        never writes `validated` (§3.1).
        """
        seed_id = landing_id(env)
        if not (env.dedup is not None and env.dedup.canonical_key):
            # OQ-3 fallback: no canonical_key ⇒ a `candidate_id`-derived id. Two
            # candidates for the same canonical blueprint that never ran S6 would land
            # as TWO nodes (a semantic-dupe window) — WARN so it is observable.
            _logger.warning(
                "landing candidate %s under the candidate_id fallback id %s (no "
                "canonical_key; OQ-3 semantic-dupe window)",
                env.candidate_id,
                seed_id,
            )
        # Branch on the artifact type (UI Slice 2 §1.1 row 3): a blueprint lands as a
        # `:Blueprint` (generalized seed), a global_knowledge chunk lands as a
        # `:KnowledgeChunk` (entity-free text seed). Each populates ONLY its own
        # `load_corpus` list; the other stays empty.
        blueprint_seeds: list[BlueprintSeed] = []
        knowledge_seeds: list[KnowledgeSeed] = []
        seed: BlueprintSeed | KnowledgeSeed
        if env.type == "global_knowledge":
            seed = knowledge_seed_from_candidate(env, id=seed_id, verified=verified)
            knowledge_seeds = [seed]
        else:
            seed = blueprint_seed_from_candidate(env, id=seed_id, verified=verified)
            blueprint_seeds = [seed]
        # Last gate BEFORE any embed/neo4j write: an entity in the seed → raise, no land.
        _assert_seed_entity_free(env.candidate_id, seed, forbidden_spans)
        await load_corpus(
            self._driver,
            self._embedder,
            blueprint_seeds,
            knowledge_seeds,
            model_id=self._model_id,
            database=self._database,
            ensure_schema=False,
        )
        _logger.info(
            "landed %s %s into the neo4j retrieval corpus (id=%s)",
            env.type,
            env.candidate_id,
            seed.id,
        )

    async def update_status(
        self, env: CandidateEnvelope, *, status: str, drift_status: str
    ) -> bool:
        """Stamp the landed node's recall-eligibility (`status` + `drift_status`) by `landing_id`.

        The retraction write-back the demote/reject/user-correction/retract edges use to make a
        blueprint un-recallable (the recall filter reads exactly these two properties), AND the
        per-cycle RE-ASSERT that converges a transiently-failed write-back. IDEMPOTENT MATCH-by-id,
        so a node that was never landed matches nothing and NO write happens; returns True iff a
        node was actually stamped. NO embed, model-parity or entity defense — it mutates two
        lifecycle scalars on an EXISTING node and never rewrites the seed payload, so none of the
        landing write's global-exposure gates apply. RAISES on a driver failure; the scheduler
        catches it and FAILS OPEN.
        """
        seed_id = landing_id(env)
        # Dispatch the retraction Cypher by artifact type (UI Slice 2 §1.1 row 4): a
        # knowledge chunk stamps `:KnowledgeChunk`, everything else `:Blueprint`.
        query = (
            _RETRACT_KNOWLEDGE if env.type == "global_knowledge" else _RETRACT_BLUEPRINT
        )
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                query,
                id=seed_id,
                status=status,
                drift_status=drift_status,
            )
            rows = await result.data()
        stamped = bool(rows)
        if stamped:
            _logger.info(
                "corpus status write-back: %s %s (id=%s) -> status=%s drift_status=%s",
                env.type,
                env.candidate_id,
                seed_id,
                status,
                drift_status,
            )
        else:
            _logger.info(
                "corpus status write-back no-op: %s %s not landed (id=%s)",
                env.type,
                env.candidate_id,
                seed_id,
            )
        return stamped

    async def mark_verified(self, env: CandidateEnvelope) -> bool:
        """Flip the landed node's `verified` flag true (the Phase-3 VERIFY action), by `landing_id`.

        IDEMPOTENT MATCH-by-id, returning True iff a node was actually stamped. No embed,
        model-parity or entity defense — one lifecycle scalar on an EXISTING node, never rewriting
        the seed payload. `source` stays `"learning"`: verify does NOT move the node into the
        trusted MCP recall partition (that is the manual-PR reseed the promote action emits). RAISES
        on a driver failure; the scheduler catches it and FAILS OPEN.
        """
        seed_id = landing_id(env)
        label = _VERIFY_LABEL.get(env.type, "Blueprint")
        query = _MARK_VERIFIED.format(label=label)
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, id=seed_id)
            rows = await result.data()
        stamped = bool(rows)
        _logger.info(
            "corpus verify write-back%s: %s %s (id=%s)",
            "" if stamped else " no-op (not landed)",
            env.type,
            env.candidate_id,
            seed_id,
        )
        return stamped


__all__ = ["CorpusLandingWriter", "LandingEntityError", "landing_id"]
