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

from data_agent.runtime.retrieval.corpus_loader import BlueprintSeed, load_corpus

from ..candidate.models import CandidateEnvelope
from ..generalize.mapping import blueprint_seed_from_candidate

if TYPE_CHECKING:
    from neo4j import AsyncDriver

    from data_agent.runtime.model.embedding_client import EmbeddingClient

_logger = logging.getLogger(__name__)


class LandingEntityError(Exception):
    """A settled entity span leaked into the generalized landing seed — the last-gate
    D17 defense (§3.3). RAISED so the writer NEVER lands an entity-bearing blueprint
    into the global, recallable corpus; the scheduler then HOLDS `landing_failed`."""


def landing_id(env: CandidateEnvelope) -> str:
    """The DETERMINISTIC neo4j node id for a landed blueprint (§3.2).

    Derived from the S6 `canonical_key` (the dedup identity) so a re-promotion of the
    same canonical blueprint MERGEs the SAME node — idempotent by construction
    (`load_corpus` MERGEs by `id`). Falls back to the `candidate_id` when no
    canonical_key exists (a human-approved blueprint that never ran S6, OQ-3). Both
    forms share the `bp::` prefix; disjointness rests on the SHAPE of the suffix — a
    `sha256:`-shaped canonical key vs. a `candidate::`-shaped candidate id — so the two
    never collide. The fallback has a semantic-dupe window (two candidates for the same
    canonical blueprint that never ran S6 land as two nodes); the writer WARNs on it."""
    key = env.dedup.canonical_key if env.dedup is not None else None
    if key:
        return f"bp::{key}"
    return f"bp::{env.candidate_id}"


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


def _assert_seed_entity_free(
    candidate_id: str, seed: BlueprintSeed, forbidden_spans: tuple[str, ...]
) -> None:
    """Last-gate D17 defense (§3.3): RAISE if any *forbidden_spans* entry appears in
    the generalized seed. `forbidden_spans` are the entity spans S5 identified,
    captured BEFORE `strip_entity_bearing` blanked them (`redaction.entity_spans` on
    the PRE-strip envelope) — so this fires even though a validated candidate's own
    `entity_scan` is blanked. On the normal path the strip already removed every span
    from the payload (⇒ nothing leaks ⇒ no-op); this is the tripwire for a strip
    regression, letting an entity into the global recallable corpus."""
    if not forbidden_spans:
        return
    haystack = _seed_haystack(seed)
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
        seed = blueprint_seed_from_candidate(env, id=seed_id)
        # Last gate BEFORE any embed/neo4j write: an entity in the seed → raise, no land.
        _assert_seed_entity_free(env.candidate_id, seed, forbidden_spans)
        await load_corpus(
            self._driver,
            self._embedder,
            [seed],
            [],
            model_id=self._model_id,
            database=self._database,
            ensure_schema=False,
        )
        _logger.info(
            "landed blueprint %s into the neo4j retrieval corpus (id=%s)",
            env.candidate_id,
            seed.id,
        )


__all__ = ["CorpusLandingWriter", "LandingEntityError", "landing_id"]
