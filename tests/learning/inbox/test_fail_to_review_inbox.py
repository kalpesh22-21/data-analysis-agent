"""The fail-to-review REVIEW SURFACE: listing, rendering, and completing a
`needs_parameterization` candidate (`docs/decisions/learning-declined-candidate-review.md`).

Three layers have to learn one status or the row is written and never seen — the store
projection, the inbox service's wire shape, and the reason label — so all three are
pinned here together. Plus the half that makes the surface worth having: the reviewer
fills in the missing entries, the candidate RE-VALIDATES in full and re-runs the write
router, and a form that still does not cover the accepted SQL comes back declined with
the fresh reason rather than landing.

The two withholding rules are the sharp edges and each has its own test:

  * the decline DETAIL names predicates and their literal values, so it crosses to a
    browser only when the persisted leakage verdict is a settled clean `pass`;
  * an UNSETTLED scan withholds too, and for the stronger reason — `_leakage_view`
    renders "nobody scanned" as `result="pass"` for the reviewer's benefit, and a
    gate that read that rendering would open on the exact case where nothing is known.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from data_agent.catalog.loader import build_sqlglot_schema_from_catalog
from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import (
    CandidateStatus,
    build_declined_envelope,
    mint_review_candidate_id,
)
from data_agent.learning.extractor.models import Decline
from data_agent.learning.generalize import GeneralizeStage
from data_agent.learning.inbox import (
    InboxTransitionError,
    ParameterizationCompleter,
    ReviewInbox,
)
from data_agent.learning.inbox.completion import CompletionInputError, CompletionRaceError
from data_agent.learning.inbox.models import InboxItem
from data_agent.learning.inbox.service import create_inbox_app
from data_agent.learning.leakage import LeakageGateStage
from data_agent.learning.writer import WriterStage
from data_agent.learning.writer.routing import derive_inbox_reason
from tests._catalog_fixture import fixture_catalog

from ..extractor.helpers import (
    PAYROLL_SQL,
    blueprint_raw,
    make_summary,
    make_tool_call,
    param_slot,
    payroll_parameterization,
)

# Approving REPLAYS against the live warehouse, and this surface mints no tokens — the
# reviewer pastes one. Any non-blank string does here; `probe_factory` decides what the replay
# actually runs through, which is the fake the test already wired.
REVIEWER_TRIAL_TOKEN = "reviewer-pasted-token"


TOKEN = "reviewer-secret"
AUTH = {"X-Reviewer-Token": TOKEN}

_CATALOG = fixture_catalog()

# The live SHAPE, on the worked payroll example: the model classified every literal
# predicate of the accepted SQL except one, and the corrective rounds did not recover
# it. The reviewer's whole task is the fourth entry.
_MISSING_REGION = payroll_parameterization()[:3]
_HUMAN_ENTRIES = [
    param_slot(
        "region", slot_type="entity", required=False, optional_pattern="TRUE", value="NA"
    )
]
_UNCOVERED = blueprint_raw(parameterization=_MISSING_REGION, source_refs=("tc1",))

_DECLINE_DETAIL = (
    "candidate.payload.parameterization has no entry for 1 literal predicate(s) of the "
    "accepted SQL:\n  - region = 'NA' — no catalog rule declares this predicate"
)


def _summary():
    return make_summary(
        tool_calls=(make_tool_call(ref="tc1", sql=PAYROLL_SQL),), content_hash="hash-ratio"
    )


def _declined_envelope(*, scan_result: str | None = "pass"):
    """A persisted fail-to-review candidate. *scan_result* `None` leaves the S3 `pending`
    sentinel — the "nobody scanned" case."""
    env = build_declined_envelope(
        Decline(
            type="blueprint",
            reason="totality_violation",
            detail=_DECLINE_DETAIL,
            correctable=True,
            corrections_attempted=2,
            correction_history=("first correction", "second correction"),
            raw_payload=_UNCOVERED,
        ),
        _summary(),
        candidate_id=mint_review_candidate_id("hash-ratio", 0),
    )
    if scan_result is None:
        return env
    return replace(
        env,
        entity_scan={
            "result": scan_result,
            "hits": [],
            "scanned_fields": ["intent"],
            "scanner": "regex+ner",
        },
    )


async def _store_with(env) -> InMemoryCandidateStore:
    store = InMemoryCandidateStore()
    await store.put(env)
    return store


def _completer(store, *, stages=()):
    """The completion plane. `known_rules` is empty here because this candidate cites no
    catalog rule — what it must NOT be is a different catalog from the extractor's, which
    is the wiring the composition root enforces (`service.py::_build_completer`)."""
    return ParameterizationCompleter(
        store=store, known_rules=frozenset(), rule_index=None, stages=stages
    )


def _full_stages(store):
    """Generalize → leakage → writer: the blueprint half of the frozen order. Dedup is
    omitted here (it needs an embedder and a corpus); what this fixture is for is that
    the completed candidate goes through the ROUTER rather than around it."""
    return (
        GeneralizeStage(catalog_schema=build_sqlglot_schema_from_catalog(_CATALOG)),
        LeakageGateStage(candidate_store=store),
        WriterStage(sampler=lambda env: False),
    )


# --- the label ----------------------------------------------------------------------


def test_the_reason_is_derived_from_the_decline_block_not_the_status() -> None:
    """Keyed on the BLOCK: a row carrying a decline is a form to complete whatever else
    is true about it, and it never passed through `route_candidate` at all — so every
    rule below the new branch would be answering a question nobody asked of it."""
    env = _declined_envelope()
    assert derive_inbox_reason(env) == "needs_parameterization"
    # And a quarantined scan does NOT relabel it `leakage_near_miss`: the reviewer's
    # task is unchanged, and the scan finding is already on the row.
    assert derive_inbox_reason(_declined_envelope(scan_result="quarantine")) == (
        "needs_parameterization"
    )


def test_an_ordinary_candidate_is_unaffected() -> None:
    env = replace(_declined_envelope(), decline=None, type="global_knowledge")
    assert derive_inbox_reason(env) == "knowledge_pre_gate"


# --- the projection + the withholding rule -------------------------------------------


def test_a_cleared_scan_carries_the_detail_the_reviewer_needs() -> None:
    view = InboxItem.from_envelope(_declined_envelope()).decline_view()
    assert view["reason"] == "totality_violation"
    assert "region = 'NA'" in view["detail"]
    assert view["detail_withheld"] is False
    assert view["corrections_attempted"] == 2
    assert view["correction_history"] == ["first correction", "second correction"]


@pytest.mark.parametrize("scan_result", ["quarantine", "reroute"])
def test_an_uncleared_scan_withholds_the_detail_and_says_so(scan_result: str) -> None:
    """The scanners found something they could not clear. Shipping a sentence full of
    the same session's literals to a browser at that moment would route around the one
    gate that noticed — but the SHAPE survives, so the row still says it has something
    to say."""
    view = InboxItem.from_envelope(_declined_envelope(scan_result=scan_result)).decline_view()
    assert view["detail"] == ""
    assert view["detail_withheld"] is True
    assert view["correction_history"] == []
    assert view["reason"] == "totality_violation"
    assert view["corrections_attempted"] == 2


def test_an_unsettled_scan_withholds_too() -> None:
    """The stronger case: nobody looked. `_leakage_view` renders an unsettled scan as
    `pass` for the reviewer's benefit, so a gate reading THAT would open on the one
    state in which nothing is known."""
    view = InboxItem.from_envelope(_declined_envelope(scan_result=None)).decline_view()
    assert view["detail_withheld"] is True


def test_an_ordinary_row_has_no_decline_view() -> None:
    env = replace(_declined_envelope(), decline=None)
    assert InboxItem.from_envelope(env).decline_view() is None


# --- the HTTP surface -----------------------------------------------------------------


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_TOKEN", TOKEN)


async def _client(*, scan_result: str | None = "pass", stages=(), with_completer=True):
    store = await _store_with(_declined_envelope(scan_result=scan_result))
    inbox = ReviewInbox(
        store,
        completer=_completer(store, stages=stages(store) if callable(stages) else stages)
        if with_completer
        else None,
    )
    return TestClient(create_inbox_app(inbox=inbox, write_plane="offline")), store


async def test_the_status_is_listable_and_the_row_carries_its_decline(enabled) -> None:
    client, _ = await _client()
    resp = client.get("/inbox", params={"status": "needs_parameterization"}, headers=AUTH)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["status"] == "needs_parameterization"
    assert item["reason"] == "needs_parameterization"
    assert "region = 'NA'" in item["decline"]["detail"]
    assert item["decline"]["detail_withheld"] is False


async def test_a_withheld_detail_never_crosses_the_wire(enabled) -> None:
    client, _ = await _client(scan_result="quarantine")
    resp = client.get("/inbox", params={"status": "needs_parameterization"}, headers=AUTH)
    body = resp.text
    item = resp.json()["items"][0]
    assert item["decline"]["detail"] == ""
    assert item["decline"]["detail_withheld"] is True
    # Not merely absent from the projected field — absent from the RESPONSE.
    assert "region = 'NA'" not in body


async def test_the_review_queue_listing_is_unaffected(enabled) -> None:
    """A fail-to-review row is NOT in the `in_review` queue: the reviewer's task is a
    different one, and mixing them is what the separate status exists to prevent."""
    client, _ = await _client()
    resp = client.get("/inbox", headers=AUTH)
    assert resp.json()["items"] == []


# --- completing the form ---------------------------------------------------------------


async def test_a_completed_form_re_validates_and_re_runs_the_pipeline(enabled) -> None:
    """The happy path end to end: three entries, full re-validation, and the candidate
    leaves the review status through the ROUTER rather than around it — its
    `generalization` is stamped, its entity scan is re-settled against the CHANGED
    payload, and the decline block is gone."""
    client, store = await _client(stages=_full_stages)
    cid = mint_review_candidate_id("hash-ratio", 0)

    resp = client.post(f"/inbox/{cid}/complete", json={"entries": _HUMAN_ENTRIES}, headers=AUTH)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "completed"
    assert body["status"] != CandidateStatus.NEEDS_PARAMETERIZATION
    assert body["decline"] is None

    env = await store.get(cid)
    assert env.status != CandidateStatus.NEEDS_PARAMETERIZATION
    assert env.decline is None
    # The SNAPSHOT is kept, unlike the decline. A decline left here would render an
    # outstanding task for ever; the snapshot describes the SESSION, which a completion
    # does not alter — and a kept candidate now carries its own `ValidationSnapshot` (`build_candidate_envelope`), and a completion no longer clears it — that is what makes the review queue editable at all (design §C.4).
    assert env.revalidation is not None
    # ADDITIVE CONTRACT: this row was built with NO `evidence_refs` — the shape every
    # review item written before the audit snapshot was wired has — and it still
    # completes, because the citations are rebuilt from the snapshot's entity-free
    # pointers and the quote is never the candidate store's to hold.
    assert env.evidence_refs == ()
    assert "generalization" in env.payload  # the pipeline ran, it was not bypassed
    assert env.entity_scan["result"] != "pending"  # re-scanned against the new payload
    assert len(env.payload["parameterization"]) == 4


async def test_a_still_incomplete_form_declines_and_the_row_stays_put(enabled) -> None:
    """A reviewer cannot hand-wave a predicate into coverage. Two of the three entries
    leaves one predicate uncovered, so the D97 totality walk declines exactly as the
    model's did — 200 with the FRESH detail (it is a result the reviewer must read, not
    an error), and the candidate is untouched in status."""
    client, store = await _client(stages=_full_stages)
    cid = mint_review_candidate_id("hash-ratio", 0)

    resp = client.post(f"/inbox/{cid}/complete", json={"entries": []}, headers=AUTH)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "declined"
    assert body["status"] == CandidateStatus.NEEDS_PARAMETERIZATION
    assert body["decline"]["reason"] == "totality_violation"
    assert "region = 'NA'" in body["decline"]["detail"]

    env = await store.get(cid)
    assert env.status == CandidateStatus.NEEDS_PARAMETERIZATION
    # The reviewer's work is kept so the next attempt starts from it, and the FRESH
    # decline replaces the stale one.
    assert len(env.payload["parameterization"]) == 3
    assert env.decline.reason == "totality_violation"
    # The correction COUNT still records what the MODEL was asked — a human's attempt is
    # not a corrective turn.
    assert env.decline.corrections_attempted == 2


async def test_replace_mode_rewrites_the_whole_array(enabled) -> None:
    """The `rule_predicate_mismatch` shape: an entry is WRONG, and no amount of
    appending fixes it."""
    client, store = await _client()
    cid = mint_review_candidate_id("hash-ratio", 0)

    resp = client.post(
        f"/inbox/{cid}/complete",
        json={"entries": payroll_parameterization(), "replace": True},
        headers=AUTH,
    )

    assert resp.json()["outcome"] == "completed", resp.text
    assert len((await store.get(cid)).payload["parameterization"]) == 4


async def test_entries_that_are_not_a_parameterization_array_are_422(enabled) -> None:
    client, _ = await _client()
    cid = mint_review_candidate_id("hash-ratio", 0)
    resp = client.post(f"/inbox/{cid}/complete", json={"entries": ["not an object"]}, headers=AUTH)
    assert resp.status_code == 422


async def test_completion_without_a_validation_plane_is_503(enabled) -> None:
    """Honest degrade, mirroring the landing-plane 503: this deployment cannot
    re-validate, so it says so rather than implying the reviewer's form was wrong."""
    client, _ = await _client(with_completer=False)
    cid = mint_review_candidate_id("hash-ratio", 0)
    resp = client.post(f"/inbox/{cid}/complete", json={"entries": []}, headers=AUTH)
    assert resp.status_code == 503


