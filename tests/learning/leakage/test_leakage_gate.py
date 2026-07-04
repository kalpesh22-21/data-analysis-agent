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

from data_agent.learning.stage import StageContext
from data_agent.learning.user import InMemoryUserKnowledgeStore

from ..extractor.helpers import make_summary
from .helpers import (
    KEEP_VERDICT,
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
    # Q1 rework: `notes` (extractor free text that lands with the artifact) is now a
    # scanned blueprint surface too, so a leak there can never slip through as `pass`.
    assert verdict.scanned_fields == ("intent", "result_signature", "notes")


async def test_entity_in_intent_never_passes():
    """S5-leakage-blocks-entity: the leaking blueprint (intent carries E12345 +
    2025) must NOT pass; with no user_fact signal it quarantines. The gate stamps
    the verdict and flows on (`continue`) — the WRITER routes the near-miss to the
    inbox (frozen: S5 is not the routing authority)."""
    result, _ = await _run(envelope("leaking"))
    verdict = LeakageVerdict.from_doc(result.envelope.entity_scan)
    assert verdict.result != "pass"
    assert verdict.result in {"reroute", "quarantine", "reject"}
    assert verdict.result == "quarantine"  # blueprint + no user_fact => hold
    # S5 leaves the status untouched (extracted) and flows on; the writer routes it.
    assert result.envelope.status == "extracted"
    assert result.control == "continue"
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
    assert result.control == "continue"  # writer routes the near-miss, not S5


# --- S5-reroute-to-user-knowledge --------------------------------------------


async def test_reroute_commits_user_fact_to_user_store_end_to_end():
    """S5-reroute-to-user-knowledge: a user_fact classification reroutes — the
    entity-bearing fact is COMMITTED directly into the injected per-user store
    (scoped to the SESSION user_id, R2/R6), not spawned as an orphan candidate. The
    residual global candidate flows on (`continue`) for the writer to route."""
    from data_agent.learning.user import InMemoryUserKnowledgeStore

    user_store = InMemoryUserKnowledgeStore()
    scanner = ScriptedSemanticScanner(classification="user_fact")
    gate = LeakageGateStage(
        candidate_store=InMemoryCandidateStore(),
        semantic_scanner=scanner,
        user_store=user_store,
    )
    result = await gate.process(envelope("leaking"), ctx())

    # the settled reroute verdict is stamped and the candidate flows on
    verdict = LeakageVerdict.from_doc(result.envelope.entity_scan)
    assert verdict.result == "reroute"
    assert result.control == "continue"

    # the fact landed in the per-user store, scoped to the session's user (user-1),
    # carrying the entity-bearing statement
    rows = await user_store.list_for_user("user-1")
    assert len(rows) == 1
    fact = rows[0]
    assert fact.user_id == "user-1"
    assert fact.scope == "user"
    assert fact.fact_type == "frequent_entity"
    assert "E12345" in fact.statement
    # nothing landed under any other user
    assert await user_store.list_for_user("someone-else") == []


async def test_reroute_without_user_store_is_failsafe_noop():
    """If no per-user store is wired, the reroute cannot safely land the fact — the
    gate is a no-op on the commit and flows the near-miss on to the human inbox."""
    store = InMemoryCandidateStore()
    result, store = await _run(
        envelope("leaking"), classification="user_fact", store=store
    )
    verdict = LeakageVerdict.from_doc(result.envelope.entity_scan)
    assert verdict.result == "reroute"
    assert result.control == "continue"
    # no orphaned spawned candidate is created in the candidate store
    assert [c for c in store.all_candidates() if c.type == "user_knowledge"] == []


async def test_reroute_with_empty_session_user_id_writes_no_unscoped_record():
    """S2: `ctx.summary.user_id` can be blank (`job.user_id or ""`). A reroute must
    NOT then commit an UNSCOPED entity-bearing record (`userknow::::`) — the commit is
    refused (mirroring `from_candidate`'s non-empty guard) and the residual near-miss
    is left for the human inbox (`continue`)."""
    user_store = InMemoryUserKnowledgeStore()
    gate = LeakageGateStage(
        candidate_store=InMemoryCandidateStore(),
        semantic_scanner=ScriptedSemanticScanner(classification="user_fact"),
        user_store=user_store,
    )
    blank_ctx = StageContext(summary=make_summary(user_id=""), verdict=KEEP_VERDICT)

    result = await gate.process(envelope("leaking"), blank_ctx)

    verdict = LeakageVerdict.from_doc(result.envelope.entity_scan)
    assert verdict.result == "reroute"
    assert result.control == "continue"
    # NOTHING committed — never an unscoped record under a blank user_id.
    assert user_store.commit_calls == 0
    assert user_store.all_records() == []


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
