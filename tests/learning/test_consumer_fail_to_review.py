"""Fail-to-review: a merit-passed, form-failed decline is PERSISTED, not discarded.

**The bug this closes.** `extractor/validation.py` says a decline "routes to review,
never a bad landing". It did not: a terminal decline after corrective rounds wrote
nothing durable — no candidate, no inbox row, only a span. One traced session (the
deductions-to-earnings ratio) was processed three times, was ruled novel by the
prior-art judge every time, and evaporated every time
(`docs/decisions/learning-declined-candidate-review.md`).

What is pinned here is the ROUTE and its two conditions, because both directions are
expensive:

  * a merit-passed candidate that dies on the form is now a durable review item
    (`status=needs_parameterization`) carrying the decline, the judge's verdict, the last
    corrected payload and the snapshot the completion path re-validates against;
  * everything else keeps today's behaviour EXACTLY — a merit-failed reason writes
    nothing, and a decline nobody screened (judge skipped / no judge / judge exploded)
    writes nothing either. A review queue of unscreened declines is one that stops being
    read, which is the same failure this slice is fixing, wearing the opposite mask.
"""

from __future__ import annotations

import json

from data_agent.learning.audit import InMemoryAuditStore
from data_agent.learning.audit.judgement import CoverageAssessment
from data_agent.learning.candidate import (
    InMemoryCandidateStore,
    mint_candidate_id,
    mint_review_candidate_id,
)
from data_agent.learning.candidate.models import CandidateStatus
from data_agent.learning.candidate.verdicts import EntityHit
from data_agent.learning.config import LearningSettings
from data_agent.learning.consumer import LearningConsumer
from data_agent.learning.judge import JudgeOutcomeResult
from data_agent.learning.leakage import LeakageGateStage
from data_agent.learning.leakage.scanner import SemanticScanResult
from data_agent.learning.memory_queue import InMemoryLearningQueue
from data_agent.learning.summary.refs import sql_by_ref
from tests._catalog_fixture import fixture_catalog

from .extractor.helpers import (
    KEEP_VERDICT,
    blueprint_raw,
    make_extractor,
    make_summary,
    make_tool_call,
    scripted_turn,
)
from .extractor.test_totality_hint import _COVERED, _INDEX, _KNOWN, _RATIO_SQL, _UNCOVERED

assert fixture_catalog()  # the grounding both the extractor and the assertions use


def _summary(**kwargs):
    return make_summary(tool_calls=(make_tool_call(ref="tc1", sql=_RATIO_SQL),), **kwargs)


def _proceeded() -> JudgeOutcomeResult:
    """The judge's positive verdict: a model looked at the corpus and said this work is
    new. The `covered_by` is the closest artifact it named — the live trace's
    `bp-total-earnings-by-department`."""
    return JudgeOutcomeResult(
        drop=False,
        outcome="proceeded",
        assessment=CoverageAssessment(
            verdict="existing-plus-delta",
            covered_by="bp-total-earnings-by-department",
            covered_by_tier="mcp",
            reason="the closest artifact covers total earnings only",
            confidence=0.86,
        ),
    )


def _consumer(candidates: InMemoryCandidateStore, *, turns, stages=(), audit=None):
    return LearningConsumer(
        store=object(),  # type: ignore[arg-type]
        queue=InMemoryLearningQueue(),
        settings=LearningSettings(_env_file=None),
        audit=audit if audit is not None else InMemoryAuditStore(),
        candidates=candidates,
        extractor=make_extractor(turns, known_rules=_KNOWN, rule_index=_INDEX),
        stages=stages,
    )


def _uncovered_turns(n: int = 3):
    """The live shape: the model emits a plan covering none of the three literal
    predicates, is corrected twice, and repeats itself. 1 emit + 2 corrections."""
    return [scripted_turn([_UNCOVERED]) for _ in range(n)]


# --- the route ---------------------------------------------------------------------


