"""The `/inbox/{id}/revise` route (design §C.3): propose, then apply.

The two-step is the design, not a rough edge, and these tests pin the half that makes it worth
having: **REVISE WRITES NOTHING.** `complete` stays the only write path into a candidate's
payload, so a model's entries face the identical `to_candidate` re-validation — D97 totality
walk included — that a hand-typed array faces.

Also pinned: the status guard, and WHY it cannot simply be widened to `in_review`. A candidate
that never declined has no `ValidationSnapshot` (it is written at decline time only), so there
is no accepted SQL to propose against and nothing to re-validate a revision with. That is the
structural reason §C.4 scopes the first cut to this one queue, and it is the kind of thing a
future reader would otherwise try to "fix" in a line.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import (
    CandidateStatus,
    build_declined_envelope,
    mint_review_candidate_id,
)
from data_agent.learning.candidate.verdicts import LeakageVerdict
from data_agent.learning.extractor.models import Decline
from data_agent.learning.inbox import ReviewInbox
from data_agent.learning.inbox.service import create_inbox_app
from data_agent.learning.revise import BlueprintReviser

from ..extractor.helpers import PAYROLL_SQL, make_summary, make_tool_call
from .test_reviser import ScriptedClient, proposal_turn

TOKEN = "reviewer-secret"
AUTH = {"X-Reviewer-Token": TOKEN}
CID = mint_review_candidate_id("hash-revise", 0)

_UNCOVERED = {
    "intent": "ratio of deductions to earnings by department",
    "kind": "single",
    "parameterization": [],
    "source_tool_call_refs": ["tc1"],
}


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_TOKEN", TOKEN)


def _declined_envelope():
    summary = make_summary(tool_calls=[make_tool_call("tc1", PAYROLL_SQL)])
    env = build_declined_envelope(
        Decline(
            type="blueprint",
            reason="totality_violation",
            detail="no entry for 2 literal predicate(s)",
            correctable=True,
            corrections_attempted=2,
            correction_history=(),
            raw_payload=_UNCOVERED,
        ),
        summary,
        candidate_id=CID,
    )
    return replace(
        env,
        entity_scan=LeakageVerdict(
            result="pass", scanned_fields=("intent",), scanner="regex"
        ).to_doc(),
    )


async def _client(*, reviser: BlueprintReviser | None, env=None):
    store = InMemoryCandidateStore()
    await store.put(env if env is not None else _declined_envelope())
    inbox = ReviewInbox(store, reviser=reviser)
    return TestClient(create_inbox_app(inbox=inbox, write_plane="offline")), store


async def test_a_proposal_comes_back_with_a_diff_and_the_store_is_untouched(
    enabled: None,
) -> None:
    """⚠ THE ROUTE'S GUARANTEE. A model's suggestion reaching the store without passing
    `to_candidate` is the one failure this whole two-step design exists to prevent."""
    reviser = BlueprintReviser(model_client=ScriptedClient([proposal_turn()]))
    client, store = await _client(reviser=reviser)
    before = (await store.get(CID)).to_doc()

    resp = client.post(f"/inbox/{CID}/revise", json={"feedback": "inline both"}, headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["entries"]) == 2
    assert body["replace"] is False
    assert body["rationale"]
    assert [row["kind"] for row in body["diff"]] == ["added", "added"]
    # NOTHING moved: same doc, same status, no new candidate.
    assert (await store.get(CID)).to_doc() == before
    assert (await store.get(CID)).status == CandidateStatus.NEEDS_PARAMETERIZATION
    # ...and the body carries no status, because implying one would be a lie.
    assert "status" not in body


async def test_no_suggestion_is_a_200_with_a_reason_not_an_error(enabled: None) -> None:
    """The reviewer did nothing wrong and the useful next step belongs on the page, not in an
    error banner — the same call `complete` makes for a still-incomplete form."""
    from data_agent.runtime.model.client import ModelTurnResult

    reviser = BlueprintReviser(
        model_client=ScriptedClient([ModelTurnResult(assistant_text="hmm", tool_calls=[])])
    )
    client, _ = await _client(reviser=reviser)
    resp = client.post(f"/inbox/{CID}/revise", json={"feedback": "x"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["entries"] == []
    assert resp.json()["reason"]


async def test_a_template_edit_is_a_422_naming_the_reason(enabled: None) -> None:
    """Surfaced VERBATIM: the model worked against a contract this system does not have, and a
    reviewer reading that sentence learns something true rather than "the assistant failed"."""
    reviser = BlueprintReviser(
        model_client=ScriptedClient(
            [
                proposal_turn(
                    arguments={
                        "entries": [
                            {"locator": {"table": "t", "column": "c", "value": "v"},
                             "role": "inline", "why": "w"}
                        ],
                        "rationale": "r",
                        "sql_template": "SELECT 1",
                    }
                )
            ]
        )
    )
    client, _ = await _client(reviser=reviser)
    resp = client.post(f"/inbox/{CID}/revise", json={"feedback": "x"}, headers=AUTH)
    assert resp.status_code == 422
    assert "sql_template" in resp.json()["detail"]


async def test_no_reviser_wired_is_a_503_and_the_form_still_works(enabled: None) -> None:
    """Absent, the assistant costs a CONVENIENCE, not a capability — which is why its switch is
    independent of the completer's."""
    client, _ = await _client(reviser=None)
    resp = client.post(f"/inbox/{CID}/revise", json={"feedback": "x"}, headers=AUTH)
    assert resp.status_code == 503


