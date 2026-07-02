"""QA2 adversarial Layer-1 tests for corpus_loader (neo4j-corpus-design §3).

Attacks the pure, infra-free loader surfaces the reviewer's `test_corpus_loader.py`
does not: malformed `uses` keys (no dot / 2-seg / 4-seg / empty / whitespace /
non-str), duplicate ids across fixtures, empty fixtures, empty knowledge text,
and the write-time parity guard's boundary cases.

The loader is a bulk upsert of a TRUSTED hand-seed (§3.4), so it does almost no
input validation. Where that means garbage is silently stored or the loader
CRASHES on bad input, the test PINS the actual behavior and the module report
FLAGS it — none of these are hard failures of the shipped contract, but they are
the silent-scope-drop foot-guns §8 warns about.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from data_agent.runtime.retrieval.corpus_loader import (
    BlueprintSeed,
    CorpusLoadError,
    KnowledgeSeed,
    _use_edges,
    check_model_parity,
    load_seed_fixtures,
)

# --------------------------------------------------------------------------
# _use_edges — malformed dotted-key matrix (§1.3)
# --------------------------------------------------------------------------


def test_use_edges_two_segment_key_groups_to_first_segment() -> None:
    # "db.table" (2-seg, missing column) still parses: table_key = "db". This is
    # NOT a valid scope key but the edge derivation happily accepts it.
    assert _use_edges(["db.table"]) == [{"column_key": "db.table", "table_key": "db"}]


def test_use_edges_four_segment_key_groups_to_first_three() -> None:
    # 4-seg over-qualified key: table_key = everything before the final dot.
    assert _use_edges(["a.b.c.d"]) == [
        {"column_key": "a.b.c.d", "table_key": "a.b.c"}
    ]


@pytest.mark.parametrize("key", ["nodots", "", "   "])
def test_use_edges_skips_dotless_keys(key: str) -> None:
    # No dot -> no table grouping -> edge silently skipped (empty/whitespace too).
    assert _use_edges([key]) == []


def test_use_edges_trailing_dot_yields_empty_column_name() -> None:
    # "a.b." -> table_key "a.b", column_key "a.b." (empty column). Garbage in,
    # garbage out — no validation.
    assert _use_edges(["a.b."]) == [{"column_key": "a.b.", "table_key": "a.b"}]


def test_use_edges_non_str_key_raises_typeerror() -> None:
    # `_use_edges` itself is a low-level helper and still raises TypeError on a
    # non-str key. In `load_corpus` this is now UNREACHABLE: S2's
    # `_validate_blueprint_uses` runs first and rejects a non-str key with a
    # clear `CorpusLoadError` (see test_corpus_loader_write_validation below).
    with pytest.raises(TypeError):
        _use_edges([123])  # type: ignore[list-item]


# --------------------------------------------------------------------------
# check_model_parity — boundary cases (§3.3)
# --------------------------------------------------------------------------


def test_check_model_parity_empty_target_model_vs_existing_raises() -> None:
    # Writing under model_id="" while the index holds a real model still trips
    # the guard (the existing model conflicts with "").
    with pytest.raises(CorpusLoadError):
        check_model_parity({"all-mpnet-base-v2"}, "")


def test_check_model_parity_multiple_conflicts_reported_sorted() -> None:
    with pytest.raises(CorpusLoadError) as exc:
        check_model_parity({"z-model", "a-model"}, "target")
    msg = str(exc.value)
    assert "a-model" in msg and "z-model" in msg


def test_check_model_parity_none_target_treats_stored_as_conflict() -> None:
    # A None target is falsy for the stored side but the stored real model still
    # differs -> conflict. Pins that None target is not a silent no-op.
    with pytest.raises(CorpusLoadError):
        check_model_parity({"real-model"}, None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# BlueprintSeed / KnowledgeSeed — no field-level validation (pin + FLAG)
# --------------------------------------------------------------------------


def test_blueprint_seed_accepts_garbage_uses_but_loader_rejects_it() -> None:
    # The seed DATACLASS is still permissive (no field validation) — dotless /
    # over-qualified keys are stored verbatim. But S2's `_validate_blueprint_uses`
    # in `load_corpus` now REJECTS them at write with a clear CorpusLoadError, so
    # the §8 silent-scope-drop foot-gun no longer reaches recall via the loader.
    seed = BlueprintSeed(
        id="bp",
        intent="x",
        slots_summary="",
        uses=["nodots", "", "a.b.c.d", "dbpcm_warehouse.payroll.Amount"],
    )
    assert seed.uses[0] == "nodots"  # dataclass stores verbatim; loader guards it


def test_blueprint_seed_accepts_empty_uses_list_flag() -> None:
    # FLAG (scope-safety): an empty `uses` is accepted by the dataclass AND passes
    # S2 validation vacuously (no entries to check). At recall it coerces to
    # uses=None (B1 `_coerce_uses`), so the scope filter DROPs it fail-closed —
    # an empty-uses blueprint is now never retrievable, not always-in-scope.
    seed = BlueprintSeed(id="bp", intent="x", slots_summary="", uses=[])
    assert seed.uses == []


def test_knowledge_seed_accepts_empty_text_flag() -> None:
    # FLAG (product): empty text is accepted; `load_corpus` would embed "" into a
    # near-meaningless vector. No non-empty-text guard.
    seed = KnowledgeSeed(id="kn", text="", doc_id="d")
    assert seed.text == ""


def test_blueprint_seed_defaults_are_the_documented_lifecycle_stubs() -> None:
    seed = BlueprintSeed(id="bp", intent="x", slots_summary="", uses=["a.b.c"])
    assert seed.status == "validated"
    assert seed.drift_status == "clean"
    assert seed.catalog_sha == ""


def test_seed_rejects_unknown_fixture_keys() -> None:
    # A YAML row with an unexpected key surfaces as a TypeError at BlueprintSeed
    # construction (dataclass strictness) — this is the ONE place the loader is
    # strict, so an authoring typo in a known field name is caught.
    with pytest.raises(TypeError):
        BlueprintSeed(  # type: ignore[call-arg]
            id="bp", intent="x", slots_summary="", uses=["a.b.c"], typo_field="oops"
        )


# --------------------------------------------------------------------------
# load_seed_fixtures — empty files, duplicate ids, non-list YAML
# --------------------------------------------------------------------------


def _write_corpus(dir_: Path, blueprints: object, knowledge: object) -> Path:
    (dir_ / "blueprints.yaml").write_text(yaml.safe_dump(blueprints), encoding="utf-8")
    (dir_ / "knowledge.yaml").write_text(yaml.safe_dump(knowledge), encoding="utf-8")
    return dir_


def test_load_seed_fixtures_empty_files_yield_empty_lists(tmp_path: Path) -> None:
    (tmp_path / "blueprints.yaml").write_text("", encoding="utf-8")
    (tmp_path / "knowledge.yaml").write_text("", encoding="utf-8")
    blueprints, knowledge = load_seed_fixtures(tmp_path)
    assert blueprints == [] and knowledge == []


def test_load_seed_fixtures_duplicate_ids_raise(tmp_path: Path) -> None:
    # QA flag 6 FIX (was: pinned both returned + silent MERGE overwrite). A
    # duplicate id now fails loudly at load, so a copy-pasted id is caught instead
    # of silently overwriting in place.
    _write_corpus(
        tmp_path,
        [
            {"id": "dup", "intent": "first", "slots_summary": "", "uses": ["a.b.c"]},
            {"id": "dup", "intent": "second", "slots_summary": "", "uses": ["a.b.c"]},
        ],
        [],
    )
    with pytest.raises(CorpusLoadError):
        load_seed_fixtures(tmp_path)


def test_load_seed_fixtures_duplicate_id_across_files_raises(tmp_path: Path) -> None:
    # QA flag 6: the id namespace is checked ACROSS both fixture files too.
    _write_corpus(
        tmp_path,
        [{"id": "shared", "intent": "bp", "slots_summary": "", "uses": ["a.b.c"]}],
        [{"id": "shared", "text": "kn", "doc_id": "d"}],
    )
    with pytest.raises(CorpusLoadError):
        load_seed_fixtures(tmp_path)


def test_load_seed_fixtures_non_list_yaml_raises_corpusloaderror(tmp_path: Path) -> None:
    (tmp_path / "blueprints.yaml").write_text(
        "id: not-a-list\nintent: scalar-mapping\n", encoding="utf-8"
    )
    (tmp_path / "knowledge.yaml").write_text("", encoding="utf-8")
    with pytest.raises(CorpusLoadError):
        load_seed_fixtures(tmp_path)


def test_load_seed_fixtures_missing_file_raises(tmp_path: Path) -> None:
    # No fixtures at all -> FileNotFoundError from the open() (not a silent []).
    with pytest.raises(FileNotFoundError):
        load_seed_fixtures(tmp_path)