async def test_a_merit_passed_totality_decline_is_persisted_for_review() -> None:
    candidates = InMemoryCandidateStore()
    summary = _summary(content_hash="hash-ratio")
    consumer = _consumer(
        candidates,
        turns=_uncovered_turns(),
        stages=(LeakageGateStage(candidate_store=candidates),),
    )

    await consumer._run_extractor(summary, KEEP_VERDICT, _proceeded())

    stored = candidates.all_candidates()
    assert len(stored) == 1
    env = stored[0]
    assert env.status == CandidateStatus.NEEDS_PARAMETERIZATION
    # A namespace of its own — a kept sibling minted `::0` from a different count over
    # the same batch must never be able to collide with this.
    assert env.candidate_id == mint_review_candidate_id("hash-ratio", 0)
    assert env.candidate_id != mint_candidate_id("hash-ratio", 0)

    # The decline, as the model saw it: the reason, the hint text naming the predicates
    # and the catalog rules, and the record that it WAS re-asked.
    assert env.decline is not None
    assert env.decline.reason == "totality_violation"
    assert "has no entry for 3 literal predicate(s)" in env.decline.detail
    assert "'EARN' as rule 'gross_earnings'" in env.decline.detail
    assert env.decline.corrections_attempted == 2
    assert len(env.decline.correction_history) == 2

    # The judge's verdict travels with it — the reason it is in front of a person.
    assert env.judge is not None
    assert env.judge.covered_by == "bp-total-earnings-by-department"

    # The last corrected payload, and the snapshot the completion path re-validates it
    # against (the accepted SQL by ref, the acceptance marker, the citations as
    # entity-free pointers).
    assert env.payload["intent"] == "ratio of deductions to earnings per employee"
    assert env.revalidation is not None
    assert env.revalidation.sql_by_ref == {"tc1": (_RATIO_SQL,)}
    assert env.revalidation.accepted_signal == "no_correction"
    assert [(p.turn_ref, p.tool_call_ref) for p in env.revalidation.evidence] == [(0, "tc1")]

    # The leakage scan RAN before the persist — a decline is not a side door around the
    # entity gate.
    assert env.entity_scan["result"] == "pass"
    assert env.entity_scan["scanner"] == "regex+ner"


async def test_a_withdrawn_totality_decline_reaches_the_same_review_route() -> None:
    """The route's second entrance, and the one that did not exist. A merit-passed
    candidate can fail the form in two ways: it can be corrected and still be wrong, or
    the model can take the sanctioned exit and OMIT it from the re-emit. The second used
    to leave nothing at all — no decline, so no reason, so no `raw_payload`, so no review
    item — and a session whose blueprint a human could have finished in seconds
    evaporated while the span said zero declines.

    The two conditions the route actually turns on are read here on a WITHDRAWN decline
    rather than assumed to travel with it: the reason is `totality_violation` (the
    decline the model was ASKED to fix, unchanged by the withdrawal) and `raw_payload` is
    the array the correction named it in, which is what `build_declined_envelope` fills
    the review form from."""
    candidates = InMemoryCandidateStore()
    consumer = _consumer(
        candidates,
        # One emit, one correction, and an empty re-emit: the model gave up on it.
        turns=[scripted_turn([_UNCOVERED]), scripted_turn([])],
        stages=(LeakageGateStage(candidate_store=candidates),),
    )

    await consumer._run_extractor(_summary(content_hash="hash-withdrawn"), KEEP_VERDICT,
                                  _proceeded())

    stored = candidates.all_candidates()
    assert len(stored) == 1
    env = stored[0]
    assert env.status == CandidateStatus.NEEDS_PARAMETERIZATION
    assert env.candidate_id == mint_review_candidate_id("hash-withdrawn", 0)
    assert env.decline is not None
    assert env.decline.reason == "totality_violation"
    assert env.decline.corrections_attempted == 1  # asked once, then dropped
    assert len(env.decline.correction_history) == 1
    # The form is filled from the payload the correction POINTED AT — the re-emit was
    # empty, so there is no other candidate this row could have been built from.
    assert env.payload["intent"] == "ratio of deductions to earnings per employee"
    assert env.revalidation is not None
