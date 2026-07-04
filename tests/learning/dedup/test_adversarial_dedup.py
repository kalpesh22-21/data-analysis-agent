"""Adversarial QA on the S6 D48 dedup (invariant #2 — dedup soundness).

Two failure modes are catastrophic and opposite:
  * FALSE MERGE — two semantically DIFFERENT blueprints collapse to one key → data
    loss (one is silently dropped/incremented away).
  * SPURIOUS DUP — two IDENTICAL blueprints produce DIFFERENT keys → a missed
    `increment`, two artifacts for one concept (corpus pollution, split hit_count).

We attack `compute_canonical_key` directly (the hasher S6 owns; S4 is a different
track, so the key must be robust to any order/duplication the producer emits) and
the fail-soft path (must never auto-drop). strict-xfail = real hole; passing =
pinned guarantee.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.dedup import (
    CorpusArtifact,
    DedupStage,
    InMemoryBlueprintCorpus,
    compute_canonical_key,
)
from data_agent.learning.dedup.canonical_key import compute_canonical_key as _key
from data_agent.learning.stage import StageContext
from data_agent.learning.summary.models import SessionSummary
from data_agent.learning.triage import TriageVerdict
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"

_RESOLVES = {"earnings": "payroll.payroll_fact.gross_pay"}
_GRAIN = {"columns": [], "verifiable": False}
_NORM = "SELECT sum(gross_pay) AS t FROM payroll.payroll_fact WHERE d = {d: }"


def _ctx() -> StageContext:
    summary = SessionSummary(
        session_id="sess-fixture", user_id="u1", scope_ref="scope-1",
        trace_id="trace-fixture", content_hash="hash-fixture", turns=(),
        tool_calls=(), blueprint_usages=(), askuser_exchanges=(),
        failed_fixed_sql=(), accepted_signal="no_correction",
    )
    return StageContext(summary=summary, verdict=TriageVerdict(decision="keep", reason="K1"))


def _single_envelope() -> CandidateEnvelope:
    doc = json.loads((FIXTURES / "s4_enriched_blueprint.json").read_text())
    return CandidateEnvelope.from_doc(doc["single"]["envelope"])


# =============================================================================
# STRICT-XFAIL REPROS — spurious-dup holes in the canonical key
# =============================================================================


def test_uses_rules_duplication_must_not_change_key():
    once = _key(_RESOLVES, ["rule.dept_scope"], _GRAIN, _NORM)
    twice = _key(_RESOLVES, ["rule.dept_scope", "rule.dept_scope"], _GRAIN, _NORM)
    # SECURE: the rule SET is identical → same key. Currently FAILS -> strict xfail.
    assert once == twice


def test_result_grain_column_order_must_not_change_key():
    ab = _key(_RESOLVES, ["r"], {"columns": ["department", "region"], "verifiable": True}, _NORM)
    ba = _key(_RESOLVES, ["r"], {"columns": ["region", "department"], "verifiable": True}, _NORM)
    # SECURE: same grain columns → same key. Currently FAILS -> strict xfail.
    assert ab == ba


# =============================================================================
# PASSING HARDENING — soundness that HOLDS under attack (pin it)
# =============================================================================


def test_uses_rules_permutation_without_dup_is_stable():
    """Pure re-ordering (no duplicates) is normalized away by the explicit sort —
    the ordering half of the invariant holds; only duplication (above) does not."""
    a = _key(_RESOLVES, ["rule.a", "rule.b", "rule.c"], _GRAIN, _NORM)
    b = _key(_RESOLVES, ["rule.c", "rule.a", "rule.b"], _GRAIN, _NORM)
    assert a == b


def test_resolves_key_order_is_stable():
    """resolves is a dict; canonical_json sorts its keys, so insertion order cannot
    mint a different key."""
    a = compute_canonical_key({"x": "t.c1", "y": "t.c2"}, [], _GRAIN, _NORM)
    b = compute_canonical_key({"y": "t.c2", "x": "t.c1"}, [], _GRAIN, _NORM)
    assert a == b


def test_different_templates_never_collide_to_same_key():
    """A false-merge guard: two genuinely different templates (different AST norm)
    must produce different keys — the hard key never collapses distinct SQL."""
    k1 = _key(_RESOLVES, [], _GRAIN, "SELECT a FROM t WHERE d = {d: }")
    k2 = _key(_RESOLVES, [], _GRAIN, "SELECT b FROM t WHERE d = {d: }")
    assert k1 != k2


async def test_failsoft_never_auto_drops_against_identical_intent_artifact():
    """Fail-soft (empty canonical_ast_norm) must never `increment`/`drop`, even when
    the corpus holds an artifact whose intent is IDENTICAL to this candidate's — the
    soft near-match is routed (continue), never silently merged away (D48/D52).

    This is the strongest fail-soft attack: identical intent would drive the soft
    embedder to a merge verdict, but a merge is a REVIEW routing (control=continue),
    never the auto-drop that a hard `increment` performs."""
    env = _single_envelope()
    intent = env.payload["intent"]
    gen = {**env.payload["generalization"], "canonical_ast_norm": ""}  # fail-soft
    env = replace(env, payload={**env.payload, "generalization": gen})

    vec = [0.5, 0.5, 0.5, 0.5]
    embedder = FakeEmbeddingClient({intent: vec})  # identical intent → cosine 1.0
    corpus = InMemoryBlueprintCorpus(
        [CorpusArtifact(id="bp::landed", canonical_key="sha256:would-be", intent=intent, hit_count=7)]
    )
    stage = DedupStage(corpus, embedder)
    result = await stage.process(env, _ctx())

    # No auto-drop, no hit-count bump — the invariant "fail-soft never a wrong merge".
    assert result.control != "drop"
    assert result.envelope.dedup.action != "increment"
    assert corpus.increment_calls == []
    assert corpus.get_sync("sha256:would-be").hit_count == 7  # untouched