async def test_completing_an_ordinary_review_item_is_refused(enabled) -> None:
    """The two surfaces cannot be crossed: `complete` is guarded on
    `needs_parameterization` so it can never become a second, unvalidated route into an
    ordinary candidate's payload."""
    store = await _store_with(
        replace(_declined_envelope(), status=CandidateStatus.IN_REVIEW, decline=None)
    )
    inbox = ReviewInbox(store, completer=_completer(store))
    client = TestClient(create_inbox_app(inbox=inbox, write_plane="offline"))
    resp = client.post(
        f"/inbox/{mint_review_candidate_id('hash-ratio', 0)}/complete",
        json={"entries": []},
        headers=AUTH,
    )
    assert resp.status_code == 409


# --- the other transitions --------------------------------------------------------------


async def test_a_fail_to_review_row_can_be_rejected() -> None:
    """It writes no content, and gating it would leave the row with NO terminal action —
    completable only by a human who may have decided the form has no honest answer, and
    otherwise clearable only by waiting out a 180-day TTL."""
    store = await _store_with(_declined_envelope())
    inbox = ReviewInbox(store)
    env = await inbox.reject(mint_review_candidate_id("hash-ratio", 0))
    assert env.status == CandidateStatus.REJECTED


async def test_it_cannot_be_approved() -> None:
    """Approve is `in_review` only. A form that has not been completed has no validated
    parameterization, and approving one would be exactly the bypass the whole path
    exists to refuse."""
    store = await _store_with(_declined_envelope())
    inbox = ReviewInbox(store)
    with pytest.raises(InboxTransitionError, match="needs_parameterization"):
        await inbox.approve(
            mint_review_candidate_id("hash-ratio", 0), token=REVIEWER_TRIAL_TOKEN
        )