async def test_the_quote_is_snapshotted_to_audit_and_never_to_the_candidate_store() -> None:
    """THE SPLIT, unchanged for a review item: the entity-bearing quote goes to
    `learning_audit` and only the minted ref travels on the envelope (D51/D17).

    A review row is a durable candidate — it can LAND, through completion — so its
    citations have to be fetchable by whoever audits that landing. Carrying only the
    snapshot's entity-free pointers would have left it as the one candidate class in the
    store whose evidence resolves to nothing."""
    candidates = InMemoryCandidateStore()
    audit = InMemoryAuditStore()
    secret = "SSN-424-11-9090-Jane-Doe"
    raw = dict(_UNCOVERED)
    raw["evidence"] = [{"turn_ref": 0, "tool_call_ref": "tc1", "quote": secret}]
    consumer = _consumer(candidates, turns=[scripted_turn([raw]) for _ in range(3)], audit=audit)

    await consumer._run_extractor(_summary(), KEEP_VERDICT, _proceeded())

    env = candidates.all_candidates()[0]
    # The quote is IN the audit store...
    assert audit.snapshot_calls == 1
    assert secret in [s.quote for s in audit._snapshots.values()]
    # ...and the envelope carries the audit KEYS, not the text.
    assert env.evidence_refs
    assert set(env.evidence_refs) == set(audit._snapshots.keys())
    assert secret not in json.dumps(env.to_doc())
    # The entity-free pointers ride the snapshot as well, because the completion path
    # rebuilds the citation from them without ever needing the quote.
    assert [(p.turn_ref, p.tool_call_ref) for p in env.revalidation.evidence] == [(0, "tc1")]


async def test_a_review_row_can_never_arrive_with_unreadable_citations() -> None:
    """Why the snapshot is safe to take at face value: a candidate whose citations cannot
    be READ declines `malformed_candidate` — a shape fault, caught BEFORE the totality
    walk — and `malformed_candidate` is not a review-routed reason. So every row that
    reaches the queue cited something the validator could read, which is exactly what
    `ValidationSnapshot.from_doc` relies on when it treats an empty pointer list as
    damage rather than as a legitimate shape."""
    candidates = InMemoryCandidateStore()
    audit = InMemoryAuditStore()
    raw = dict(_UNCOVERED)
    raw["evidence"] = [{"turn_ref": "not an index", "tool_call_ref": 42}]
    consumer = _consumer(candidates, turns=[scripted_turn([raw]) for _ in range(3)], audit=audit)

    await consumer._run_extractor(_summary(), KEEP_VERDICT, _proceeded())

    assert audit.snapshot_calls == 0
    assert candidates.put_calls == 0


# --- the gate's VERDICT, never the gate's consequences --------------------------------


class _UserFactScanner:
    """A semantic scanner that classifies every candidate as a legitimate per-user fact —
    the ONE classification that makes the gate write to another store."""

    async def scan(self, request):
        return SemanticScanResult(
            classification="user_fact",
            hits=(EntityHit(field="intent", kind="person", span="Jane Doe"),),
        )


class _RecordingUserStore:
    def __init__(self) -> None:
        self.commits: list[object] = []

    async def commit(self, record) -> None:
        self.commits.append(record)

    async def get(self, record_id):
        return None

    async def list_for_user(self, user_id, *, limit: int = 100):
        return []


async def test_the_declined_path_takes_the_verdict_and_writes_nothing_else() -> None:
    """THE SIDE-DOOR QUESTION, settled with a test rather than by reading the code.

    `LeakageGateStage.process` has a WRITE in it: a `reroute` verdict commits a per-user
    knowledge record. On the validated path that is the gate doing its job. On the
    declined path it would take an entity out of a candidate that FAILED validation — one
    that may never be completed, and that a reviewer may reject outright — and commit it
    to a user's durable store with nothing anywhere to retract it.

    So `_scan_declined` calls the gate's SCAN, not its process: the verdict is stamped
    (here `reroute`, faithfully) and the user store is untouched."""
    candidates = InMemoryCandidateStore()
    user_store = _RecordingUserStore()
    consumer = _consumer(
        candidates,
        turns=_uncovered_turns(),
        stages=(
            LeakageGateStage(
                candidate_store=candidates,
                semantic_scanner=_UserFactScanner(),
                user_store=user_store,
            ),
        ),
    )

    await consumer._run_extractor(_summary(), KEEP_VERDICT, _proceeded())

    env = candidates.all_candidates()[0]
    assert env.entity_scan["result"] == "reroute"  # the verdict IS honoured
    assert env.status == CandidateStatus.NEEDS_PARAMETERIZATION  # the routing is not
    assert user_store.commits == []  # and the write never happened


