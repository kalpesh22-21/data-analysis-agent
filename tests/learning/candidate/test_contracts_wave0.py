"""contracts-envelope-additive (D102, contracts-design §1/§6/§9 row 13).

The Wave-0 envelope grows by ADDITIVE typed verdict fields only. This proves the
additivity invariant: every stage's output is a valid `to_doc`/`from_doc` round-trip
*with the other stages' fields absent or at their default*, and a pre-stage (S3)
doc — which knows nothing of `dedup`/`drift`/`generalization` — still parses. It
also proves each frozen verdict / generalization dataclass round-trips, and that
the six frozen fixtures load through their models unchanged.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from data_agent.learning.candidate import (
    BlueprintGeneralization,
    CandidateEnvelope,
    CandidateStatus,
    DedupVerdict,
    DriftStamp,
    EntityHit,
    LeakageVerdict,
    NodeTemplate,
    ResultGrainStamp,
    StaticValidation,
)

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"


def _base_envelope(**overrides) -> CandidateEnvelope:
    """A pre-stage (S3) envelope: pending self-check, no dedup, unchecked drift."""
    kw = dict(
        candidate_id="candidate::hash-x::0",
        type="blueprint",
        status=CandidateStatus.EXTRACTED,
        payload={"intent": "total earnings for a department in a given year"},
        source_session="sess-1",
        source_trace="trace-1",
        evidence_refs=("evidence::sess-1::e1",),
        extractor_rationale="reusable report",
        entity_scan={"result": "pending", "hits": [], "self_check_contains_entities": False},
        confidence=0.9,
        proposed_action="new",
        depends_on=(),
        content_hash="hash-x",
        created_at="2026-07-03T00:00:00+00:00",
    )
    kw.update(overrides)
    return CandidateEnvelope(**kw)


# --- the pre-stage spine round-trips + defaults are stage-neutral -------------


def test_pre_stage_envelope_round_trips_with_stage_fields_absent():
    env = _base_envelope()
    # Defaults: no dedup, unchecked drift — the S3 state.
    assert env.dedup is None
    assert env.drift == DriftStamp()  # status == "unchecked"

    round_tripped = CandidateEnvelope.from_doc(env.to_doc())
    assert round_tripped == env


def test_old_s3_doc_without_new_keys_still_parses():
    """An envelope doc persisted BEFORE Wave 0 (no `dedup`/`drift` keys) must
    still deserialize — additivity means missing keys fall to their defaults."""
    doc = _base_envelope().to_doc()
    del doc["dedup"]
    del doc["drift"]
    parsed = CandidateEnvelope.from_doc(doc)
    assert parsed.dedup is None
    assert parsed.drift == DriftStamp()
    # The fail-to-review stamps are absent from every doc written before that slice, and
    # ABSENT is the load-bearing value: their presence is what marks a row as a form to
    # complete (`writer/routing.py::derive_inbox_reason` keys on it), so an old doc that
    # read back with an empty block would put a task in front of a reviewer that nobody
    # ever recorded.
    assert "decline" not in doc and "revalidation" not in doc
    assert parsed.decline is None
    assert parsed.revalidation is None


def test_the_fail_to_review_stamps_round_trip_and_normalize_rather_than_raise():
    """Both new fields follow the store's rule: emitted only when set, and a MALFORMED
    stored value reads as ABSENT rather than raising inside a queue worker or an inbox
    projection. The snapshot in particular must read as missing rather than as empty —
    an empty `sql_by_ref` would make every re-validation decline `unrewritable_sql`, a
    misdiagnosis blaming the model's SQL for a storage fault."""
    from data_agent.learning.candidate.decline import (
        DeclineBlock,
        EvidencePointer,
        ValidationSnapshot,
    )

    env = _base_envelope(
        status=CandidateStatus.NEEDS_PARAMETERIZATION,
        decline=DeclineBlock(
            reason="totality_violation",
            detail="two predicates have no entry",
            corrections_attempted=2,
            correction_history=("first", "second"),
        ),
        revalidation=ValidationSnapshot(
            session_id="sess-1",
            user_id="user-1",
            trace_id="trace-1",
            content_hash="hash-x",
            accepted_signal="no_correction",
            sql_by_ref={"tc1": ("SELECT 1 WHERE a = 'b'",)},
            evidence=(EvidencePointer(turn_ref=0, tool_call_ref="tc1"),),
        ),
    )
    assert CandidateEnvelope.from_doc(env.to_doc()) == env

    junk = env.to_doc()
    junk["decline"] = "totality_violation"  # a string where the block belongs
    junk["revalidation"] = ["tc1"]
    parsed = CandidateEnvelope.from_doc(junk)
    assert parsed.decline is None
    assert parsed.revalidation is None

    # A block with no reason is not a decline anyone can act on, so it reads as absent
    # too — the same rule, applied to the field that carries the meaning.
    reasonless = env.to_doc()
    reasonless["decline"] = {"detail": "something happened"}
    assert CandidateEnvelope.from_doc(reasonless).decline is None


# --- each stage's field round-trips with the OTHER fields at default ----------


