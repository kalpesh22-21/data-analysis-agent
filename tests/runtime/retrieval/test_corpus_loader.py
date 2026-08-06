"""Layer-1 tests for corpus_loader (neo4j-corpus-design §3) — no live neo4j.

Covers the pure, infra-free pieces: write-time model-parity refusal (§3.3), DDL
idempotency (§1.4), the USES-edge derivation, and the hand-authored seed
fixtures round-tripping into seeds with byte-exact `"database.table.column"`
scope keys (§8 highest-risk contract).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from data_agent.runtime.retrieval.corpus_loader import (
    BlueprintSeed,
    CorpusLoadError,
    KnowledgeSeed,
    _use_edges,
    _validate_blueprint_uses,
    check_model_parity,
    load_seed_fixtures,
    schema_statements,
)

# A representative embedding dimension for the DDL-shape assertions (the runtime
# resolves this from config/inference; the exact value is arbitrary here).
_DIM = 768

_FIXTURE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "corpus"


# --------------------------------------------------------------------------
# Write-time model parity (§3.3) — strict at write
# --------------------------------------------------------------------------


def test_check_model_parity_passes_on_empty_or_matching() -> None:
    check_model_parity(set(), "all-mpnet-base-v2")  # fresh index
    check_model_parity({"all-mpnet-base-v2"}, "all-mpnet-base-v2")  # same model


def test_check_model_parity_refuses_a_mixed_model_write() -> None:
    with pytest.raises(CorpusLoadError):
        check_model_parity({"some-other-model"}, "all-mpnet-base-v2")


def test_check_model_parity_empty_stored_stamp_is_a_conflict() -> None:
    # N2: an empty-string stored stamp is a BROKEN row, not a free pass — it
    # conflicts with a real target model and is refused.
    with pytest.raises(CorpusLoadError):
        check_model_parity({""}, "all-mpnet-base-v2")


# --------------------------------------------------------------------------
# DDL idempotency (§1.4)
# --------------------------------------------------------------------------


def test_every_schema_statement_is_idempotent() -> None:
    statements = schema_statements(_DIM)
    assert statements  # non-empty
    for statement in statements:
        assert "IF NOT EXISTS" in statement


def test_vector_indexes_render_the_given_dimension_cosine() -> None:
    for dim in (768, 384, 1536):
        vector_ddl = [s for s in schema_statements(dim) if "VECTOR INDEX" in s]
        assert len(vector_ddl) == 2  # one per corpus (§1.2)
        for ddl in vector_ddl:
            assert f"`vector.dimensions`: {dim}" in ddl
            assert "`vector.similarity_function`: 'cosine'" in ddl
        names = " ".join(vector_ddl)
        assert "blueprint_intent_vec" in names
        assert "knowledge_text_vec" in names


def test_uniqueness_constraints_cover_all_four_keyed_labels() -> None:
    constraints = " ".join(s for s in schema_statements(_DIM) if "CONSTRAINT" in s)
    assert "b.id IS UNIQUE" in constraints
    assert "k.id IS UNIQUE" in constraints
    assert "c.key IS UNIQUE" in constraints
    assert "t.key IS UNIQUE" in constraints


# --------------------------------------------------------------------------
# USES-edge derivation (§1.3)
# --------------------------------------------------------------------------


def test_use_edges_derive_table_key_from_final_dot() -> None:
    edges = _use_edges(["dbpcm_warehouse.payroll.Amount"])
    assert edges == [
        {
            "column_key": "dbpcm_warehouse.payroll.Amount",
            "table_key": "dbpcm_warehouse.payroll",
        }
    ]


def test_use_edges_skip_malformed_keys_without_a_dot() -> None:
    assert _use_edges(["nodotshere"]) == []


# --------------------------------------------------------------------------
# S2 — write-time uses validation (fail-closed with context)
# --------------------------------------------------------------------------


def _seed_with_uses(uses: list) -> BlueprintSeed:
    return BlueprintSeed(id="bp", intent="x", slots_summary="", uses=uses)


def test_validate_uses_accepts_three_part_scope_keys() -> None:
    _validate_blueprint_uses(_seed_with_uses(["a.b.c", "dbpcm_warehouse.payroll.Amount"]))


@pytest.mark.parametrize("bad", ["nodots", "a.b", "a.b.", ".b.c", "a..c", ""])
def test_validate_uses_rejects_malformed_scope_keys(bad: str) -> None:
    with pytest.raises(CorpusLoadError):
        _validate_blueprint_uses(_seed_with_uses([bad]))


def test_validate_uses_rejects_non_str_entry_with_context() -> None:
    with pytest.raises(CorpusLoadError) as exc:
        _validate_blueprint_uses(_seed_with_uses([123]))
    assert "bp" in str(exc.value)  # the offending blueprint id is in the message


# --------------------------------------------------------------------------
# Seed fixtures round-trip (§3.2) — byte-exact HR-warehouse scope keys
# --------------------------------------------------------------------------


def test_load_seed_fixtures_parses_blueprints_and_knowledge() -> None:
    blueprints, knowledge = load_seed_fixtures(_FIXTURE_DIR)
    assert blueprints and knowledge
    assert all(isinstance(b, BlueprintSeed) for b in blueprints)
    assert all(isinstance(k, KnowledgeSeed) for k in knowledge)

    overtime = next(b for b in blueprints if b.id == "bp-overtime-by-department")
    # THE contract: uses are byte-exact `database.table.column` scope keys.
    assert "dbpcm_warehouse.payroll.amount" in overtime.uses
    assert "dbpcm_warehouse.employee.department_name" in overtime.uses


def test_every_fixture_use_key_is_a_three_part_dotted_scope_key() -> None:
    blueprints, _ = load_seed_fixtures(_FIXTURE_DIR)
    for bp in blueprints:
        assert bp.uses, f"blueprint {bp.id} must declare a non-empty uses set"
        for key in bp.uses:
            parts = key.split(".")
            assert len(parts) >= 3, f"{bp.id}: scope key {key!r} is not database.table.column"


def test_knowledge_fixtures_carry_no_uses() -> None:
    _, knowledge = load_seed_fixtures(_FIXTURE_DIR)
    # KnowledgeSeed has no `uses` field at all (entity-agnostic by construction).
    assert not any(hasattr(k, "uses") for k in knowledge)