async def test_a_candidate_with_no_snapshot_refuses_rather_than_guesses() -> None:
    """A review item whose snapshot did not survive (a hand edit, a foreign writer)
    cannot be checked against the accepted SQL it must cover, and re-validating against
    an invented summary would decline every entry `unrewritable_sql` — blaming the
    model's SQL for a storage fault."""
    store = await _store_with(replace(_declined_envelope(), revalidation=None))
    inbox = ReviewInbox(store, completer=_completer(store))
    with pytest.raises(Exception, match="completion_unavailable"):
        await inbox.complete_parameterization(
            mint_review_candidate_id("hash-ratio", 0), entries=_HUMAN_ENTRIES
        )


async def test_a_completion_the_pipeline_drops_still_leaves_the_queue() -> None:
    """THE ZOMBIE, closed. A stage may answer `drop` — S6 dedup does exactly that when the
    completed blueprint's canonical key already exists, which is GUARANTEED for a
    re-processed session whose blueprint landed on the earlier run. `drop` means "do not
    persist the enriched envelope", and on the extraction path that is harmless because
    the row was already written at `extracted` before the stages ran.

    Here there IS a row, and it says `needs_parameterization`. Leaving it there while
    answering "completed" produced a row that relisted for ever and bumped the corpus
    counter again on every re-completion. So the completion persists whatever the pipeline
    left, at a status that is never the review one: the store and the response agree, and
    the row is where the consumer's dropped candidates sit."""

    class _DroppingStage:
        stage_id = "dedup"

        async def process(self, env, ctx):
            from data_agent.learning.candidate.verdicts import DedupVerdict
            from data_agent.learning.stage import StageResult

            return StageResult(
                replace(
                    env,
                    dedup=DedupVerdict(
                        canonical_key="k",
                        matched_id="bp-already-landed",
                        similarity=1.0,
                        action="increment",
                        layer="hard",
                    ),
                ),
                "drop",
            )

    store = await _store_with(_declined_envelope())
    inbox = ReviewInbox(store, completer=_completer(store, stages=(_DroppingStage(),)))
    cid = mint_review_candidate_id("hash-ratio", 0)

    result = await inbox.complete_parameterization(cid, entries=_HUMAN_ENTRIES)

    assert result.outcome == "completed"
    assert result.envelope.status != CandidateStatus.NEEDS_PARAMETERIZATION
    stored = await store.get(cid)
    # The store agrees with the response — that is the invariant that broke.
    assert stored.status == result.envelope.status
    assert stored.status == CandidateStatus.EXTRACTED
    assert stored.decline is None
    # And the verdict that explains why nothing landed is kept.
    assert stored.dedup is not None and stored.dedup.action == "increment"
    # It is gone from every listable surface — no relist, no second increment.
    assert await store.list_by_status(CandidateStatus.NEEDS_PARAMETERIZATION) == []