async def test_the_same_gate_on_the_validated_path_does_commit() -> None:
    """The control, so the test above is about the PATH and not about a gate that cannot
    reroute at all: the identical stage, over a candidate that passed validation, writes
    the per-user fact."""
    candidates = InMemoryCandidateStore()
    user_store = _RecordingUserStore()
    gate = LeakageGateStage(
        candidate_store=candidates,
        semantic_scanner=_UserFactScanner(),
        user_store=user_store,
    )
    consumer = _consumer(candidates, turns=[scripted_turn([_COVERED])], stages=(gate,))

    await consumer._run_extractor(_summary(), KEEP_VERDICT, _proceeded())

    assert candidates.all_candidates()[0].status == CandidateStatus.EXTRACTED
    assert len(user_store.commits) == 1


async def test_a_rule_predicate_mismatch_routes_the_same_way() -> None:
    """The second qualifying reason: the entry cites a REAL rule the catalog proves is a
    different filter. Also a form the reviewer can fix in seconds."""
    candidates = InMemoryCandidateStore()
    mismatched = blueprint_raw(
        intent="deductions per employee",
        source_refs=("tc1",),
        parameterization=[
            {
                "locator": {
                    "table": "dbpcm_warehouse.payroll",
                    "column": "register_type",
                    "value": "DDUCT,EARN",
                },
                "role": "inline",
                "why": "the ratio is defined over these two register types",
            },
            {
                "locator": {
                    "table": "dbpcm_warehouse.payroll",
                    "column": "register_type",
                    "value": "DDUCT",
                },
                "role": "rule",
                "rule_id": "gross_earnings",
            },
            {
                "locator": {
                    "table": "dbpcm_warehouse.payroll",
                    "column": "register_type",
                    "value": "EARN",
                },
                "role": "rule",
                "rule_id": "gross_earnings",
            },
        ],
    )
    consumer = _consumer(candidates, turns=[scripted_turn([mismatched]) for _ in range(3)])

    await consumer._run_extractor(_summary(), KEEP_VERDICT, _proceeded())

    stored = candidates.all_candidates()
    assert len(stored) == 1
    assert stored[0].decline.reason == "rule_predicate_mismatch"
    assert stored[0].status == CandidateStatus.NEEDS_PARAMETERIZATION


# --- and everything else keeps today's behaviour ------------------------------------


async def test_an_unscreened_session_writes_nothing() -> None:
    """The judge never ran (no judge wired / it raised / it skipped below the floor).
    `not_judged` is not a verdict, and the queue only takes work a model said was new."""
    candidates = InMemoryCandidateStore()
    consumer = _consumer(candidates, turns=_uncovered_turns())

    await consumer._run_extractor(_summary(), KEEP_VERDICT, JudgeOutcomeResult())

    assert candidates.put_calls == 0


async def test_a_skipped_judge_outcome_writes_nothing() -> None:
    """`skipped_no_prior_art` and its siblings mean the judgement did not HAPPEN. They
    look like a pass only if you read `drop=False` as merit."""
    candidates = InMemoryCandidateStore()
    consumer = _consumer(candidates, turns=_uncovered_turns())

    await consumer._run_extractor(
        _summary(), KEEP_VERDICT, JudgeOutcomeResult(outcome="skipped_no_prior_art")
    )

    assert candidates.put_calls == 0


async def test_a_merit_failed_decline_writes_nothing_even_with_a_proceeded_judge() -> None:
    """`no_evidence` is supposed to die. The judge's opinion is about the SESSION; this
    decline is about the candidate, and it has nothing a human could complete."""
    candidates = InMemoryCandidateStore()
    consumer = _consumer(candidates, turns=[scripted_turn([blueprint_raw(evidence=[])])])

    await consumer._run_extractor(_summary(), KEEP_VERDICT, _proceeded())

    assert candidates.put_calls == 0


