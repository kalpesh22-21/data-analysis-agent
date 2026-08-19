"""The seeded corpus must yield DETERMINED `searchBlueprints` provenance.

`tools._cards_to_provenance` claims the union of the returned cards' `uses` as the
trail entry's D44 provenance, and fails closed to `None` when a card PRINTS a
column identifier that union does not cover (release-1 §02 ⚠Provenance, and the
QA residue `tests/runtime/context/test_release1_replay_qa.py` measured).

`None` is not a soft failure here. `context/scope_filter.is_provenance_in_scope`
drops an undetermined entry ALWAYS — under allow-all, and inside its own turn once
the entry is `ok` — so a card that fails the guard is deleted from the model's
context one round-trip after it asked for it, silently, with the search looking
perfectly successful. A guard that is even slightly too strict therefore disables
blueprint-first routing rather than protecting it.

So the real corpus is the acceptance test for the guard's strictness, and this file
runs the WHOLE chain the runtime runs — `blueprints.yaml` → the `*_json` node props
the loader writes → `map_blueprint_record` → the pipeline card projection →
`_search_card` → provenance — against every seed, with no infra. It fails in both
directions: a corpus edit that prints an uncovered column (a `resolves` value
naming a column outside `uses`) fails closed and is caught here, and a guard change
that starts rejecting ordinary seeds is caught here too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context import scope_filter
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
from data_agent.runtime.retrieval.corpus_loader import BlueprintSeed, load_seed_fixtures
from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
from data_agent.runtime.retrieval.tools import SearchBlueprintsTool
from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex, map_blueprint_record

_REPO = Path(__file__).resolve().parents[3]
_FIXTURE_DIR = _REPO / "tests" / "fixtures" / "corpus"
_Q = "salary by department"
_QVEC = [1.0, 0.0]


def _seeds() -> list[BlueprintSeed]:
    blueprints, _knowledge = load_seed_fixtures(_FIXTURE_DIR)
    return blueprints


def _recall_record(seed: BlueprintSeed) -> dict[str, Any]:
    """One seed as the RECALL ROW neo4j returns for it.

    The three `*_json` props mirror `compiler.dag_properties` exactly —
    `json.dumps(value) if value else None`, and `result_grain` encoded whenever it
    is not `None` (an authored `[]` is stored, not dropped). Going through the real
    `map_blueprint_record` from here means the decode/type-coercion path is the
    runtime's, not a test's re-implementation.
    """
    return {
        "id": seed.id,
        "text": seed.intent,
        "slots_summary": seed.slots_summary,
        "uses": list(seed.uses),
        # Recall's own similarity; the fake index overwrites it per query anyway.
        "score": 1.0,
        "resolves_json": json.dumps(seed.resolves) if seed.resolves else None,
        "slots_json": json.dumps(seed.slots) if seed.slots else None,
        "result_grain_json": (
            json.dumps(seed.result_grain) if seed.result_grain is not None else None
        ),
    }


def _tool(*records: dict[str, Any]) -> SearchBlueprintsTool:
    index = FakeVectorIndex(
        [(map_blueprint_record(record), _QVEC) for record in records]
    )
    return SearchBlueprintsTool(
        pipeline=RetrievalPipeline(
            embedding_client=FakeEmbeddingClient({_Q: _QVEC}),
            reranker=None,
            vector_index=index,
            user_memory=NullUserMemoryProvider(),
            recall_k=50,
            top_k_blueprints=50,
            top_k_knowledge=5,
        ),
        default_k=20,
        max_k=20,
    )


def _creds(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s1", jwt="jwt", column_scope=scope)


async def _provenance(*records: dict[str, Any]) -> Any:
    result = await _tool(*records).run({"query": _Q, "k": 20}, _creds())
    assert result.status == "ok"
    assert result.result_full["count"] == len(records)
    return result.provenance


async def test_every_seeded_blueprint_yields_determined_provenance() -> None:
    """The load-bearing one. Each seed searched on its own, so one bad seed is
    named rather than hidden behind another's footprint."""
    undetermined = []
    for seed in _seeds():
        if await _provenance(_recall_record(seed)) is None:
            undetermined.append(seed.id)
    assert undetermined == [], (
        "these seeded blueprints print a column identifier their own `uses` "
        "footprint does not cover, so their searchBlueprints entry is dropped from "
        "D44 replay entirely — the model would lose its own search result on the "
        "next round-trip"
    )


async def test_the_whole_corpus_in_one_result_is_determined_and_is_the_uses_union() -> None:
    """The real shape: `k` cards in one entry. The claimed provenance is exactly
    the union of the seeds' `uses` — no more (which would over-restrict replay),
    no less (which would under-cover what the cards print)."""
    seeds = _seeds()
    provenance = await _provenance(*(_recall_record(seed) for seed in seeds))
    expected = {
        (use.rpartition(".")[0], use.rpartition(".")[2])
        for seed in seeds
        for use in seed.uses
    }
    assert provenance == frozenset(expected)


async def test_a_corpus_scale_entry_survives_replay_under_the_corpus_scope() -> None:
    """Determined is only half of it: under the scope an analyst who can run these
    blueprints actually holds, the entry must still REPLAY. This is what a
    too-strict guard would break silently."""
    seeds = _seeds()
    corpus_scope = frozenset({use for seed in seeds for use in seed.uses})
    provenance = await _provenance(*(_recall_record(seed) for seed in seeds))
    assert scope_filter.is_provenance_in_scope(provenance, corpus_scope)
    # ...and still drops the moment the scope narrows past any of that union.
    narrowed = corpus_scope - {"dbpcm_warehouse.employee.department_name"}
    assert not scope_filter.is_provenance_in_scope(provenance, narrowed)


async def test_a_resolves_value_outside_uses_makes_that_seed_undetermined() -> None:
    """The canary this file exists to be. Take a real seed, pin a term to a column
    its DAG does not read — the authoring mistake that reintroduces the leak — and
    the entry must fail closed rather than claim the narrow `uses` footprint while
    printing a column outside it."""
    seed = next(s for s in _seeds() if s.id == "bp-average-salary-by-department")
    assert await _provenance(_recall_record(seed)) is not None  # unedited: determined

    record = _recall_record(seed)
    record["resolves_json"] = json.dumps({"salary": "annual_salary", "band": "salary_band"})
    assert await _provenance(record) is None


async def test_a_qualified_result_grain_entry_outside_uses_is_caught_too() -> None:
    """A grain entry is normally an output-column display LABEL (`[Department]` for
    `SELECT … AS department`), which is why a bare one is not required to be
    covered. A QUALIFIED one is no label under any reading — it is a column
    identifier, and it must be covered."""
    seed = next(s for s in _seeds() if s.id == "bp-active-headcount-by-department")
    record = _recall_record(seed)
    record["result_grain_json"] = json.dumps(["dbpcm_warehouse.employee.annual_salary"])
    assert await _provenance(record) is None