async def test_an_unscanned_form_withholds_its_payload_as_well_as_its_detail() -> None:
    """The other half of the withholding rule. On an UNSETTLED scan the redaction has no
    spans to key off, so `payload_view` and `summary` would ship the raw payload verbatim
    — withholding the decline sentence while handing over the same session's literals one
    field down. A decline-bearing row that nobody scanned therefore withholds its CONTENT
    too, and says why."""
    from data_agent.learning.inbox.models import UNSCANNED_NOTICE

    item = InboxItem.from_envelope(_declined_envelope(scan_result=None))

    assert item.payload_view == {"withheld": UNSCANNED_NOTICE}
    assert item.summary == UNSCANNED_NOTICE
    assert item.decline_view()["detail_withheld"] is True
    # The row still LISTS — a reviewer must know the work exists — with its reason and
    # its counts intact.
    assert item.reason == "needs_parameterization"
    assert item.decline_view()["corrections_attempted"] == 2


async def test_a_scanned_form_shows_its_payload_normally() -> None:
    """The control: a settled scan means the redaction machinery works, so the ordinary
    entity-free view is what a reviewer gets. The withholding is keyed on "nobody looked",
    not on "this is a form"."""
    item = InboxItem.from_envelope(_declined_envelope())
    assert "withheld" not in item.payload_view
    assert item.payload_view["intent"] == _UNCOVERED["payload"]["intent"]


