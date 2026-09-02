"""QA gaps around K2 — the promote button (knowledge-edit design §B, §D.2).

WHAT THIS FILE IS FOR. `test_user_knowledge_promote.py` covers the happy shapes: the
deterministic id, the second press, the owner check, where the record's fields land. What it
does NOT reach is the set of paths a real deployment gets to first — two reviewers pressing the
same button at the same instant, a record whose statement is blank, a record whose `structured`
is not the shape §D.2 assumes, and what a promoted row actually LANDS AS when somebody finally
approves it. Each of those is a place where "nothing was written" and "somebody else's row was
overwritten" look identical from the outside, so they are asserted rather than reasoned about.

⚠ THE APPROVE TEST IS THE POINT OF THE FILE. §B was CORRECTED after building to say that a
settled finding WITH spans is approvable and gets stripped on the way to `validated`. That
sentence is a claim about what reaches the shared corpus, and until this file nothing exercised
it end to end for a promoted row: the existing tests stop at `_entity_scan_is_actionable`,
which is the predicate, not the outcome. Here the row goes through a wired scheduler and a
landing writer, and the assertion is on the payload the writer was handed.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import (
    CandidateStatus,
    mint_promoted_candidate_id,
)
from data_agent.learning.candidate.verdicts import LeakageVerdict
from data_agent.learning.inbox import InboxTransitionError, ReviewInbox
from data_agent.learning.inbox.completion import CompletionRaceError
from data_agent.learning.inbox.knowledge_edit import (
    KnowledgeEditInputError,
    KnowledgeEditor,
    KnowledgeEditorUnavailableError,
)
from data_agent.learning.leakage.gate import PENDING_ENTITY_SCAN, LeakageGateStage
from data_agent.learning.promotion import PromotionScheduler
from data_agent.learning.user.memory_user_store import InMemoryUserKnowledgeStore
from data_agent.learning.user.models import UserKnowledgeRecord, mint_record_id

from ..promotion.helpers import (
    FakeDependencyResolver,
    FakeHitCountReader,
    FakeLandingWriter,
    FakeWarehouseProbe,
    promotion_policy,
)

OWNER = "u-alice"
DIRTY_FACT = "employee E10842 in my team accrues 1.5 days of leave per month"
RECORD_ID = mint_record_id(OWNER, "candidate::orig-1")


def user_record(**over: Any) -> UserKnowledgeRecord:
    fields: dict[str, Any] = {
        "record_id": RECORD_ID,
        "user_id": OWNER,
        "statement": DIRTY_FACT,
        "fact_type": "frequent_entity",
        "scope": "user",
        "structured": {"rate": "1.5 days per month"},
        "source_session": "sess-9",
        "source_trace": "trace-9",
        "evidence_refs": ("audit::9",),
        "committed_at": "2026-08-01T00:00:00+00:00",
    }
    fields.update(over)
    return UserKnowledgeRecord(**fields)


async def wired(
    *,
    records: list[UserKnowledgeRecord] | None = None,
    stages: bool = True,
    store: InMemoryCandidateStore | None = None,
):
    store = store if store is not None else InMemoryCandidateStore()
    users = InMemoryUserKnowledgeStore()
    for record in records if records is not None else [user_record()]:
        await users.commit(record)
    gate = (LeakageGateStage(candidate_store=store),) if stages else ()
    inbox = ReviewInbox(
        store,
        knowledge_editor=KnowledgeEditor(store=store, stages=gate),
        user_store=users,
    )
    return inbox, store, users


# --- the collision the deterministic id makes possible ----------------------


class _RacingStore(InMemoryCandidateStore):
    """A store where a COMPETING promote lands between the existence check and the write.

    The window is real and named in the source: `promote_user_knowledge` reads the id, finds
    nothing, and only then calls `admit`, which validates, scans and finally writes. Two
    reviewers looking at the same user's list hit exactly this — and because the id is
    DETERMINISTIC they collide on one row rather than filing two.

    The plant is armed only after the first `get` of the promoted id has already answered
    `None`, so this reproduces the interleaving rather than defeating the first check.
    """

    def __init__(self, competitor: Any) -> None:
        super().__init__()
        self._competitor = competitor
        self._contested_id = competitor.candidate_id
        self._armed = False

    async def get(self, candidate_id: str):  # type: ignore[override]
        if candidate_id != self._contested_id:
            return await super().get(candidate_id)
        if self._armed and self._competitor is not None:
            planted, self._competitor = self._competitor, None
            await super().put(planted)
        self._armed = True
        return await super().get(candidate_id)


async def test_a_concurrent_promote_of_the_same_record_is_a_race_not_an_overwrite() -> None:
    """⚠ THE `expect_absent` PATH, which nothing else exercises.

    `guarded_put(expect_absent=True)` re-reads the id and requires it to be STILL ABSENT. Without
    it the second promotion would `put` straight over the row the first reviewer created — same
    id, different envelope — and the loser would never know: both presses would answer 200 with
    identical bodies, because the id and the status are derived from the record, not from the
    row. The 409 is what makes "somebody else got there first" sayable.
    """
    competitor = replace(
        _promoted_row_shape(), route_reason="promoted_from_user", extractor_rationale="theirs"
    )
    store = _RacingStore(competitor)
    inbox, store, _users = await wired(store=store)

    with pytest.raises(CompletionRaceError) as exc:
        await inbox.promote_user_knowledge(OWNER, RECORD_ID)

    assert "was created while" in str(exc.value)
    # THE COMPETITOR'S ROW IS INTACT — this is the assertion the 409 exists for.
    landed = await store.get(competitor.candidate_id)
    assert landed.extractor_rationale == "theirs"
    assert len(store.all_candidates()) == 1


def _promoted_row_shape():
    """A stand-in for "the row the other reviewer just created", at the deterministic id."""
    from data_agent.learning.candidate.models import CandidateEnvelope

    return CandidateEnvelope(
        candidate_id=mint_promoted_candidate_id(RECORD_ID),
        type="global_knowledge",
        status=CandidateStatus.IN_REVIEW,
        payload={"statement": "somebody else's promotion of the same fact"},
        source_session="sess-9",
        source_trace="trace-9",
        evidence_refs=("audit::9",),
        extractor_rationale="theirs",
        entity_scan={"result": "pass", "hits": []},
        confidence=1.0,
        proposed_action="promote_user_knowledge",
        depends_on=(),
        content_hash=f"userpromote::{RECORD_ID}",
    )


@pytest.mark.parametrize(
    "status",
    [CandidateStatus.REJECTED, CandidateStatus.VALIDATED, CandidateStatus.PROMOTED],
)
async def test_a_second_press_over_a_terminal_row_returns_it_rather_than_reviving_it(
    status: str,
) -> None:
    """"No duplicate, no status move, WHATEVER STATUS IT IS AT" (§D.2), including the two
    terminal ones the existing test does not reach.

    `rejected` is the one that matters: a reviewer said no to this fact, and a stale tab
    re-pressing Promote must not put it back in front of the next reviewer as `in_review`. The
    `already` answer is what turns a resurrection into a report.
    """
    inbox, store, _users = await wired()
    first = await inbox.promote_user_knowledge(OWNER, RECORD_ID)
    env = await store.get(first.candidate_id)
    await store.put(replace(env, status=status))

    again = await inbox.promote_user_knowledge(OWNER, RECORD_ID)

    assert again.already is True
    assert again.status == status
    assert (await store.get(first.candidate_id)).status == status
    assert len(store.all_candidates()) == 1


# --- records the store can legally hold but §D.2's payload cannot ----------


@pytest.mark.parametrize("statement", ["", "   ", "\n\t "])
async def test_a_blank_statement_is_refused_and_nothing_is_written(statement: str) -> None:
    """⚠ A `user_knowledge` record with a blank statement IS reachable: `_user_knowledge_payload`
    requires a non-empty one at intake, but the store also holds rows written before that reader
    existed, and `UserKnowledgeRecord.from_doc` defaults `statement` to `""` for a doc missing it
    entirely.

    Promoting one must be a 422 carrying the intake reader's own sentence — NOT a
    `global_knowledge` candidate with an empty statement, which is a review row nobody can judge
    and which `knowledge_seed_from_candidate` would refuse at landing anyway, one gate too late.
    """
    inbox, store, _users = await wired(records=[user_record(statement=statement)])

    with pytest.raises(KnowledgeEditInputError) as exc:
        await inbox.promote_user_knowledge(OWNER, RECORD_ID)

    assert "statement" in str(exc.value)
    # NOTHING WAS WRITTEN — the validation runs before the row is created, so a bad record
    # leaves no half-built candidate behind for the next press to find as `already`.
    assert store.all_candidates() == []
    assert await store.get(mint_promoted_candidate_id(RECORD_ID)) is None


async def test_a_blank_statement_is_a_422_at_the_route(monkeypatch) -> None:
    """The same refusal through the wire. §D.2 and the route's docstring both promise 422 for
    "a record whose fields cannot form a legal knowledge payload".

    HISTORY, kept because the shape of the defect is the point. This was written as a strict
    xfail: the route answered 400, because its handler caught `ValueError` before anything
    narrower and `KnowledgeEditInputError` IS a `ValueError`. The source comment beside that
    branch claimed the case "cannot reach this frame — promote_user_knowledge raises it only
    from admit, which is below", which was not true: `admit` is called BY
    `promote_user_knowledge`, so the error propagates straight out into the handler.
    `apply_knowledge` had it right all along (its 422 branch is separate), so the two write
    surfaces answered differently for one identical refusal. The fix was one branch —
    `except KnowledgeEditInputError -> 422` above `except ValueError` — and this test now
    asserts it rather than predicting it.
    """
    from fastapi.testclient import TestClient

    from data_agent.learning.inbox.service import create_inbox_app

    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_TOKEN", "reviewer-secret")
    inbox, _store, _users = await wired(records=[user_record(statement="  ")])
    client = TestClient(create_inbox_app(inbox=inbox, write_plane="offline"))

    resp = client.post(
        "/inbox/user_knowledge/promote",
        json={"user_id": OWNER, "record_id": RECORD_ID},
        headers={"X-Reviewer-Token": "reviewer-secret"},
    )

    assert resp.status_code == 422
    assert "statement" in resp.json()["detail"]


@pytest.mark.parametrize(
    "structured",
    [
        None,
        {},
        ["rate", "1.5"],
        "rate=1.5",
        42,
    ],
)
async def test_a_structured_the_payload_cannot_carry_is_dropped_not_forwarded(
    structured: Any,
) -> None:
    """§D.2: `structured` is carried ONLY when it is a non-empty OBJECT.

    The record's field is typed `dict | None` but is REHYDRATED JSON, so a list or a bare string
    is reachable from the store. Intake declines a non-object `structured`, so forwarding one
    would turn a promotion into a 422 about a field the reviewer never typed and cannot fix —
    the button would be permanently broken for that record with no way to tell why. Dropping it
    loses supporting detail and keeps the statement, which is the part being promoted.
    """
    inbox, store, _users = await wired(records=[user_record(structured=structured)])

    result = await inbox.promote_user_knowledge(OWNER, RECORD_ID)

    env = await store.get(result.candidate_id)
    assert "structured" not in env.payload
    assert env.payload["statement"] == DIRTY_FACT


async def test_a_nested_structured_value_still_reaches_the_scanner() -> None:
    """⚠ THE SHAPE THE TOOL REFUSES AND INTAKE ALLOWS, checked for the thing that matters.

    `validate_payload` checks `structured` is an object and does NOT type its VALUES, so a
    nested object under it survives the promote path (the reviser's tool would have refused it,
    but a promoted record never went through the reviser). What must hold is that the entity
    buried in there is still SCANNED: `gate._collect_text` walks dicts and lists to any depth,
    so the hit is attributed to a dotted path and the row is flagged.

    If this ever stops holding, a promoted fact would carry an unscanned entity into a row a
    human can approve — the exact failure the closed key set exists to prevent, arriving through
    a value instead of a key.
    """
    inbox, store, _users = await wired(
        records=[
            user_record(
                statement="leave accrues monthly",
                structured={"detail": {"owner": "employee E10842"}},
            )
        ]
    )

    result = await inbox.promote_user_knowledge(OWNER, RECORD_ID)

    env = await store.get(result.candidate_id)
    assert env.payload["structured"] == {"detail": {"owner": "employee E10842"}}
    fields = {hit["field"] for hit in env.entity_scan["hits"]}
    assert "structured.detail.owner" in fields
    # The wire projection keeps the dotted field and drops the span, as everywhere else.
    assert "E10842" not in str(result.entity_scan)


async def test_a_record_with_no_evidence_refs_still_promotes() -> None:
    """A record committed before the audit trail existed carries no `evidence_refs`. That is a
    thinner provenance, not a reason to refuse — the statement is still the thing being judged."""
    inbox, store, _users = await wired(records=[user_record(evidence_refs=())])

    result = await inbox.promote_user_knowledge(OWNER, RECORD_ID)

    env = await store.get(result.candidate_id)
    assert env.evidence_refs == ()
    assert env.status == CandidateStatus.IN_REVIEW


# --- what a promoted row actually LANDS as (§B, corrected) -----------------


def _scheduler(store, *, landing_writer=None):
    return PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader({}),
        dependency_resolver=FakeDependencyResolver(),
        policy=promotion_policy(),
        landing_writer=landing_writer,
        require_landing=landing_writer is not None,
        clock=lambda: "2026-09-02T12:00:00+00:00",
    )


async def test_approving_a_localized_promotion_lands_the_statement_with_the_entity_stripped() -> (
    None
):
    """⚠ §B's CORRECTED SENTENCE, END TO END — the claim nothing else checks the OUTCOME of.

    A promoted user fact is entity-bearing by construction, so its scan settles `reject` with a
    localized span. §B says such a row IS approvable and that `strip_entity_bearing` removes the
    spans on the way to `validated`. Everything shipped stops one step short of proving it: the
    existing tests assert `_entity_scan_is_actionable(env) is True`, which is the PREDICATE.

    What a reviewer is actually promising when they press Approve is about the CORPUS, so this
    asserts on the envelope the landing writer was handed: the entity is gone from the payload,
    the statement around it survives (a strip that blanked the whole fact would be a different
    and much worse bug), and the span is handed to the writer as a forbidden-span tripwire.
    """
    inbox, store, _users = await wired()
    writer = FakeLandingWriter()
    scheduler = _scheduler(store, landing_writer=writer)
    result = await inbox.promote_user_knowledge(OWNER, RECORD_ID)
    env = await store.get(result.candidate_id)
    assert env.entity_scan["result"] == "reject"  # the premise of the whole test

    decision = await scheduler.apply_human_decision(env, "approve", probe=FakeWarehouseProbe())

    assert decision.action == "approve"
    assert (await store.get(result.candidate_id)).status == CandidateStatus.VALIDATED
    landed = [e for e in writer.landed if e.candidate_id == result.candidate_id]
    assert landed, "the promoted candidate never reached the landing writer"
    text = str(landed[-1].payload)
    assert "E10842" not in text
    assert "accrues" in text  # the fact survived the strip; only the entity went
    # The stored row is stripped too — the corpus and the review surface agree.
    assert "E10842" not in str((await store.get(result.candidate_id)).to_doc())


async def test_an_unscanned_promotion_is_refused_by_approve_rather_than_landed() -> None:
    """The other half of the corrected guard, asserted on the OUTCOME rather than the predicate:
    an unsettled scan means approve must HOLD — nothing reaches the landing writer, because with
    no spans the strip and the tripwire are both no-ops and the raw fact would cross into the
    corpus unexamined.

    ⚠ THE UNSCANNED ROW IS PLANTED, NOT PROMOTED, and that is a change of setup rather than of
    subject. This test originally reached the posture by promoting through an editor with no
    gate; design §F.1.a now REFUSES that promotion outright (see
    `test_a_promote_with_no_scanner_is_refused_rather_than_degraded`), so the row is written
    into the store directly instead. The guard being asserted is the same one, and it still
    has to hold: §F.1.a's refusal is one door, and a store can hold an unsettled row that came
    through another — a backup restore, a gate removed after the row was made, a future
    surface. Approve is the layer that must fail closed regardless of how the row got there.
    """
    inbox, store, _users = await wired()
    writer = FakeLandingWriter()
    scheduler = _scheduler(store, landing_writer=writer)
    result = await inbox.promote_user_knowledge(OWNER, RECORD_ID)
    scanned = await store.get(result.candidate_id)
    await store.put(replace(scanned, entity_scan=dict(PENDING_ENTITY_SCAN)))
    env = await store.get(result.candidate_id)
    assert LeakageVerdict.is_settled(env.entity_scan) is False  # the premise

    decision = await scheduler.apply_human_decision(env, "approve", probe=FakeWarehouseProbe())

    assert decision.action == "hold"
    assert writer.landed == []
    assert (await store.get(result.candidate_id)).status == CandidateStatus.IN_REVIEW


# --- §F.1.a: the posture that must never produce a row ----------------------


async def test_a_promote_with_no_scanner_is_refused_rather_than_degraded() -> None:
    """⚠ THE REACHABLE POSTURE §F.1.a CLOSES, and it is not a test-only wiring.

    Real `USER_KNOWLEDGE_*` credentials give the inbox a live per-user store; an unreadable
    catalog gives it no write-router pipeline, so `_build_completer` returns `None` and the
    editor is built with NO stages beside a store full of real facts. Admitting through it
    stamps the `pending` sentinel — which `inbox/models.py::_leakage_view` renders to the card
    as a green `pass` — and a raw per-user fact would then sit on the SHARED review queue under
    a verdict nobody reached.

    K2's input is entity-bearing BY CONSTRUCTION (the fact went to the private store precisely
    because it named someone), so the answer is a refusal, and NOTHING is written: a half-built
    row would be found by the next press as `already`.
    """
    inbox, store, _users = await wired(stages=False)

    with pytest.raises(KnowledgeEditorUnavailableError) as exc:
        await inbox.promote_user_knowledge(OWNER, RECORD_ID)

    message = str(exc.value)
    assert "entity-bearing by construction" in message
    assert "no leakage scanner is wired" in message
    assert "shared review queue" in message
    assert store.all_candidates() == []
    assert await store.get(mint_promoted_candidate_id(RECORD_ID)) is None


async def test_the_route_maps_the_missing_scanner_to_503(monkeypatch) -> None:
    """503, the same answer every other "this deployment has no such plane" gives — and NOT a
    422, which would tell the reviewer their input was wrong when the deployment is."""
    from fastapi.testclient import TestClient

    from data_agent.learning.inbox.service import create_inbox_app

    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_TOKEN", "reviewer-secret")
    inbox, _store, _users = await wired(stages=False)
    client = TestClient(create_inbox_app(inbox=inbox, write_plane="offline"))

    resp = client.post(
        "/inbox/user_knowledge/promote",
        json={"user_id": OWNER, "record_id": RECORD_ID},
        headers={"X-Reviewer-Token": "reviewer-secret"},
    )

    assert resp.status_code == 503
    # ⚠ THE SENTENCE REACHES THE REVIEWER. A bare "knowledge editing unavailable" reads as a
    # transient outage on a button they would then keep pressing; this one says the promotion
    # is refused, why, and that editing still works here.
    detail = resp.json()["detail"]
    assert "entity-bearing by construction" in detail
    assert "no leakage scanner is wired" in detail


async def test_k1_still_applies_an_edit_in_the_same_posture() -> None:
    """⚠ THE ASYMMETRY, PINNED. K1 is NOT refused when the scanner is missing: the reviewer
    typed the text and can re-read it, so an unscanned edit is degraded but HONEST, and the
    `pending` sentinel keeps the row unapprovable either way. Refusing it too would take the
    one available action away from an offline dev inbox in exchange for nothing.
    """
    inbox, store, _users = await wired()
    promoted = await inbox.promote_user_knowledge(OWNER, RECORD_ID)
    # The same store, now behind an editor with no stages — the §F.1.a posture, arrived at
    # after the row already exists.
    offline = ReviewInbox(
        store, knowledge_editor=KnowledgeEditor(store=store, stages=()), user_store=_users
    )

    result = await offline.apply_knowledge_edit(
        promoted.candidate_id,
        payload={"statement": "leave accrues at 1.5 days per month", "knowledge_type": "rule"},
    )

    assert result.entity_scan["result"] == "pending"
    env = await store.get(promoted.candidate_id)
    assert env.payload["statement"] == "leave accrues at 1.5 days per month"
    assert env.status == CandidateStatus.IN_REVIEW


async def test_an_unsettled_promoted_row_is_withheld_by_the_listing() -> None:
    """⚠ BELT AS WELL AS BRACES (§F.1.a). The refusal above is one door; this is what keeps the
    LISTING closed for a row some other path stored unscanned.

    `_leakage_view` renders an unsettled scan as a green `pass` — right for its own purpose,
    since the inbox must not assert a finding S5 never settled — so without the withholding the
    card would show a raw per-user statement under a verdict nobody reached, with no spans to
    redact and no decline to trigger the older rule.
    """
    from data_agent.learning.inbox.models import InboxItem

    inbox, store, _users = await wired()
    result = await inbox.promote_user_knowledge(OWNER, RECORD_ID)
    scanned = await store.get(result.candidate_id)
    await store.put(replace(scanned, entity_scan=dict(PENDING_ENTITY_SCAN)))
    env = await store.get(result.candidate_id)
    assert env.decline is None  # the older withholding rule does NOT fire here

    item = InboxItem.from_envelope(env)

    assert "E10842" not in str(item.payload_view)
    assert "E10842" not in item.summary
    assert "withheld" in str(item.payload_view)
    # The row still LISTS — only the unscanned content is held back.
    assert item.candidate_id == result.candidate_id
    assert item.type == "global_knowledge"


async def test_the_owner_check_uses_the_record_not_the_id_shape() -> None:
    """The owner check reads `record.user_id`, not the `userknow::<user>::…` prefix of the id.

    Worth pinning separately: a check written against the id STRING would pass for a record
    whose stored owner disagrees with its own key — which is exactly what a mis-keyed migration
    produces — and would then promote one person's fact under another's name.
    """
    forged = user_record(record_id=mint_record_id("u-mallory", "candidate::x"))
    inbox, store, _users = await wired(records=[forged])

    # The id SAYS mallory; the record SAYS alice. The record wins, both ways round.
    with pytest.raises(InboxTransitionError):
        await inbox.promote_user_knowledge("u-mallory", forged.record_id)
    result = await inbox.promote_user_knowledge(OWNER, forged.record_id)
    assert result.already is False
    assert (await store.get(result.candidate_id)).status == CandidateStatus.IN_REVIEW


# --- the edit badge belongs to EDITS (review fix) ---------------------------


async def test_a_promotion_carries_no_edit_badge() -> None:
    """⚠ NOBODY EDITED THIS ROW — the reviewer CREATED it, and creation is not an edit.

    `admit` used to stamp unconditionally, so a fresh promotion recorded `edits=1` against a
    `previous_statement_sha256` of the EMPTY string (the envelope's payload is empty by
    construction at that moment: `_promoted_envelope` leaves it for `admit` to write). The
    badge is the durable record of human revision on a card a later reviewer reads, and it
    was claiming a revision that never happened against a digest of nothing.
    """
    import hashlib

    inbox, store, _users = await wired()
    result = await inbox.promote_user_knowledge(OWNER, RECORD_ID)

    env = await store.get(result.candidate_id)
    assert env.knowledge_edit is None
    assert hashlib.sha256(b"").hexdigest() not in str(env.to_doc())


async def test_the_first_edit_after_a_promotion_is_the_first_edit() -> None:
    """The consequence that made the miscount visible: with the promotion stamped, a reviewer's
    first real correction read `edits=2` and pointed its digest at the empty string rather than
    at the statement it replaced."""
    inbox, store, _users = await wired()
    result = await inbox.promote_user_knowledge(OWNER, RECORD_ID)

    await inbox.apply_knowledge_edit(
        result.candidate_id, payload={"statement": "leave accrues at 1.5 days per month"}
    )

    badge = (await store.get(result.candidate_id)).knowledge_edit
    assert badge is not None
    assert badge.edits == 1
    # The digest is of the statement actually REPLACED — the promoted one, not the empty string.
    import hashlib

    assert badge.previous_statement_sha256 == hashlib.sha256(
        DIRTY_FACT.encode("utf-8")
    ).hexdigest()
    # ...and it is a digest, never the text.
    assert DIRTY_FACT not in str(badge)
