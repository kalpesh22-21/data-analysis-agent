"""The ONE write path into a `global_knowledge` candidate's payload (design §B, §C.2, §F).

Everything here is about the four things `KnowledgeEditor.admit` does in order and the one thing
it deliberately does NOT do.

DOES: intake-validate with the reader the EXTRACTOR uses (so the closed key set — which is a
LEAKAGE rule, not a tidiness one — holds on the human path too); settle the leakage scan over
the NEW text and stamp it; land at `in_review` with a route reason; write only if the row has
not moved.

DOES NOT: run the gate's CONSEQUENCES. `_decide` calls a hard entity in a `global_knowledge`
payload a terminal reject and `reroute` commits a fact into the session user's private store —
right for an unattended pipeline, and on this path they would reject every promoted fact on
arrival and let a reviewer's keystroke write into somebody else's store. §B.1 owns that call;
what these tests pin is that dropping the consequences did NOT drop the VERDICT, because the
verdict is what the approve guard and the card's withholding rule both read.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.candidate.verdicts import EntityHit, LeakageVerdict
from data_agent.learning.inbox import ReviewInbox
from data_agent.learning.inbox.completion import CompletionRaceError
from data_agent.learning.inbox.knowledge_edit import (
    KnowledgeEditInputError,
    KnowledgeEditor,
    KnowledgeEditorUnavailableError,
)
from data_agent.learning.inbox.models import InboxItem, _leakage_cleared
from data_agent.learning.inbox.service import create_inbox_app
from data_agent.learning.leakage.gate import LeakageGateStage
from data_agent.learning.promotion.scheduler import _entity_scan_is_actionable

TOKEN = "reviewer-secret"
AUTH = {"X-Reviewer-Token": TOKEN}
CID = "candidate::kn-edit"

DIRTY = "employee E10842 accrues 1.5 days of leave per month"
CLEAN = "leave accrues at 1.5 days per month for full-time staff"


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_TOKEN", TOKEN)


def knowledge_candidate(**over: Any) -> CandidateEnvelope:
    fields: dict[str, Any] = {
        "candidate_id": CID,
        "type": "global_knowledge",
        "status": CandidateStatus.IN_REVIEW,
        "payload": {"statement": DIRTY, "knowledge_type": "business_rule"},
        "source_session": "sess-1",
        "source_trace": "trace-1",
        "evidence_refs": ("audit::1",),
        "extractor_rationale": "stated by the user",
        "entity_scan": LeakageVerdict(
            result="reject",
            hits=(EntityHit(field="statement", kind="employee_code", span="E10842"),),
            scanned_fields=("statement",),
            scanner="regex+ner",
        ).to_doc(),
        "confidence": 0.8,
        "proposed_action": "add",
        "depends_on": (),
        "content_hash": "hash-kn-edit",
    }
    fields.update(over)
    return CandidateEnvelope(**fields)


async def wired(*, stages: bool = True, env: CandidateEnvelope | None = None):
    """An inbox with a knowledge editor over the REAL S5 gate (or none, for the degraded case)."""
    store = InMemoryCandidateStore()
    await store.put(env if env is not None else knowledge_candidate())
    gate = (LeakageGateStage(candidate_store=store),) if stages else ()
    editor = KnowledgeEditor(store=store, stages=gate)
    return ReviewInbox(store, knowledge_editor=editor), store


async def client_for(inbox: ReviewInbox) -> TestClient:
    return TestClient(create_inbox_app(inbox=inbox, write_plane="offline"))


# --- intake validation ------------------------------------------------------


async def test_an_extra_key_is_refused_and_the_message_names_it() -> None:
    """⚠ THE CLOSED KEY SET IS A LEAKAGE RULE. The S5 gate scans exactly five surfaces, so a
    sixth key is text NOBODY SCANNED on its way to a reviewer's card and the global index. That
    is not a hypothetical: the stuck candidate this reader was written for carried `definition`
    and `intent`, and the gate reported `pass` on a payload it had never read."""
    inbox, store = await wired()
    with pytest.raises(KnowledgeEditInputError) as exc:
        await inbox.apply_knowledge_edit(
            CID, payload={"statement": CLEAN, "definition": "a rule about leave"}
        )
    assert "definition" in str(exc.value)
    # NOTHING WRITTEN. A refused payload must not have moved the row.
    assert (await store.get(CID)).payload["statement"] == DIRTY


async def test_a_blank_statement_is_refused_with_the_readers_own_sentence() -> None:
    """The sentence is the EXTRACTOR's, verbatim — a second vocabulary here would give the
    reviewer two error messages for one mistake and only one of them would name the fix."""
    inbox, _store = await wired()
    with pytest.raises(KnowledgeEditInputError) as exc:
        await inbox.apply_knowledge_edit(CID, payload={"statement": "   "})
    assert "statement" in str(exc.value)


async def test_a_wrongly_typed_surface_is_refused() -> None:
    """`related_terms` is an ARRAY. A string there lands as ONE term rather than several — the
    mapper is not indifferent to the shape, it just fails quietly, which is what the reader's
    type checks exist to make loud."""
    inbox, _store = await wired()
    with pytest.raises(KnowledgeEditInputError) as exc:
        await inbox.apply_knowledge_edit(
            CID, payload={"statement": CLEAN, "related_terms": "leave, accrual"}
        )
    assert "related_terms" in str(exc.value)


async def test_the_route_maps_an_intake_decline_to_422(enabled: None) -> None:
    inbox, _store = await wired()
    client = await client_for(inbox)
    resp = client.post(
        f"/inbox/{CID}/apply_knowledge",
        json={"payload": {"statement": CLEAN, "user_id": "u1"}},
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert "user_id" in resp.json()["detail"]


# --- the scan is re-settled over the NEW text -------------------------------


async def test_a_cleaned_fact_re_settles_to_pass_and_becomes_approvable() -> None:
    """⚠ THE SCAN IS MEASURED AGAINST WHAT IS BEING STORED. The stored verdict was settled about
    DIFFERENT content, and it is exactly what the card's withholding and the approve guard
    consult next — `_still_declined` makes the same move for the same reason."""
    inbox, store = await wired()
    result = await inbox.apply_knowledge_edit(CID, payload={"statement": CLEAN})

    assert result.entity_scan["result"] == "pass"
    after = await store.get(CID)
    assert after.status == CandidateStatus.IN_REVIEW
    assert after.entity_scan["result"] == "pass"
    assert _leakage_cleared(after) is True
    assert _entity_scan_is_actionable(after) is True


async def test_a_still_dirty_fact_is_stored_with_its_verdict_and_stays_withheld() -> None:
    """§B.1: the terminal reject is replaced by a HOLD only a human can clear. The row stays
    `in_review` with the verdict stamped, the card keeps withholding the flagged text, and the
    reviewer's next move is another edit — not an archive row."""
    inbox, store = await wired()
    result = await inbox.apply_knowledge_edit(
        CID, payload={"statement": "employee E99999 also accrues leave"}
    )

    assert result.entity_scan["result"] == "reject"
    after = await store.get(CID)
    assert after.status == CandidateStatus.IN_REVIEW
    assert _leakage_cleared(after) is False
    # The CARD withholds it — the projection is the rule, and the edit did not weaken it.
    item = InboxItem.from_envelope(after)
    assert "E99999" not in str(item.payload_view)


