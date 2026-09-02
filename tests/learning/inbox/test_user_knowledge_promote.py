"""K2 — a user's private fact, promoted to a `global_knowledge` candidate (design §D, §F).

Two things are being tested and they pull in opposite directions.

THE FEATURE: `user_knowledge` has auto-committed into a per-user store since D17 and NOTHING has
ever read it — not the agent at runtime, not the inbox tab drawn for it. The one thing a fact a
user taught the agent can do for anyone else is be lifted into the shared corpus, and this is the
button that does it.

THE BOUNDARY: D17 says a per-user fact is "surfaced only in that user's context", and this is the
deliberate exception to that — recorded, not hidden. So most of this file is about what keeps the
exception narrow: the reviewer must NAME the user (an empty id is a 400, never "all users"), the
record's owner must MATCH the id in the request (a record id is guessable), and the promoted row
is a CANDIDATE — the user's own fact is never altered, never deleted, never re-scoped.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import (
    CandidateStatus,
    mint_promoted_candidate_id,
    promoted_record_digest,
)
from data_agent.learning.candidate.verdicts import LeakageVerdict
from data_agent.learning.inbox import InboxTransitionError, ReviewInbox
from data_agent.learning.inbox.inbox import promoted_content_hash
from data_agent.learning.inbox.knowledge_edit import KnowledgeEditor
from data_agent.learning.inbox.models import InboxItem, _leakage_cleared
from data_agent.learning.inbox.service import create_inbox_app
from data_agent.learning.leakage.gate import PENDING_ENTITY_SCAN, LeakageGateStage
from data_agent.learning.promotion.scheduler import _entity_scan_is_actionable
from data_agent.learning.user.memory_user_store import InMemoryUserKnowledgeStore
from data_agent.learning.user.models import UserKnowledgeRecord, mint_record_id

TOKEN = "reviewer-secret"
AUTH = {"X-Reviewer-Token": TOKEN}

OWNER = "u-alice"
DIRTY_FACT = "employee E10842 in my team accrues 1.5 days of leave per month"
RECORD_ID = mint_record_id(OWNER, "candidate::orig-1")


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_TOKEN", TOKEN)


def user_record(**over: Any) -> UserKnowledgeRecord:
    fields: dict[str, Any] = {
        "record_id": RECORD_ID,
        "user_id": OWNER,
        "statement": DIRTY_FACT,
        "fact_type": "frequent_entity",
        # ⚠ THE LITERAL STRING `user` — what makes the record per-user, and what must NOT be
        # carried onto a knowledge chunk, where the same field becomes the node TITLE.
        "scope": "user",
        "structured": {"rate": "1.5 days per month"},
        "source_session": "sess-9",
        "source_trace": "trace-9",
        "evidence_refs": ("audit::9",),
        "committed_at": "2026-08-01T00:00:00+00:00",
    }
    fields.update(over)
    return UserKnowledgeRecord(**fields)


async def wired(*, records: list[UserKnowledgeRecord] | None = None, stages: bool = True):
    """An inbox with a per-user store, a knowledge editor and (optionally) the real S5 gate."""
    store = InMemoryCandidateStore()
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


async def client_for(inbox: ReviewInbox) -> TestClient:
    return TestClient(create_inbox_app(inbox=inbox, write_plane="offline"))


# --- the deterministic id ---------------------------------------------------


def test_the_promoted_candidate_id_is_derived_from_the_record_id() -> None:
    """DETERMINISM REPLACES A UNIQUENESS CHECK this surface cannot perform. "Promote" is one
    button on a list, and a double click, a stale tab or a redelivered proxy request all fire it
    twice; with a random id each press would file another review row for the same fact."""
    minted = mint_promoted_candidate_id(RECORD_ID)
    digest = hashlib.sha256(RECORD_ID.encode("utf-8")).hexdigest()
    assert minted == f"candidate::userpromote::{digest[:32]}"
    assert mint_promoted_candidate_id(RECORD_ID) == minted


def test_the_promoted_candidate_id_does_not_carry_the_user_id() -> None:
    """⚠ HASHED, NOT INTERPOLATED. A record id is `userknow::<user>::<candidate>` and a
    candidate id is rendered on cards, put in URLs and written to logs — interpolating would
    leak the user's identity through the one field every surface shows."""
    assert OWNER not in mint_promoted_candidate_id(RECORD_ID)
    assert RECORD_ID not in mint_promoted_candidate_id(RECORD_ID)


# --- the promotion ----------------------------------------------------------


