"""S6 dedup — the UNTRUSTED rehydrated-JSON crash class.

`DedupStage.process` reads a `CandidateEnvelope.payload` that is model-authored and
rehydrated from Couchbase. The envelope itself is a dict by construction; everything
INSIDE it is whatever the extractor produced and the store round-tripped. The frozen
hard key (`compute_canonical_key`) cannot be made defensive — its digests are persisted
(Contract C §3) — so the guard has to live at the call site, and it has to be DERIVED
from the operations that function performs rather than from a list of field names.

**History.** Before the PriorArt Slice-2 guard (`_hard_key_inputs_ok`), a bare-string
`result_grain` raised `ValueError: dictionary update sequence element #0 has length 1`
straight out of `process` — killing the whole extraction for that session, not just the
one candidate. It was found by a fuzz case in the sibling prior-art suite, which is the
fourth time this class has surfaced in this codebase; hence a dedicated file.

Slug: S6-untrusted-payload-never-raises.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.dedup import DedupStage, InMemoryBlueprintCorpus
from data_agent.learning.stage import StageContext
from data_agent.learning.summary.models import SessionSummary
from data_agent.learning.triage import TriageVerdict
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"


def _ctx() -> StageContext:
    return StageContext(
        summary=SessionSummary(
            session_id="s", user_id="u", scope_ref="sc", trace_id="t", content_hash="h",
            turns=(), tool_calls=(), blueprint_usages=(), askuser_exchanges=(),
            failed_fixed_sql=(), accepted_signal="no_correction",
        ),
        verdict=TriageVerdict(decision="keep", reason="K1"),
    )


def _envelope(**payload_overrides) -> CandidateEnvelope:
    doc = json.loads((FIXTURES / "s4_enriched_blueprint.json").read_text())
    env = CandidateEnvelope.from_doc(doc["single"]["envelope"])
    if not payload_overrides:
        return env
    return replace(env, payload={**env.payload, **payload_overrides})


def _with_gen(**gen_overrides) -> CandidateEnvelope:
    env = _envelope()
    return replace(
        env, payload={**env.payload, "generalization": {**env.payload["generalization"], **gen_overrides}}
    )


# The operation each field feeds, and the type that breaks it. Every case below was
# chosen because SOME total-looking operation on the intended type is fatal on a
# neighbouring one — not because the field name looked risky.
_HOSTILE_PAYLOADS = [
    # `dict(resolves)` — ValueError on a str / list of scalars, TypeError on an int.
    pytest.param({"resolves": "department"}, id="resolves-bare-string"),
    pytest.param({"resolves": ["a", "b"]}, id="resolves-list"),
    pytest.param({"resolves": 7}, id="resolves-int"),
    pytest.param({"resolves": None}, id="resolves-null"),
    # `(intent or "").strip()` — AttributeError on any truthy non-string.
    pytest.param({"intent": 5}, id="intent-int"),
    pytest.param({"intent": ["total earnings"]}, id="intent-list"),
    pytest.param({"intent": {"text": "x"}}, id="intent-dict"),
]

_HOSTILE_GENERALIZATIONS = [
    # `dict(result_grain)` then `sorted(columns)`.
    pytest.param({"result_grain": "department"}, id="grain-bare-string"),
    pytest.param({"result_grain": ["department"]}, id="grain-list"),
    pytest.param({"result_grain": 3}, id="grain-int"),
    pytest.param({"result_grain": {"columns": [1, "department"]}}, id="grain-mixed-columns"),
    pytest.param({"result_grain": {"columns": [None]}}, id="grain-null-column"),
    # NOTE `{"columns": "department"}` is deliberately ABSENT — it is tolerated, not
    # rejected; see `test_a_columns_value_that_is_not_a_list_is_tolerated_not_rejected`.
    # `sorted(set(uses_rules))` — unhashable members, mixed types, char explosion.
    pytest.param({"uses_rules": "rule-a"}, id="rules-bare-string"),
    pytest.param({"uses_rules": [["rule-a"]]}, id="rules-nested-list"),
    pytest.param({"uses_rules": [{"id": "rule-a"}]}, id="rules-dicts"),
    pytest.param({"uses_rules": [1, "rule-a"]}, id="rules-mixed-types"),
    pytest.param({"uses_rules": 7}, id="rules-int"),
    # `(canonical_ast_norm or "").strip()` — AttributeError on a truthy non-string.
    pytest.param({"canonical_ast_norm": 42}, id="norm-int"),
    pytest.param({"canonical_ast_norm": ["SELECT 1"]}, id="norm-list"),
    pytest.param({"canonical_ast_norm": {"sql": "SELECT 1"}}, id="norm-dict"),
]


@pytest.mark.parametrize("overrides", _HOSTILE_PAYLOADS)
async def test_a_hostile_payload_field_never_raises_out_of_the_stage(overrides):
    stage = DedupStage(InMemoryBlueprintCorpus(), FakeEmbeddingClient())
    result = await stage.process(_envelope(**overrides), _ctx())  # must NOT raise
    assert result.envelope.dedup is not None
    assert result.control in ("continue", "drop")


@pytest.mark.parametrize("overrides", _HOSTILE_GENERALIZATIONS)
async def test_a_hostile_generalization_field_never_raises_out_of_the_stage(overrides):
    stage = DedupStage(InMemoryBlueprintCorpus(), FakeEmbeddingClient())
    result = await stage.process(_with_gen(**overrides), _ctx())  # must NOT raise
    assert result.envelope.dedup is not None
    assert result.control in ("continue", "drop")


@pytest.mark.parametrize("overrides", _HOSTILE_GENERALIZATIONS)
async def test_a_malformed_hard_key_input_skips_the_key_rather_than_minting_one(overrides):
    """The FAIL-SOFT direction (D52), and the reason a guard beats a try/except: a
    malformed input must produce NO hard key, not a key hashed over coerced garbage.
    A spurious key would be persisted as an artifact identity and would then collide
    with — or fail to collide with — real candidates forever."""
    corpus = InMemoryBlueprintCorpus()
    stage = DedupStage(corpus, FakeEmbeddingClient())
    result = await stage.process(_with_gen(**overrides), _ctx())
    assert result.envelope.dedup.canonical_key == ""
    assert result.envelope.dedup.layer == "soft"
    assert corpus.seed_calls == []  # nothing keyed ⇒ nothing seeded


async def test_a_well_formed_candidate_still_mints_its_key():
    """The guard must not be so strict it rejects the real thing — the regression that
    would make every candidate fail-soft and silently disable hard-key dedup entirely."""
    corpus = InMemoryBlueprintCorpus()
    stage = DedupStage(corpus, FakeEmbeddingClient())
    result = await stage.process(_envelope(), _ctx())
    assert result.envelope.dedup.canonical_key.startswith("sha256:")
    assert corpus.seed_calls == [result.envelope.dedup.canonical_key]


async def test_a_columns_value_that_is_not_a_list_is_tolerated_not_rejected():
    """Deliberate asymmetry with `_normalized_grain`: it only sorts `columns` when it IS
    a list/tuple, so a non-list `columns` is harmless to the key function and the guard
    must not invent a stricter rule than the operation it is protecting."""
    stage = DedupStage(InMemoryBlueprintCorpus(), FakeEmbeddingClient())
    env = _with_gen(result_grain={"columns": "department", "verifiable": True})
    result = await stage.process(env, _ctx())
    assert result.envelope.dedup.canonical_key.startswith("sha256:")