async def test_the_gates_consequences_never_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠ SCAN-ONLY (§B.1). The full gate would REJECT this candidate terminally and, on a
    `reroute` verdict, COMMIT a fact into the session user's private store — on a reviewer's
    keystroke, with nothing to retract it. `settle_entity_scan` calls `scan`, never `process`."""
    inbox, store = await wired()
    gate_process_called = False

    original = LeakageGateStage.process

    async def spy(self, env, ctx):
        nonlocal gate_process_called
        gate_process_called = True
        return await original(self, env, ctx)

    monkeypatch.setattr(LeakageGateStage, "process", spy)
    await inbox.apply_knowledge_edit(CID, payload={"statement": DIRTY})

    assert gate_process_called is False
    assert (await store.get(CID)).status == CandidateStatus.IN_REVIEW


async def test_with_no_stages_the_scan_is_the_pending_sentinel_and_approve_refuses() -> None:
    """⚠ A DEGRADED-BUT-HONEST OFFLINE POSTURE. Intake validation still runs (it needs no
    infra), but nothing scanned — so the sentinel is stamped and every approve guard fails
    closed on it. "Nobody looked" must never read as "cleared"."""
    inbox, store = await wired(stages=False)
    result = await inbox.apply_knowledge_edit(CID, payload={"statement": CLEAN})

    assert result.entity_scan["result"] == "pending"
    after = await store.get(CID)
    assert LeakageVerdict.is_settled(after.entity_scan) is False
    assert _entity_scan_is_actionable(after) is False


# --- the record ------------------------------------------------------------


async def test_the_edit_is_recorded_on_the_envelope_as_a_digest() -> None:
    """⚠ ADDITIVE, AND A DIGEST. It cannot go in the payload — the key set is closed, and the
    intake reader would decline it — and it cannot carry the previous TEXT, which is the very
    string the scan flagged, on a record the card renders. `sql_rewrite` records the previous
    query the same way for the same reason."""
    import hashlib

    inbox, store = await wired()
    await inbox.apply_knowledge_edit(CID, payload={"statement": CLEAN})

    after = await store.get(CID)
    assert after.knowledge_edit is not None
    assert after.knowledge_edit.edits == 1
    assert after.knowledge_edit.applied_at
    assert (
        after.knowledge_edit.previous_statement_sha256
        == hashlib.sha256(DIRTY.encode("utf-8")).hexdigest()
    )
    # THE PREVIOUS TEXT IS NOWHERE IN THE STORED DOCUMENT.
    assert DIRTY not in str(after.to_doc())


async def test_the_edit_count_climbs_across_edits() -> None:
    """"A reviewer touched this once" and "this has been rewritten five times" are different
    facts about the same row, and only the count keeps them apart."""
    inbox, store = await wired()
    await inbox.apply_knowledge_edit(CID, payload={"statement": CLEAN})
    await inbox.apply_knowledge_edit(CID, payload={"statement": CLEAN + " (revised)"})
    assert (await store.get(CID)).knowledge_edit.edits == 2


async def test_the_route_reason_says_which_surface_produced_the_row() -> None:
    inbox, store = await wired()
    await inbox.apply_knowledge_edit(CID, payload={"statement": CLEAN})
    assert (await store.get(CID)).route_reason == "knowledge_edited"


async def test_the_knowledge_edit_stamp_round_trips_and_is_tolerated_absent() -> None:
    """Additive + optional, like every other stamp on this envelope: a pre-slice document has
    no such key and must round-trip byte-identically."""
    plain = knowledge_candidate()
    assert "knowledge_edit" not in plain.to_doc()
    assert CandidateEnvelope.from_doc(plain.to_doc()).knowledge_edit is None

    inbox, store = await wired()
    await inbox.apply_knowledge_edit(CID, payload={"statement": CLEAN})
    doc = (await store.get(CID)).to_doc()
    assert CandidateEnvelope.from_doc(doc).knowledge_edit == (await store.get(CID)).knowledge_edit


async def test_a_junk_knowledge_edit_reads_as_absent_rather_than_raising() -> None:
    """NORMALIZE, DO NOT TRUST — the doc comes from a store humans can write through cbq, and an
    unreadable badge must not stop a review queue from listing."""
    doc = knowledge_candidate().to_doc()
    doc["knowledge_edit"] = "not a dict"
    assert CandidateEnvelope.from_doc(doc).knowledge_edit is None


# --- the guards ------------------------------------------------------------


async def test_a_blueprint_cannot_be_edited_through_this_surface() -> None:
    """⚠ THE TYPE GUARD IS NOT REDUNDANT WITH THE STATUS ONE. `in_review` holds blueprints too,
    and the editor validates ONLY against the knowledge reader — pointed at a blueprint it would
    replace a generalization, a template and a parameterization with five text fields and call
    it valid, because it never consulted the blueprint reader at all."""
    from data_agent.learning.inbox import InboxTransitionError

    env = knowledge_candidate(type="blueprint", payload={"intent": "x"})
    inbox, store = await wired(env=env)
    with pytest.raises(InboxTransitionError) as exc:
        await inbox.apply_knowledge_edit(CID, payload={"statement": CLEAN})
    assert "blueprint" in str(exc.value)
    assert (await store.get(CID)).payload == {"intent": "x"}


async def test_a_validated_candidate_cannot_be_edited() -> None:
    """Editing a LANDED node means re-landing it and resetting `verified`, which is a different
    operation with a different blast radius (§G defers it). `in_review` only."""
    from data_agent.learning.inbox import InboxTransitionError

    inbox, _store = await wired(env=knowledge_candidate(status=CandidateStatus.VALIDATED))
    with pytest.raises(InboxTransitionError):
        await inbox.apply_knowledge_edit(CID, payload={"statement": CLEAN})


async def test_the_route_maps_a_wrong_type_to_409(enabled: None) -> None:
    inbox, _store = await wired(env=knowledge_candidate(type="blueprint", payload={}))
    client = await client_for(inbox)
    resp = client.post(
        f"/inbox/{CID}/apply_knowledge", json={"payload": {"statement": CLEAN}}, headers=AUTH
    )
    assert resp.status_code == 409


async def test_an_unknown_candidate_is_a_404(enabled: None) -> None:
    inbox, _store = await wired()
    client = await client_for(inbox)
    resp = client.post(
        "/inbox/candidate::nope/apply_knowledge",
        json={"payload": {"statement": CLEAN}},
        headers=AUTH,
    )
    assert resp.status_code == 404


async def test_no_editor_wired_refuses_loudly_rather_than_doing_less() -> None:
    """A 200 that wrote nothing would leave a reviewer believing the corpus had been fixed."""
    store = InMemoryCandidateStore()
    await store.put(knowledge_candidate())
    inbox = ReviewInbox(store)
    with pytest.raises(KnowledgeEditorUnavailableError):
        await inbox.apply_knowledge_edit(CID, payload={"statement": CLEAN})


async def test_the_route_maps_a_missing_editor_to_503(enabled: None) -> None:
    store = InMemoryCandidateStore()
    await store.put(knowledge_candidate())
    client = await client_for(ReviewInbox(store))
    resp = client.post(
        f"/inbox/{CID}/apply_knowledge", json={"payload": {"statement": CLEAN}}, headers=AUTH
    )
    assert resp.status_code == 503


# --- the race --------------------------------------------------------------


async def test_a_row_that_moved_is_a_race_and_nothing_is_written() -> None:
    """The window is NARROWED, not closed — see `guarded_put`. What it catches is the wide one:
    the validate-and-scan, during which a colleague can reject the row. Resurrecting a REJECTED
    candidate re-enters work a human deliberately removed (D29)."""
    inbox, store = await wired()
    env = await store.get(CID)
    editor = KnowledgeEditor(store=store, stages=())
    await store.put(replace(env, status=CandidateStatus.REJECTED))

    with pytest.raises(CompletionRaceError):
        await editor.admit(env, payload={"statement": CLEAN}, route_reason="knowledge_edited")

    after = await store.get(CID)
    assert after.status == CandidateStatus.REJECTED
    assert after.payload["statement"] == DIRTY


async def test_the_route_maps_a_race_to_409_with_the_reason_verbatim(enabled: None) -> None:
    """The reviewer did nothing wrong and the only useful next step is to re-read the row."""

    class MovingStore(InMemoryCandidateStore):
        """A colleague rejects the row AFTER the inbox read it and BEFORE the guarded put.

        Counted rather than timed: the first `get` is the inbox's own status guard (which must
        SEE `in_review`, or the wrong branch fires and the test proves nothing about the race),
        and the second is `guarded_put`'s re-read.
        """

        gets = 0

        async def get(self, candidate_id: str):
            env = await super().get(candidate_id)
            type(self).gets += 1
            if env is not None and type(self).gets > 1:
                return replace(env, status=CandidateStatus.REJECTED)
            return env

    store = MovingStore()
    await store.put(knowledge_candidate())
    inbox = ReviewInbox(store, knowledge_editor=KnowledgeEditor(store=store, stages=()))
    client = await client_for(inbox)
    resp = client.post(
        f"/inbox/{CID}/apply_knowledge", json={"payload": {"statement": CLEAN}}, headers=AUTH
    )
    assert resp.status_code == 409
    assert "changed while" in resp.json()["detail"]


# --- the wire --------------------------------------------------------------


async def test_the_response_carries_the_verdict_without_the_span(enabled: None) -> None:
    """⚠ The span is the entity VALUE. Two responses now carry a verdict and neither goes
    through `InboxItem`, so `entity_scan_view` owns the blanking for both."""
    inbox, _store = await wired()
    client = await client_for(inbox)
    resp = client.post(
        f"/inbox/{CID}/apply_knowledge",
        json={"payload": {"statement": "employee E77777 gets leave"}},
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "edited"
    assert body["status"] == CandidateStatus.IN_REVIEW
    assert body["entity_scan"]["result"] == "reject"
    assert body["entity_scan"]["hits"] == [
        {"field": "statement", "kind": "employee_code"}
    ]
    assert "E77777" not in resp.text


async def test_a_clean_apply_answers_200_with_a_passing_scan(enabled: None) -> None:
    inbox, _store = await wired()
    client = await client_for(inbox)
    resp = client.post(
        f"/inbox/{CID}/apply_knowledge", json={"payload": {"statement": CLEAN}}, headers=AUTH
    )
    assert resp.status_code == 200
    assert resp.json()["entity_scan"] == {"result": "pass", "hits": []}