async def test_the_record_fields_land_where_the_design_says() -> None:
    inbox, store, _users = await wired()
    result = await inbox.promote_user_knowledge(OWNER, RECORD_ID)

    env = await store.get(result.candidate_id)
    assert env.type == "global_knowledge"
    assert env.status == CandidateStatus.IN_REVIEW
    assert env.payload["statement"] == DIRTY_FACT
    assert env.payload["knowledge_type"] == "frequent_entity"
    assert env.payload["structured"] == {"rate": "1.5 days per month"}
    assert env.route_reason == "promoted_from_user"
    assert env.proposed_action == "promote_user_knowledge"
    assert env.confidence == 1.0
    assert env.depends_on == ()
    assert env.extractor_rationale == "promoted from user knowledge by a reviewer"
    # THE EVIDENCE TRAIL SURVIVES THE HOP — the difference between a promotion and a retyping.
    assert env.source_session == "sess-9"
    assert env.source_trace == "trace-9"
    assert env.evidence_refs == ("audit::9",)


async def test_scope_is_not_carried_because_the_two_fields_mean_different_things() -> None:
    """⚠ On a user record `scope` is the literal `"user"`; on a knowledge chunk it becomes the
    node TITLE (`knowledge_seed_from_candidate`). Copying it would title a shared corpus entry
    "user" — meaningless, and the exact opposite of what the field now claims."""
    inbox, store, _users = await wired()
    result = await inbox.promote_user_knowledge(OWNER, RECORD_ID)
    assert "scope" not in (await store.get(result.candidate_id)).payload


async def test_the_content_hash_has_its_own_namespace() -> None:
    """⚠ LOAD-BEARING, NOT TIDY. `supersede` sweeps on `content_hash`, so a re-extraction of the
    session this fact came from would delete every candidate carrying that session's hash — and
    a promoted row sharing it would vanish under a reviewer mid-review with nothing to say it
    had existed."""
    inbox, store, _users = await wired()
    result = await inbox.promote_user_knowledge(OWNER, RECORD_ID)
    env = await store.get(result.candidate_id)
    assert env.content_hash == f"userpromote::{promoted_record_digest(RECORD_ID)}"

    await store.supersede("sess-9")
    assert await store.get(result.candidate_id) is not None


def test_the_content_hash_does_not_carry_the_user_id_either() -> None:
    """⚠ THE SAME EXPOSURE THE CANDIDATE ID HASHES TO AVOID. `content_hash` used to interpolate
    the record id — `userpromote::userknow::<user_id>::<candidate_id>` — putting the owner's
    identity in cleartext on a stored, exported, log-visible field, on a row whose whole purpose
    is to stop being about one user. Same digest, same reason."""
    hashed = promoted_content_hash(RECORD_ID)
    assert OWNER not in hashed
    assert RECORD_ID not in hashed
    assert hashed.startswith("userpromote::")  # the namespace is the point, and it survives


async def test_the_stored_promoted_doc_carries_the_user_id_nowhere() -> None:
    """The property asserted where it matters: on the DOC, not on one field. `revalidation`
    legitimately carries the owner (a stage that scopes to a user must scope to the right one)
    and is never projected to the wire — everything else must be free of it."""
    inbox, store, _users = await wired()
    result = await inbox.promote_user_knowledge(OWNER, RECORD_ID)
    doc = (await store.get(result.candidate_id)).to_doc()
    doc.pop("revalidation", None)

    assert OWNER not in str(doc)
    assert RECORD_ID not in str(doc)


async def test_the_snapshot_carries_the_owners_user_id_not_the_reviewers() -> None:
    """A stage that scopes anything to a user must scope it to the person whose fact this was —
    not to the reviewer who pressed the button, and not to nobody."""
    inbox, store, _users = await wired()
    result = await inbox.promote_user_knowledge(OWNER, RECORD_ID)
    snapshot = (await store.get(result.candidate_id)).revalidation
    assert snapshot is not None
    assert snapshot.user_id == OWNER
    assert snapshot.to_summary().user_id == OWNER
    # MINIMAL: it carries three facts and asserts nothing about a session it never saw.
    assert snapshot.sql_by_ref == {}
    assert snapshot.evidence == ()


async def test_the_promoted_candidate_lists_on_the_global_knowledge_review_queue() -> None:
    """The whole point of the button: the fact now appears where a reviewer adjudicates shared
    knowledge."""
    inbox, _store, _users = await wired()
    result = await inbox.promote_user_knowledge(OWNER, RECORD_ID)
    items = await inbox.list(status=CandidateStatus.IN_REVIEW)
    assert [(it.candidate_id, it.type) for it in items] == [
        (result.candidate_id, "global_knowledge")
    ]


