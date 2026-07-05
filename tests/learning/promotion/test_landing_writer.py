"""Layer-1 — the S9 corpus-landing writer mapping + last-gate entity defense
(S9-activation Slice 2, §3). Infra-free: the candidate→`BlueprintSeed` projection and
the deterministic id are pure; the entity defense RAISES before any neo4j/embed call,
so a fake embedder + a driver that refuses `session()` prove NO land happened.

Slugs: `S9-land-only-validated` (the mapping + deterministic id), and
`S9-land-entity-strip-defense` (an entity in the seed fields → raise, no land).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from data_agent.learning.candidate.models import CandidateStatus
from data_agent.learning.generalize.mapping import blueprint_seed_from_candidate
from data_agent.learning.promotion.landing import (
    CorpusLandingWriter,
    LandingEntityError,
    landing_id,
)
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient

from .helpers import make_blueprint_candidate

KEY = "sha256:single-bp"
_FIXTURE_INTENT = "total earnings for a department in a given year"
_FIXTURE_SQL = (
    "SELECT sum(gross_pay) AS total_earnings FROM payroll.payroll_fact "
    "WHERE department = {department} AND toYear(pay_period) = {year} "
    "AND record_type = 'EARNING' AND region = {region}"
)
_FIXTURE_USES = [
    "payroll.payroll_fact.department",
    "payroll.payroll_fact.gross_pay",
    "payroll.payroll_fact.pay_period",
    "payroll.payroll_fact.record_type",
    "payroll.payroll_fact.region",
]


class _NoSessionDriver:
    """A driver double that RAISES if `session()` is ever opened — proves the entity
    defense fails BEFORE any neo4j write (the writer never touches the store)."""

    def session(self, **_: object) -> object:
        raise AssertionError("neo4j must not be opened when the entity defense raises")


# --- S9-land-only-validated (mapping + deterministic id) -------------------------


def test_seed_maps_generalized_fields_with_deterministic_id() -> None:
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=KEY)

    seed_id = landing_id(env)
    seed = blueprint_seed_from_candidate(env, id=seed_id)

    # The id is DERIVED from the canonical_key (idempotent re-land MERGEs in place).
    assert seed_id == f"bp::{KEY}"
    assert seed.id == seed_id
    # Only generalized, entity-free fields cross into the seed.
    assert seed.intent == _FIXTURE_INTENT
    assert seed.sql_template == _FIXTURE_SQL
    assert seed.uses == _FIXTURE_USES
    assert seed.status == "validated"
    assert seed.slots_summary == "department, year, region"
    assert seed.resolves == {"earnings": "payroll.payroll_fact.gross_pay"}
    # Provenance (review S3): loop-landed, with the originating candidate stamped.
    assert seed.created_by == "learning"
    assert seed.source_candidate_id == env.candidate_id


def test_remap_is_identical_idempotent() -> None:
    """A re-map of the same candidate yields a BYTE-identical seed (same id, same
    fields) — so a re-promotion MERGEs one node, never a dupe."""
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=KEY)

    first = blueprint_seed_from_candidate(env, id=landing_id(env))
    second = blueprint_seed_from_candidate(env, id=landing_id(env))

    assert first == second


def test_landing_id_falls_back_to_candidate_id_without_canonical_key() -> None:
    """A human-approved blueprint that never ran S6 (no canonical_key) still lands
    under a STABLE id derived from the candidate_id (OQ-3), disjoint from the
    sha256-shaped canonical namespace."""
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=None)

    assert landing_id(env) == f"bp::{env.candidate_id}"


# --- S9-land-entity-strip-defense (entity in the seed → raise, no land) ----------


def _entity_bearing_candidate() -> object:
    """A candidate whose intent still carries an entity ("0420") — a STRIP-REGRESSED
    envelope (the strip failed to redact it), used to exercise the last-gate defense."""
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=KEY)
    leaked_payload = dict(env.payload)
    leaked_payload["intent"] = "total earnings for department 0420 in a given year"
    return replace(env, payload=leaked_payload)


async def test_entity_in_seed_raises_and_never_lands() -> None:
    """The last-gate D17 defense: a `forbidden_spans` entry (captured pre-strip by the
    scheduler) that still appears in the seed ⇒ the writer RAISES before any
    embed/neo4j write, so the entity never reaches the global recallable corpus."""
    env = _entity_bearing_candidate()
    embedder = FakeEmbeddingClient()
    writer = CorpusLandingWriter(
        _NoSessionDriver(), embedder, model_id="all-mpnet-base-v2"
    )

    with pytest.raises(LandingEntityError):
        await writer.land(env, forbidden_spans=("0420",))

    # Fail-closed: NO embed, NO neo4j session — the seed never landed.
    assert embedder.calls == []


async def test_no_forbidden_spans_passes_the_defense() -> None:
    """The normal path: a clean (stripped) candidate's PRE-strip spans were empty, so
    the writer is handed `forbidden_spans=()` and the defense is a no-op — the writer
    proceeds to the neo4j write (which this fake driver then rejects, proving control
    reached it past the entity gate)."""
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=KEY)
    embedder = FakeEmbeddingClient()
    writer = CorpusLandingWriter(
        _NoSessionDriver(), embedder, model_id="all-mpnet-base-v2"
    )

    # The defense passes; load_corpus embeds then opens a session — our driver refuses
    # the SESSION (an AssertionError), proving control got past the entity gate.
    with pytest.raises(AssertionError, match="neo4j must not be opened"):
        await writer.land(env)  # forbidden_spans defaults to ()
    assert embedder.calls, "the defense passed → load_corpus embedded before the session"