async def test_no_judgement_at_all_is_the_pre_slice_path() -> None:
    """The S3-era callers (and every test that drives `_run_extractor` directly) pass no
    judgement. Absent is not `proceeded`."""
    candidates = InMemoryCandidateStore()
    consumer = _consumer(candidates, turns=_uncovered_turns())

    await consumer._run_extractor(_summary(), KEEP_VERDICT)

    assert candidates.put_calls == 0


# --- the guarantees around the persist ----------------------------------------------


async def test_only_one_review_item_is_written_per_extraction() -> None:
    """Bounded by design (decision doc §6). A session that produces several unfillable
    forms is a prompt problem; putting each one in front of a person is how the surface
    earns its own neglect. The LAST is kept — the model's final word."""
    candidates = InMemoryCandidateStore()
    first = dict(_UNCOVERED)
    first["payload"] = {**_UNCOVERED["payload"], "intent": "the first one"}
    second = dict(_UNCOVERED)
    second["payload"] = {**_UNCOVERED["payload"], "intent": "the last one"}
    consumer = _consumer(
        candidates, turns=[scripted_turn([first, second]) for _ in range(3)]
    )

    await consumer._run_extractor(_summary(), KEEP_VERDICT, _proceeded())

    stored = candidates.all_candidates()
    assert len(stored) == 1
    assert stored[0].payload["intent"] == "the last one"


async def test_a_reprocessed_session_replaces_its_stale_review_item() -> None:
    """`supersede(content_hash)` runs AFTER the persist, keeping only the ids this run
    wrote, so re-processing a session leaves ONE review item — the current one — rather than
    accumulating a row per run. The ordering is the point: superseding first would erase the
    prior generation before its replacement existed. The live case was processed three
    times."""
    candidates = InMemoryCandidateStore()
    summary = _summary(content_hash="hash-stable")

    for _ in range(3):
        consumer = _consumer(candidates, turns=_uncovered_turns())
        await consumer._run_extractor(summary, KEEP_VERDICT, _proceeded())

    assert len(candidates.all_candidates()) == 1


async def test_the_review_row_is_a_keeper_and_survives_the_sweep_beside_kept_candidates() -> None:
    """THE REVIEW ROW IS ON THE KEEPER LIST, and this is the arrangement that proves it.

    `_run_extractor` supersedes the prior generation AFTER publishing the new one, keeping
    every id this run wrote. The review row is written LAST, by `_persist_declined_for_review`,
    under an id from a different namespace (`review-0`, not `::N`) — so it is appended to the
    keeper list separately from the loop, and it is the one keeper that no `enumerate` would
    ever produce. Drop that append and the sweep deletes, microseconds after writing it, the
    single row this whole route exists to create.

    The other tests here run a decline ALONE, where a sweep that removed the review row would
    leave an empty store that looks much like "nothing qualified". Here the run produces BOTH
    a kept candidate and a review row, over a session that previously produced three kept
    candidates, so all three facts are separable in one assertion: the replacement `::0`
    survives, the review row survives, and the two genuine orphans (`::1`, `::2`) are swept.
    """
    candidates = InMemoryCandidateStore()
    summary = _summary(content_hash="hash-both")

    # Run 1: three ordinary, complete candidates and no decline at all.
    await _consumer(
        candidates, turns=[scripted_turn([_COVERED, _COVERED, _COVERED])]
    )._run_extractor(summary, KEEP_VERDICT, _proceeded())
    assert {c.candidate_id for c in candidates.all_candidates()} == {
        mint_candidate_id("hash-both", 0),
        mint_candidate_id("hash-both", 1),
        mint_candidate_id("hash-both", 2),
    }

    # Run 2: one candidate survives validation, one is corrected and then WITHDRAWN — the
    # merit-passed, form-failed decline that becomes the review row.
    await _consumer(
        candidates, turns=[scripted_turn([_COVERED, _UNCOVERED]), scripted_turn([])]
    )._run_extractor(summary, KEEP_VERDICT, _proceeded())

    stored = {c.candidate_id: c for c in candidates.all_candidates()}
    assert set(stored) == {
        mint_candidate_id("hash-both", 0),  # the one kept candidate of this run
        mint_review_candidate_id("hash-both", 0),  # the review row, NOT superseded
    }
    assert stored[mint_review_candidate_id("hash-both", 0)].status == (
        CandidateStatus.NEEDS_PARAMETERIZATION
    )
    assert stored[mint_candidate_id("hash-both", 0)].status == CandidateStatus.EXTRACTED
    # The sweep DID run — run 1's surplus ordinals are gone — so the review row's survival
    # is a keeper-list fact and not an absence of superseding.
    assert await candidates.get(mint_candidate_id("hash-both", 1)) is None
    assert await candidates.get(mint_candidate_id("hash-both", 2)) is None