async def test_the_users_own_record_is_untouched() -> None:
    """A promotion produces a CANDIDATE. It never edits, deletes or re-scopes the fact the user
    actually taught the agent — this surface holds a read-only handle on that store."""
    inbox, _store, users = await wired()
    before = (await users.get(RECORD_ID)).to_doc()
    await inbox.promote_user_knowledge(OWNER, RECORD_ID)
    assert (await users.get(RECORD_ID)).to_doc() == before
    assert users.commit_calls == 1  # the seeding write, and nothing since


# --- idempotence ------------------------------------------------------------


async def test_a_second_press_returns_the_same_row_and_says_so() -> None:
    inbox, store, _users = await wired()
    first = await inbox.promote_user_knowledge(OWNER, RECORD_ID)
    second = await inbox.promote_user_knowledge(OWNER, RECORD_ID)

    assert second.candidate_id == first.candidate_id
    assert second.already is True
    assert first.already is False
    assert len(store.all_candidates()) == 1


async def test_a_second_press_never_moves_a_row_that_has_progressed() -> None:
    """"No duplicate, no status move, whatever status it is at" (§D.2). A reviewer who already
    approved this fact must not have it dragged back to `in_review` by somebody re-pressing a
    button on a stale tab."""
    inbox, store, _users = await wired()
    first = await inbox.promote_user_knowledge(OWNER, RECORD_ID)
    env = await store.get(first.candidate_id)
    await store.put(replace(env, status=CandidateStatus.VALIDATED))

    again = await inbox.promote_user_knowledge(OWNER, RECORD_ID)

    assert again.already is True
    assert again.status == CandidateStatus.VALIDATED
    assert (await store.get(first.candidate_id)).status == CandidateStatus.VALIDATED


# --- the boundary -----------------------------------------------------------


async def test_a_foreign_user_id_is_a_404() -> None:
    """⚠ A RECORD ID IS GUESSABLE — `userknow::<user>::<candidate>`. Without the owner check the
    button would promote any user's fact from a URL, which is a far wider read than the
    named-user listing it is supposed to sit on."""
    inbox, store, _users = await wired()
    with pytest.raises(InboxTransitionError) as exc:
        await inbox.promote_user_knowledge("u-mallory", RECORD_ID)
    assert "not found" in str(exc.value)
    assert store.all_candidates() == []


async def test_the_message_does_not_distinguish_absent_from_not_yours() -> None:
    """ONE message for both, so this endpoint cannot be used as an oracle for which record ids
    exist in other users' stores — the read the owner check exists to prevent."""
    inbox, _store, _users = await wired()
    with pytest.raises(InboxTransitionError) as absent:
        await inbox.promote_user_knowledge(OWNER, "userknow::u-alice::nope")
    with pytest.raises(InboxTransitionError) as foreign:
        await inbox.promote_user_knowledge("u-mallory", RECORD_ID)
    assert str(absent.value).split("record ")[1].split(" for")[0] != str(
        foreign.value
    ).split("record ")[1].split(" for")[0]
    # Same SHAPE of sentence, and neither says whether the record exists.
    for message in (str(absent.value), str(foreign.value)):
        assert message.endswith("not found")


async def test_the_route_maps_a_foreign_record_to_404(enabled: None) -> None:
    inbox, _store, _users = await wired()
    client = await client_for(inbox)
    resp = client.post(
        "/inbox/user_knowledge/promote",
        json={"user_id": "u-mallory", "record_id": RECORD_ID},
        headers=AUTH,
    )
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "body", [{"record_id": RECORD_ID}, {"user_id": OWNER}, {"user_id": "  ", "record_id": "x"}]
)
async def test_a_missing_id_is_a_400(enabled: None, body: dict) -> None:
    inbox, _store, _users = await wired()
    client = await client_for(inbox)
    resp = client.post("/inbox/user_knowledge/promote", json=body, headers=AUTH)
    assert resp.status_code == 400


# --- the listing ------------------------------------------------------------


async def test_the_listing_is_one_named_user_only() -> None:
    inbox, _store, _users = await wired(
        records=[user_record(), user_record(record_id="userknow::u-bob::c", user_id="u-bob")]
    )
    views = await inbox.list_user_knowledge(OWNER)
    assert [v.record.user_id for v in views] == [OWNER]


async def test_an_empty_user_id_is_refused_and_never_read_as_all_users() -> None:
    """⚠ THE ONE DEFAULT that decides whether this is a narrow exception or a bulk export."""
    inbox, _store, _users = await wired()
    with pytest.raises(ValueError, match="user_id is required"):
        await inbox.list_user_knowledge("")


