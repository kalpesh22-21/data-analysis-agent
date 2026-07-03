"""S5 leakage gate (D58/D17) — Layer-1.

Tagged slugs:
  * `S5-leakage-blocks-entity`      — an entity in intent/result_signature yields a
    verdict in {reroute, quarantine, reject}, NEVER pass.
  * `S5-reroute-to-user-knowledge`  — a `reroute` verdict spawns a linked
    `user_knowledge` candidate and rejects the global one.

The injected semantic scan is a scripted double (NO real LLM). The gate WRITES the
settled `LeakageVerdict` into `entity_scan`, overwriting S3's `pending` self-check.
"""

from __future__ import annotations

from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.verdicts import LeakageVerdict
from data_agent.learning.leakage import LeakageGateStage

from .helpers import (
    ScriptedSemanticScanner,
    ctx,
    envelope,
    global_knowledge_envelope,
)


async def _run(env, *, classification="clean", store=None):
    store = store or InMemoryCandidateStore()
    scanner = ScriptedSemanticScanner(classification=classification)
    gate = LeakageGateStage(candidate_store=store, semantic_scanner=scanner)
    result = await gate.process(env, ctx())
    return result, store


# --- S5-leakage-blocks-entity ------------------------------------------------


async def test_clean_blueprint_passes():
    result, _ = await _run(envelope("clean"))
    verdict = LeakageVerdict.from_doc(result.envelope.entity_scan)
    assert verdict.result == "pass"
    assert result.control == "continue"
    # pass leaves the lifecycle status to the downstream writer (still extracted).
    assert result.envelope.status == "extracted"
    assert verdict.hits == ()
    assert verdict.scanned_fields == ("intent", "result_signature")


async def test_entity_in_intent_never_passes():
    """S5-leakage-blocks-entity: the leaking blueprint (intent carries E12345 +
    2025) must NOT pass; with no user_fact signal it quarantines."""
    result, _ = await _run(envelope("leaking"))
    verdict = LeakageVerdict.from_doc(result.envelope.entity_scan)
    assert verdict.result != "pass"
    assert verdict.result in {"reroute", "quarantine", "reject"}
    assert verdict.result == "quarantine"  # blueprint + no user_fact => hold
    assert result.envelope.status == "quarantined"
    assert result.control == "route_inbox"
    # the settled verdict overwrote S3's pending self-check
    assert LeakageVerdict.is_settled(result.envelope.entity_scan)
    kinds = {h.kind for h in verdict.hits}
    assert "employee_code" in kinds  # E12345
    assert any(h.field == "intent" for h in verdict.hits)


async def test_hard_entity_in_global_knowledge_rejects():
    env = global_knowledge_envelope(
        statement="employees in dept 0420 earning over 100000 are flagged"
    )
    result, _ = await _run(env)
    verdict = LeakageVerdict.from_doc(result.envelope.entity_scan)
    assert verdict.result == "reject"
    assert result.envelope.status == "rejected"
    assert result.control == "route_inbox"
    assert verdict.scanned_fields == ("statement",)


async def test_settled_verdict_records_scanner_provenance():
    result, _ = await _run(envelope("leaking"))
    verdict = LeakageVerdict.from_doc(result.envelope.entity_scan)
    # a wired semantic scanner => the verdict advertises the llm layer
    assert verdict.scanner == "regex+ner+llm"


async def test_null_scanner_labels_regex_only():
    from data_agent.learning.leakage.scanner import NullSemanticEntityScanner

    gate = LeakageGateStage(
        candidate_store=InMemoryCandidateStore(),
        semantic_scanner=NullSemanticEntityScanner(),
    )
    result = await gate.process(envelope("leaking"), ctx())
    verdict = LeakageVerdict.from_doc(result.envelope.entity_scan)
    assert verdict.scanner == "regex+ner"
    assert verdict.result == "quarantine"


# --- S5-reroute-to-user-knowledge --------------------------------------------


async def test_reroute_spawns_linked_user_knowledge_and_rejects_global():
    """S5-reroute-to-user-knowledge: a user_fact classification reroutes — a linked
    user_knowledge candidate is spawned (depends_on the global) and the global is
    rejected."""
    store = InMemoryCandidateStore()
    result, store = await _run(
        envelope("leaking"), classification="user_fact", store=store
    )

    # the global candidate is rejected + persisted (route_inbox = persist + stop)
    verdict = LeakageVerdict.from_doc(result.envelope.entity_scan)
    assert verdict.result == "reroute"
    assert result.envelope.status == "rejected"
    assert result.control == "route_inbox"

    # a linked user_knowledge candidate was spawned into the candidate store
    spawned = [c for c in store.all_candidates() if c.type == "user_knowledge"]
    assert len(spawned) == 1
    uk = spawned[0]
    assert uk.depends_on == (result.envelope.candidate_id,)
    assert uk.payload["user_id"] == "user-1"
    assert uk.payload["scope"] == "user"
    # the entity-bearing statement was carried onto the user fact
    assert "E12345" in uk.payload["statement"]
    assert uk.status == "extracted"


async def test_non_global_targets_pass_through_untouched():
    """user_knowledge / schema_edit are not the gate's remit — untouched."""
    from dataclasses import replace

    uk = replace(
        envelope("clean"),
        type="user_knowledge",
        payload={"user_id": "user-1", "statement": "I mean E12345 by 'me'"},
    )
    gate = LeakageGateStage(candidate_store=InMemoryCandidateStore())
    result = await gate.process(uk, ctx())
    assert result.control == "continue"
    # entity_scan untouched — still S3's pending sentinel, NOT a settled verdict
    assert result.envelope.entity_scan.get("result") == "pending"
    assert not LeakageVerdict.is_settled(result.envelope.entity_scan)


async def test_gate_never_reads_pending_self_check_as_verdict():
    """The gate is the WRITER — it must not parse the incoming pending self-check
    as a settled verdict (would raise). A pass over the clean candidate succeeds
    without touching the un-settled inbound entity_scan."""
    env = envelope("clean")
    assert not LeakageVerdict.is_settled(env.entity_scan)  # inbound is pending
    result, _ = await _run(env)
    # writing succeeded and produced a settled verdict
    assert LeakageVerdict.is_settled(result.envelope.entity_scan)