def test_entity_scan_leakage_verdict_round_trips_alone():
    verdict = LeakageVerdict(
        result="quarantine",
        hits=(EntityHit(field="intent", kind="employee_code", span="E12345"),),
        scanned_fields=("intent", "result_signature"),
        scanner="regex+ner+llm",
    )
    env = _base_envelope(entity_scan=verdict.to_doc())
    # dedup/drift untouched (default) while entity_scan is filled.
    assert env.dedup is None and env.drift == DriftStamp()
    back = CandidateEnvelope.from_doc(env.to_doc())
    assert back == env
    assert LeakageVerdict.is_settled(back.entity_scan)
    assert LeakageVerdict.from_doc(back.entity_scan) == verdict


def test_leakage_verdict_from_doc_rejects_pending_self_check():
    """The S3 pre-leakage `entity_scan` is a `{result:"pending"}` self-check whose
    `hits` are bare strings — NOT a settled `LeakageVerdict`. `is_settled` must
    say so, and `from_doc` must fail LOUD rather than mint an out-of-literal
    verdict or choke on the bare-string hits (the S5/S7 boundary guard)."""
    pending = {"result": "pending", "hits": ["E12345"],
               "self_check_contains_entities": True}
    assert LeakageVerdict.is_settled(pending) is False
    with pytest.raises(ValueError, match="not a settled verdict"):
        LeakageVerdict.from_doc(pending)
    # A settled verdict passes both.
    settled = LeakageVerdict(result="reject").to_doc()
    assert LeakageVerdict.is_settled(settled) is True
    assert LeakageVerdict.from_doc(settled).result == "reject"
    # An unknown/garbage result is also rejected (not silently accepted).
    with pytest.raises(ValueError):
        LeakageVerdict.from_doc({"result": "bogus"})


def test_dedup_verdict_round_trips_alone():
    dedup = DedupVerdict(
        canonical_key="sha256:abc", matched_id="blueprint::x",
        similarity=1.0, action="increment", layer="hard",
    )
    env = _base_envelope(dedup=dedup)
    # entity_scan still the pending S3 dict; drift still default.
    assert env.entity_scan["result"] == "pending" and env.drift == DriftStamp()
    back = CandidateEnvelope.from_doc(env.to_doc())
    assert back == env
    assert back.dedup == dedup


def test_dedup_none_matched_id_round_trips():
    dedup = DedupVerdict(canonical_key="sha256:new", matched_id=None,
                         similarity=0.0, action="insert", layer="hard")
    back = CandidateEnvelope.from_doc(_base_envelope(dedup=dedup).to_doc())
    assert back.dedup == dedup
    assert back.dedup.matched_id is None


def test_drift_stamp_round_trips_alone():
    drift = DriftStamp(
        status="suspect", last_drift_check_at="2026-07-03T01:00:00+00:00",
        probes=("grain_integrity", "catalog_conformance"), failed_probe="grain_integrity",
    )
    env = _base_envelope(drift=drift)
    assert env.dedup is None and env.entity_scan["result"] == "pending"
    back = CandidateEnvelope.from_doc(env.to_doc())
    assert back == env
    assert back.drift == drift


def test_all_three_verdicts_together_round_trip():
    verdict = LeakageVerdict(result="pass", scanned_fields=("intent",), scanner="regex")
    dedup = DedupVerdict(canonical_key="sha256:k", matched_id=None, similarity=0.5,
                         action="insert", layer="soft")
    drift = DriftStamp(status="clean", last_drift_check_at="2026-07-03T02:00:00+00:00",
                       probes=("grain_integrity",))
    env = _base_envelope(entity_scan=verdict.to_doc(), dedup=dedup, drift=drift)
    assert CandidateEnvelope.from_doc(env.to_doc()) == env


# --- payload["generalization"] (Contract A) round-trips inside payload --------


def _sample_generalization() -> BlueprintGeneralization:
    return BlueprintGeneralization(
        sql_template="SELECT sum(gross_pay) FROM t WHERE department = :d",
        uses=("payroll.payroll_fact.department", "payroll.payroll_fact.gross_pay"),
        uses_rules=("rule.earning_record_type",),
        node_templates=(),
        result_grain=ResultGrainStamp(columns=(), verifiable=False),
        static_validation=StaticValidation(
            explain_ok=True, binds_to_subset_uses=True, dag_ok=True,
            read_only_select=True, outcome="ok",
        ),
        canonical_ast_norm="SELECT sum(gross_pay) FROM t WHERE department = {d: }",
    )


def test_generalization_round_trips_inside_payload():
    gen = _sample_generalization()
    payload = {"intent": "x", "generalization": gen.to_doc()}
    env = _base_envelope(payload=payload)
    back = CandidateEnvelope.from_doc(env.to_doc())
    assert back == env
    assert BlueprintGeneralization.from_doc(back.payload["generalization"]) == gen