async def test_the_route_requires_user_id(enabled: None) -> None:
    inbox, _store, _users = await wired()
    client = await client_for(inbox)
    assert client.get("/inbox/user_knowledge", headers=AUTH).status_code == 400
    assert client.get("/inbox/user_knowledge?user_id=", headers=AUTH).status_code == 400
    assert client.get("/inbox/user_knowledge?user_id=%20%20", headers=AUTH).status_code == 400


async def test_the_listing_reports_whether_each_record_is_already_promoted() -> None:
    """Read by the DETERMINISTIC id — one KV read per row, and the whole reason the id is
    derived from the record's. The card needs exactly this to decide whether the button is
    still live."""
    inbox, _store, _users = await wired()
    assert (await inbox.list_user_knowledge(OWNER))[0].promoted is None

    result = await inbox.promote_user_knowledge(OWNER, RECORD_ID)
    view = (await inbox.list_user_knowledge(OWNER))[0]
    assert view.promoted is not None
    assert view.to_wire()["promotion"] == {
        "candidate_id": result.candidate_id,
        "status": CandidateStatus.IN_REVIEW,
    }


async def test_the_wire_record_carries_what_the_card_renders(enabled: None) -> None:
    inbox, _store, _users = await wired()
    client = await client_for(inbox)
    resp = client.get(f"/inbox/user_knowledge?user_id={OWNER}", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["user_id"] == OWNER
    record = body["records"][0]
    assert record["record_id"] == RECORD_ID
    assert record["statement"] == DIRTY_FACT
    assert record["fact_type"] == "frequent_entity"
    assert record["scope"] == "user"
    assert record["structured"] == {"rate": "1.5 days per month"}
    assert record["committed_at"] == "2026-08-01T00:00:00+00:00"
    assert record["provenance"] == {"source_session": "sess-9", "source_trace": "trace-9"}
    assert record["promotion"] is None
    # The audit KV keys have no reader on this page, and a pointer offered with nothing to
    # follow it with is an invitation to add one.
    assert "evidence_refs" not in record


async def test_no_user_store_wired_is_a_503_not_an_empty_list(enabled: None) -> None:
    """"This user has no facts" and "nobody looked" must not be the same answer."""
    store = InMemoryCandidateStore()
    client = await client_for(ReviewInbox(store))
    resp = client.get(f"/inbox/user_knowledge?user_id={OWNER}", headers=AUTH)
    assert resp.status_code == 503


# --- the guards that must still hold ---------------------------------------


async def test_a_promoted_fact_arrives_flagged_and_the_card_withholds_its_text() -> None:
    """⚠ THE DESIGNED OUTCOME, not a failure. The fact was routed into the user store BECAUSE it
    carried an entity, so the scan will not pass — the row lands `in_review` with the verdict
    stamped, the card withholds the flagged text, and the knowledge assistant is the next click.
    """
    inbox, store, _users = await wired()
    result = await inbox.promote_user_knowledge(OWNER, RECORD_ID)

    assert result.entity_scan["result"] == "reject"
    assert result.entity_scan["hits"] == [
        {"field": "statement", "kind": "employee_code"}
    ]
    env = await store.get(result.candidate_id)
    assert _leakage_cleared(env) is False
    item = InboxItem.from_envelope(env)
    assert "E10842" not in str(item.payload_view)


async def test_the_promote_response_never_carries_the_span(enabled: None) -> None:
    inbox, _store, _users = await wired()
    client = await client_for(inbox)
    resp = client.post(
        "/inbox/user_knowledge/promote",
        json={"user_id": OWNER, "record_id": RECORD_ID},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert "E10842" not in resp.text
    assert resp.json()["already"] is False


async def test_an_unscanned_promotion_cannot_be_approved() -> None:
    """⚠ THE GUARD THAT ACTUALLY REFUSES. On an unsettled scan `_entity_scan_is_actionable`
    fails closed: nobody looked, machine or human, and the reviewer cannot supply a judgement
    the machine never made.

    ⚠ THE ROW IS PLANTED RATHER THAN PROMOTED THROUGH A GATELESS EDITOR, because design §F.1.a
    now refuses that promotion outright (`test_a_promote_with_no_scanner_is_refused_rather_
    than_degraded` in the QA file pins the refusal). The guard here is unchanged and still has
    to hold on its own: a store can hold an unsettled row that arrived by some other route, and
    approve is the layer that must fail closed however it got there.

    ⚠ NOTE WHAT THIS DOES *NOT* ASSERT, because the design doc (§B) overstates it. A SETTLED
    `reject`/`quarantine` verdict WITH hits IS actionable — `_entity_scan_is_actionable` returns
    True for a finding that localizes itself, deliberately, so that a leakage near-miss is not a
    permanently un-approvable dead end; the approve path then STRIPS those spans
    (`strip_entity_bearing`) before landing. So a promoted fact whose entity was localized can be
    approved by a human, with the entity removed on the way. Only an UNSETTLED scan, or a
    finding with NO spans, is refused. That guard is untouched by this slice.
    """
    inbox, store, _users = await wired()
    result = await inbox.promote_user_knowledge(OWNER, RECORD_ID)
    scanned = await store.get(result.candidate_id)
    await store.put(replace(scanned, entity_scan=dict(PENDING_ENTITY_SCAN)))
    env = await store.get(result.candidate_id)

    assert env.entity_scan["result"] == "pending"
    assert LeakageVerdict.is_settled(env.entity_scan) is False
    assert _entity_scan_is_actionable(env) is False


async def test_a_localized_finding_stays_actionable_which_is_the_existing_asymmetry() -> None:
    """The companion to the test above, pinning the CURRENT behaviour rather than the doc's
    description of it — so a later reader can see the difference was noticed, not missed."""
    inbox, store, _users = await wired()
    result = await inbox.promote_user_knowledge(OWNER, RECORD_ID)
    env = await store.get(result.candidate_id)
    assert env.entity_scan["result"] == "reject"
    assert _entity_scan_is_actionable(env) is True
    # ...while the CARD still withholds, which is the guard this slice actually relies on.
    assert _leakage_cleared(env) is False


async def test_editing_the_promoted_row_clears_the_scan_and_the_withholding() -> None:
    """THE WHOLE LOOP: promote → still flagged → edit → clean → approvable. K1 and K2 are the
    same operation from `admit` onward, and this is the sentence that says so."""
    inbox, store, _users = await wired()
    result = await inbox.promote_user_knowledge(OWNER, RECORD_ID)

    edited = await inbox.apply_knowledge_edit(
        result.candidate_id,
        payload={
            "statement": "leave accrues at 1.5 days per month for full-time staff",
            "knowledge_type": "business_rule",
        },
    )

    assert edited.entity_scan["result"] == "pass"
    env = await store.get(result.candidate_id)
    assert env.route_reason == "knowledge_edited"
    assert env.knowledge_edit is not None
    assert _leakage_cleared(env) is True
    assert DIRTY_FACT not in str(env.to_doc())


# --- §D.4: the shadowing trap ----------------------------------------------


async def test_the_promote_route_is_not_shadowed_by_the_candidate_one(enabled: None) -> None:
    """⚠ FastAPI MATCHES IN DECLARATION ORDER. Declared after `/inbox/{candidate_id}/promote`,
    this path would be swallowed by it: every press would look up a candidate literally named
    `user_knowledge` and answer 404 "candidate not found" — a message about the wrong noun, from
    the wrong handler, with the real route unreachable and nothing logged to say so.

    The assertion is not merely "it works". It is that the CANDIDATE route did not answer: a
    successful promotion carries `already`, which no candidate-promote response has, and a
    404 would carry the candidate route's sentence.
    """
    inbox, _store, _users = await wired()
    client = await client_for(inbox)
    resp = client.post(
        "/inbox/user_knowledge/promote",
        json={"user_id": OWNER, "record_id": RECORD_ID},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert "already" in resp.json()
    assert "user_knowledge" not in str(resp.json().get("candidate_id", ""))


async def test_the_user_knowledge_listing_is_not_shadowed_either(enabled: None) -> None:
    """`GET /inbox/user_knowledge` sits above nothing parametrised today — `/inbox/{id}` is not
    a GET route — but it is asserted anyway, because the ordering it depends on is the same one
    the promote route depends on and a future `GET /inbox/{candidate_id}` would break both."""
    inbox, _store, _users = await wired()
    client = await client_for(inbox)
    resp = client.get(f"/inbox/user_knowledge?user_id={OWNER}", headers=AUTH)
    assert resp.status_code == 200
    assert "records" in resp.json()


def test_the_route_table_declares_user_knowledge_before_the_parametrised_block() -> None:
    """The ordering ITSELF, asserted on the route table — so a reordering fails here with a
    message about declaration order rather than somewhere downstream with a 404 about a
    candidate nobody created."""
    app = create_inbox_app(inbox=ReviewInbox(InMemoryCandidateStore()), write_plane="offline")
    paths = [getattr(route, "path", "") for route in app.routes]
    assert paths.index("/inbox/user_knowledge/promote") < paths.index(
        "/inbox/{candidate_id}/promote"
    )
    assert paths.index("/inbox/user_knowledge") < paths.index("/inbox/{candidate_id}/approve")