async def test_an_ordinary_unsettled_row_is_unaffected() -> None:
    """Scoped to decline-bearing rows. Nothing else in the store can be both unsettled and
    listable — the writer routes an unsettled scan to review precisely BECAUSE it is
    unsettled, and the gate settled a verdict on the way — so widening this would blank
    payloads for a case that cannot arise while claiming to protect it."""
    env = replace(_declined_envelope(scan_result=None), decline=None)
    assert "withheld" not in InboxItem.from_envelope(env).payload_view


async def test_a_completion_that_lost_a_race_writes_nothing() -> None:
    """A reject that lands mid-completion must not be undone by the completer's put.
    Re-validation is the long part of this operation (a SQL parse, a rewrite, an entity
    scan), so the window is real; the guard re-reads the row immediately before the write
    and refuses if it stopped being a form.

    Best-effort by construction — the store has no CAS — but the direction is chosen: a
    refused completion costs one retry, while a resurrected reject re-enters work a human
    deliberately removed (D29)."""

    class _RejectsMidFlight:
        """A stage that rejects the row from under the completion, exactly as another
        reviewer's click would."""

        stage_id = "generalize"

        def __init__(self, store, cid):
            self._store = store
            self._cid = cid

        async def process(self, env, ctx):
            from data_agent.learning.stage import StageResult

            current = await self._store.get(self._cid)
            await self._store.put(replace(current, status=CandidateStatus.REJECTED))
            return StageResult(env, "continue")

    store = await _store_with(_declined_envelope())
    cid = mint_review_candidate_id("hash-ratio", 0)
    inbox = ReviewInbox(
        store, completer=_completer(store, stages=(_RejectsMidFlight(store, cid),))
    )

    with pytest.raises(CompletionRaceError):
        await inbox.complete_parameterization(cid, entries=_HUMAN_ENTRIES)

    # The human's decision stands, and nothing the completion computed was written.
    assert (await store.get(cid)).status == CandidateStatus.REJECTED