def test_generalization_composite_node_templates_round_trip():
    gen = BlueprintGeneralization(
        sql_template=None,
        uses=("a.b.c",),
        uses_rules=(),
        node_templates=(NodeTemplate(order=0, sql_template="SELECT 1"),
                        NodeTemplate(order=1, sql_template="SELECT 2")),
        result_grain=ResultGrainStamp(columns=("a",), verifiable=True),
        static_validation=StaticValidation(
            explain_ok=False, binds_to_subset_uses=True, dag_ok=False,
            read_only_select=True, outcome="fail_to_review", reason="dag_cycle",
        ),
        canonical_ast_norm="SELECT 1\nSELECT 2",
    )
    assert BlueprintGeneralization.from_doc(gen.to_doc()) == gen


# --- the frozen fixtures load + round-trip through their models ---------------


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_s3_blueprint_plan_fixture_present():
    plan = _load("s3_blueprint_plan.json")
    assert plan["single"]["kind"] == "single"
    assert plan["composite"]["kind"] == "composite"
    assert plan["composite"]["composes"]  # a real DAG


def test_s3_candidates_mixed_fixture_round_trips():
    mixed = _load("s3_candidates_mixed.json")
    clean = CandidateEnvelope.from_doc(mixed["clean"])
    leaking = CandidateEnvelope.from_doc(mixed["leaking"])
    assert clean.to_doc() == mixed["clean"]
    assert leaking.to_doc() == mixed["leaking"]
    # The leaking one carries an entity in intent; the clean one does not.
    assert "E12345" in leaking.payload["intent"]
    assert "E12345" not in clean.payload["intent"]


def test_s4_enriched_blueprint_fixture_round_trips():
    data = _load("s4_enriched_blueprint.json")
    env = CandidateEnvelope.from_doc(data["single"]["envelope"])
    assert env.to_doc() == data["single"]["envelope"]
    gen = BlueprintGeneralization.from_doc(data["single"]["generalization"])
    assert gen.to_doc() == data["single"]["generalization"]
    # §11.2: a frozen canonical_ast_norm string is carried for S6 to hash.
    assert gen.canonical_ast_norm
    assert "{department: }" in gen.canonical_ast_norm
    assert gen.static_validation.outcome == "ok"
    # Composite variant carries per-node templates and a None top-level template.
    comp = BlueprintGeneralization.from_doc(data["composite"]["generalization"])
    assert comp.sql_template is None
    assert len(comp.node_templates) == 2
    assert comp.to_doc() == data["composite"]["generalization"]


def test_envelopes_each_reason_fixture_round_trips():
    reasons = _load("envelopes_each_reason.json")
    expected = {
        "knowledge_pre_gate", "schema_edit", "leakage_near_miss",
        "blueprint_sampled", "dedup_conflict", "fail_to_review",
    }
    assert set(reasons) == expected
    for doc in reasons.values():
        env = CandidateEnvelope.from_doc(doc)
        assert env.to_doc() == doc
    # dedup_conflict carries a conflict DedupVerdict; leakage_near_miss a hit.
    conflict = CandidateEnvelope.from_doc(reasons["dedup_conflict"])
    assert conflict.dedup is not None and conflict.dedup.action == "conflict"
    near_miss = CandidateEnvelope.from_doc(reasons["leakage_near_miss"])
    assert LeakageVerdict.from_doc(near_miss.entity_scan).hits
    # fail_to_review MUST carry a generalization block S7 routes off — its
    # static_validation.outcome is fail_to_review with one failing check + reason.
    ftr = CandidateEnvelope.from_doc(reasons["fail_to_review"])
    gen = BlueprintGeneralization.from_doc(ftr.payload["generalization"])
    assert gen.static_validation.outcome == "fail_to_review"
    assert gen.static_validation.read_only_select is False
    assert gen.static_validation.reason == "not_read_only_select"


def test_user_knowledge_and_schema_edit_fixtures_round_trip():
    uk = CandidateEnvelope.from_doc(_load("s3_user_knowledge.json"))
    se = CandidateEnvelope.from_doc(_load("s3_schema_edit.json"))
    assert uk.type == "user_knowledge"
    assert se.type == "schema_edit"
    assert uk.to_doc() == _load("s3_user_knowledge.json")
    assert se.to_doc() == _load("s3_schema_edit.json")


def test_existing_corpus_keys_fixture_present():
    corpus = _load("existing_corpus_keys.json")
    assert corpus["artifacts"]
    for art in corpus["artifacts"]:
        assert art["canonical_key"]
        assert "hit_count" in art  # S6 increments this on a hard-key hit


# --- a stage-neutral replace() keeps the frozen spine intact -----------------


def test_replace_one_field_leaves_spine_untouched():
    env = _base_envelope()
    enriched = replace(env, dedup=DedupVerdict(
        canonical_key="sha256:z", matched_id=None, similarity=0.0,
        action="insert", layer="hard"))
    # The S3 spine is byte-identical; only the additive field changed.
    assert enriched.candidate_id == env.candidate_id
    assert enriched.payload == env.payload
    assert enriched.entity_scan == env.entity_scan
    assert enriched.content_hash == env.content_hash
    assert enriched.dedup is not None and env.dedup is None
