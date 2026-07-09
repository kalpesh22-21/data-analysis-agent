"""promotion/landing.py — the S9 corpus-landing writer (S9-activation Slice 2, §3).

The learning loop's FIRST write into what gets RECALLED. On `candidate → validated`,
`CorpusLandingWriter.land` MERGE-upserts the validated blueprint into the neo4j
retrieval corpus (via the reused `runtime/retrieval/corpus_loader.load_corpus`) so it
becomes recallable end-to-end. The safety guards here are load-bearing:

  * **Deterministic id (idempotent MERGE, §3.2).** The seed's neo4j `id` is derived
    from the candidate's `canonical_key` (the S6 dedup identity), so a re-promotion of
    the same canonical blueprint MERGEs the SAME node in place — never a duplicate.
    A human-approved blueprint that never ran S6 (no canonical_key) falls back to a
    stable `candidate_id`-derived id (OQ-3).

  * **Entity-strip DEFENSE (D17, §3.3).** The candidate is already stripped at
    validation, but the landing writer is the LAST gate before a GLOBAL, recallable
    write. Before any embed/neo4j write, `land` asserts NO settled entity span leaked
    into the generalized seed fields; if one is detected it RAISES (never lands).

  * **Model-parity (§3.3).** `load_corpus` stamps `model_id` and refuses to mix
    embedding models into one index (`check_model_parity`, the first statement of the
    write txn) — a mismatch raises `CorpusLoadError`, which the scheduler catches and
    HOLDS `landing_failed` (never a mixed-model index).

  * **Fail-closed by RAISING.** Any failure — the entity defense, a malformed seed, a
    parity violation, an embed error, or a neo4j write error — RAISES. The scheduler's
    `_land_and_promote` catches it and HOLDS, leaving the candidate un-promoted for an
    idempotent retry next cycle.

The neo4j async driver + the real embedding client are INJECTED (the same D71 endpoint
the online recall path embeds against — parity by construction), so Layer-1 tests drive
the mapping + defense with a fake driver/embedder (no real network) and Layer-2 lands
into real neo4j and recalls it back.
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
    """A settled entity span leaked into the generalized landing seed — the last-gate
    D17 defense (§3.3). RAISED so the writer NEVER lands an entity-bearing blueprint
    into the global, recallable corpus; the scheduler then HOLDS `landing_failed`."""


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

    Derived from the S6 `canonical_key` (the dedup identity) so a re-promotion of the
    same canonical artifact MERGEs the SAME node — idempotent by construction
    (`load_corpus` MERGEs by `id`). Falls back to the `candidate_id` when no
    canonical_key exists (a human-approved artifact that never ran S6, OQ-3). The
    prefix is TYPE-DRIVEN — `bp::` for a blueprint, `kn::` for global_knowledge — so a
    blueprint and a knowledge node can NEVER collide on a shared canonical_key/
    candidate_id suffix (they MERGE into disjoint id namespaces). Disjointness within a
    type rests on the SHAPE of the suffix — a `sha256:`-shaped canonical key vs. a
    `candidate::`-shaped candidate id. The fallback has a semantic-dupe window (two
    candidates for the same canonical artifact that never ran S6 land as two nodes);
    the writer WARNs on it. An unknown type defaults to `bp::` (baseline, unchanged)."""
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


def _seed_haystack(seed: BlueprintSeed) -> str:
    """Every text-bearing generalized field of the seed, concatenated for the entity
    scan. Covers the natural-language `intent`, the SQL templates, the slot/resolve/
    compose/grain structures, and the `uses`/`uses_rules` — the full surface a global
    write would expose."""
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
    """Every text-bearing field of a knowledge seed, concatenated for the entity scan
    — the knowledge-side sibling of `_seed_haystack`. Covers the `text` (statement +
    related terms + serialized `structured`) and the `title`, the full surface a global
    knowledge write would expose."""
    return "\n".join([seed.text, seed.title or ""])


def _assert_seed_entity_free(
    candidate_id: str,
    seed: BlueprintSeed | KnowledgeSeed,
    forbidden_spans: tuple[str, ...],
) -> None:
    """Last-gate D17 defense (§3.3): RAISE if any *forbidden_spans* entry appears in
    the landed seed. `forbidden_spans` are the entity spans S5 identified, captured
    BEFORE `strip_entity_bearing` blanked them (`redaction.entity_spans` on the
    PRE-strip envelope) — so this fires even though a validated candidate's own
    `entity_scan` is blanked. On the normal path the strip already removed every span
    from the payload (⇒ nothing leaks ⇒ no-op); this is the tripwire for a strip
    regression, letting an entity into the global recallable corpus. The haystack is
    routed by seed TYPE — the generalized blueprint fields or the knowledge text/title."""
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
    """The real `LandingWriter` (S9 §3.4). Wraps `load_corpus` over an INJECTED neo4j
    driver + embedding client, stamping `model_id` for read-path parity.

    `ensure_schema=False`: the neo4j corpus schema (constraints + native vector
    indexes) is provisioned by the seed load (`scripts/seed_neo4j_corpus.py`); the
    scheduler is a WRITER, not a provisioner (§3.3)."""

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
        self, env: CandidateEnvelope, *, forbidden_spans: tuple[str, ...] = ()
    ) -> None:
        """Materialize *env* into the neo4j retrieval corpus (idempotent MERGE).

        Order: map → entity-defense → embed + MERGE. *forbidden_spans* are the entity
        spans S5 identified, captured by the caller BEFORE the strip (the scheduler
        passes `redaction.entity_spans(pre_strip_env)`); the last-gate defense RAISES if
        any survives into the seed. Any failure RAISES (the entity defense, a malformed
        seed, a model-parity violation, an embed/neo4j error) so the scheduler HOLDS
        `landing_failed` and never writes `validated` (§3.1)."""
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
            seed = knowledge_seed_from_candidate(env, id=seed_id)
            knowledge_seeds = [seed]
        else:
            seed = blueprint_seed_from_candidate(env, id=seed_id)
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
        """Stamp the landed node's recall-eligibility (`status` + `drift_status`),
        keyed by the SAME deterministic `landing_id` (S9-activation Slice 3, §8.6).

        This is the retraction write-back the learning loop's demote/reject/user-
        correction/retract edges use to make a demoted/broken/leaked blueprint
        un-recallable (the recall filter reads exactly these two properties), AND the
        per-cycle RE-ASSERT that converges a transiently-failed write-back — a demoted
        blueprint re-stamped ineligible in the candidate scan, a still-validated one
        re-stamped `validated`/`clean` in the clean rescan.

        IDEMPOTENT + safe no-op: MATCH-by-id, so a node that was never landed (or
        already removed) matches nothing and NO write happens — retracting a
        never-landed / already-retracted node is harmless. Returns True iff a node was
        actually stamped (False = no landed node), for the caller's observability log.

        NO embed, NO model-parity, NO entity defense: this only mutates two lifecycle
        scalars on an EXISTING node — it never (re)writes the intent/embedding/seed
        payload, so none of the landing write's global-exposure gates apply. RAISES on
        a driver/query failure; the scheduler catches it and FAILS OPEN (the store
        transition is source-of-truth and must never be blocked by a corpus-write
        failure — the recall filter + the periodic re-assert are the backstops)."""
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


__all__ = ["CorpusLandingWriter", "LandingEntityError", "landing_id"]