async def test_the_race_guard_answers_409_not_500(enabled) -> None:
    """Through the wire: "somebody moved it" is a conflict the reviewer resolves by
    re-reading the row, not a server fault."""
    store = await _store_with(_declined_envelope())
    cid = mint_review_candidate_id("hash-ratio", 0)

    class _VanishingStage:
        stage_id = "generalize"

        async def process(self, env, ctx):
            from data_agent.learning.stage import StageResult

            await store.supersede("hash-ratio")  # the row is gone entirely
            return StageResult(env, "continue")

    inbox = ReviewInbox(store, completer=_completer(store, stages=(_VanishingStage(),)))
    client = TestClient(create_inbox_app(inbox=inbox, write_plane="offline"))

    resp = client.post(f"/inbox/{cid}/complete", json={"entries": _HUMAN_ENTRIES}, headers=AUTH)

    assert resp.status_code == 409, resp.text
    assert await store.get(cid) is None  # not resurrected


async def test_the_completer_rejects_a_non_array_body() -> None:
    store = await _store_with(_declined_envelope())
    inbox = ReviewInbox(store, completer=_completer(store))
    with pytest.raises(CompletionInputError):
        await inbox.complete_parameterization(
            mint_review_candidate_id("hash-ratio", 0), entries={"role": "inline"}
        )