async def test_with_no_leakage_stage_the_scan_stays_unsettled() -> None:
    """Fail CLOSED rather than fake a pass. An unsettled scan withholds the decline
    detail at the wire and blocks every approve path — a degraded review item, not a
    leak."""
    candidates = InMemoryCandidateStore()
    consumer = _consumer(candidates, turns=_uncovered_turns())

    await consumer._run_extractor(_summary(), KEEP_VERDICT, _proceeded())

    assert candidates.all_candidates()[0].entity_scan["result"] == "pending"


# --- the snapshot round-trips into something validation reads identically -----------


def test_a_snapshotted_summary_reconstructs_the_paths_validation_walks() -> None:
    """PARITY. The completion path re-validates against a summary rebuilt from the
    snapshot, so anything `to_candidate` or `GeneralizeStage` reads must survive the
    round trip byte-for-byte. `sql_by_ref` is the one that matters (both read it) and it
    is deliberately stored as its own RESULT — see `candidate/decline.py`."""
    from data_agent.learning.candidate.decline import ValidationSnapshot

    original = make_summary(
        tool_calls=(make_tool_call(ref="tc1", sql=_RATIO_SQL),),
        answer_sqls=(),
    )
    rebuilt = ValidationSnapshot.from_summary(original).to_summary()

    assert sql_by_ref(rebuilt) == sql_by_ref(original)
    assert rebuilt.accepted_signal == original.accepted_signal
    assert rebuilt.user_id == original.user_id
    assert rebuilt.content_hash == original.content_hash
    assert rebuilt.session_id == original.session_id
    assert rebuilt.trace_id == original.trace_id


def test_a_multi_designation_ref_survives_the_round_trip() -> None:
    """One `answerWithTable` can designate several queries, and the totality walk checks
    EVERY SQL a cited ref stands for. A round trip that collapsed them would let the
    second query's predicates through the D97 gate unexamined on the completion path —
    a silently dropped filter, which is the exact class the gate exists for."""
    from data_agent.learning.candidate.decline import ValidationSnapshot

    from .extractor.helpers import make_answer_sql

    second = "SELECT count() FROM dbpcm_warehouse.payroll WHERE register_type = 'EARN'"
    original = make_summary(
        tool_calls=(),
        answer_sqls=(
            make_answer_sql(_RATIO_SQL, ref="ans1"),
            make_answer_sql(second, ref="ans1"),
        ),
    )
    rebuilt = ValidationSnapshot.from_summary(original).to_summary()

    assert sql_by_ref(rebuilt) == sql_by_ref(original)
    assert sql_by_ref(rebuilt)["ans1"] == (_RATIO_SQL, second)


def test_a_snapshot_and_the_original_validate_identically() -> None:
    """The property the parity is FOR, asserted end to end: the same candidate, checked
    against the original summary and against the reconstruction, must reach the same
    verdict — the same decline for an incomplete plan, and the same acceptance for a
    complete one."""
    from data_agent.learning.candidate.decline import ValidationSnapshot
    from data_agent.learning.extractor.models import ExtractedCandidate
    from data_agent.learning.extractor.validation import to_candidate

    from .extractor.test_totality_hint import _COVERED

    original = _summary()
    rebuilt = ValidationSnapshot.from_summary(original).to_summary()

    for raw in (_UNCOVERED, _COVERED):
        before = to_candidate(raw, original, known_rules=_KNOWN, rule_index=_INDEX)
        after = to_candidate(raw, rebuilt, known_rules=_KNOWN, rule_index=_INDEX)
        if isinstance(before, ExtractedCandidate):
            assert isinstance(after, ExtractedCandidate)
            assert after.payload == before.payload
        else:
            assert before.reason == after.reason
            assert before.detail == after.detail