async def test_revise_is_offered_on_the_review_queue(enabled: None) -> None:
    """`in_review` is INSIDE the guard, and was not always.

    It was structurally impossible rather than merely disallowed: `ValidationSnapshot` was
    written at DECLINE time only, so an ordinary review item had no accepted SQL to propose
    against. `build_candidate_envelope` now stamps one on every kept candidate — design §C.4's
    named unlock — and a reviewer who can SEE a bad role on a card can fix it there.
    """
    env = replace(_declined_envelope(), status=CandidateStatus.IN_REVIEW)
    reviser = BlueprintReviser(model_client=ScriptedClient([proposal_turn()]))
    client, _ = await _client(reviser=reviser, env=env)
    resp = client.post(f"/inbox/{CID}/revise", json={"feedback": "x"}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["entries"]) == 2


@pytest.mark.parametrize(
    "status",
    [CandidateStatus.VALIDATED, CandidateStatus.PROMOTED, CandidateStatus.REJECTED],
)
async def test_revise_is_refused_past_the_point_of_judgement(
    enabled: None, status: str
) -> None:
    """⚠ WHERE THE GUARD STOPS, and why it is a set and not "any status".

    A `validated` candidate has passed golden replay; a `promoted` one may already be landed
    and serving recall. Editing either is a re-verify-and-re-land of an artifact the corpus is
    using — a different operation with a different blast radius, and `retract` is how it is
    expressed. `rejected` is terminal by design. Parameterised so adding a status to
    `REVISABLE_STATUSES` "while we are here" has to argue with a test first.
    """
    env = replace(_declined_envelope(), status=status)
    reviser = BlueprintReviser(model_client=ScriptedClient([proposal_turn()]))
    client, _ = await _client(reviser=reviser, env=env)
    resp = client.post(f"/inbox/{CID}/revise", json={"feedback": "x"}, headers=AUTH)
    assert resp.status_code == 409, resp.text


async def test_an_unknown_id_is_a_404(enabled: None) -> None:
    reviser = BlueprintReviser(model_client=ScriptedClient([proposal_turn()]))
    client, _ = await _client(reviser=reviser)
    resp = client.post("/inbox/candidate::nope::0/revise", json={"feedback": "x"}, headers=AUTH)
    assert resp.status_code == 404


async def test_an_empty_body_is_accepted(enabled: None) -> None:
    """"Just look at it" is a legitimate request: the validator's complaint is already in the
    brief, so the assistant has something to answer without any reviewer text at all."""
    reviser = BlueprintReviser(model_client=ScriptedClient([proposal_turn()]))
    client, _ = await _client(reviser=reviser)
    resp = client.post(f"/inbox/{CID}/revise", headers=AUTH)
    assert resp.status_code == 200
    assert len(resp.json()["entries"]) == 2


async def test_the_route_requires_the_reviewer_token(enabled: None) -> None:
    reviser = BlueprintReviser(model_client=ScriptedClient([proposal_turn()]))
    client, _ = await _client(reviser=reviser)
    assert client.post(f"/inbox/{CID}/revise", json={"feedback": "x"}).status_code in (401, 403)
