"""END TO END across the §C two-step: a declined form → `/revise` → `/complete`.

`tests/learning/revise/` tests the proposal in isolation and asserts that the store is
untouched. `tests/learning/inbox/test_fail_to_review_inbox.py` tests the completion in
isolation with hand-written entries. NOTHING joins them, and the join is where the design's
central safety claim lives:

    "`complete` stays the ONLY write path. The revise route is a proposal generator with no
     store access; the human is still the committer, and the thing they commit goes through
     the identical re-validation a hand-typed array goes through."  — §C.3

That claim is only testable by taking the model's EXACT bytes out of one response and posting
them into the other. A test that hand-writes the entries it "would have" proposed proves
nothing about the seam — the shape the reviser emits (`{entries, replace, rationale, reason,
diff}`) is not the shape `complete` accepts (`{entries, replace}`), and the reviewer's browser
is what bridges them.

So the harness below is `test_fail_to_review_inbox.py`'s, with a reviser bolted onto the same
inbox and the same store, and the assertions are about what the SECOND call did to the store.
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
from data_agent.learning.revise import BlueprintReviser
from data_agent.learning.writer import WriterStage
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
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

# The real fail-to-review shape: the model classified three of the four literal predicates of
# the accepted SQL and the corrective rounds never recovered `region`.
_MISSING_REGION = payroll_parameterization()[:3]
_UNCOVERED = blueprint_raw(parameterization=_MISSING_REGION, source_refs=("tc1",))
_DECLINE_DETAIL = (
    "candidate.payload.parameterization has no entry for 1 literal predicate(s) of the "
    "accepted SQL:\n  - region = 'NA' — no catalog rule declares this predicate"
)

# What a working reviser proposes for that decline: the ONE missing entry, appended.
_PROPOSED_ENTRY = param_slot(
    "region", slot_type="entity", required=False, optional_pattern="TRUE", value="NA"
)


class _ScriptedClient:
    def __init__(self, turns: list[ModelTurnResult]) -> None:
        self._turns = list(turns)
        self.calls: list[tuple[list[dict], list[dict]]] = []

    async def send_turn(self, messages: list[dict], tools: list[dict]) -> ModelTurnResult:
        self.calls.append((messages, tools))
        return self._turns.pop(0) if self._turns else ModelTurnResult(tool_calls=[])


def _proposal_turn(entries: list[dict] | None = None, **extra: object) -> ModelTurnResult:
    arguments: dict[str, object] = {
        "entries": entries if entries is not None else [_PROPOSED_ENTRY],
        "rationale": "region is the caller's question, not part of what the blueprint means",
    }
    arguments.update(extra)
    return ModelTurnResult(
        tool_calls=[
            ToolCallRequest(id="r1", name="propose_parameterization", arguments=arguments)
        ]
    )


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_TOKEN", TOKEN)


def _declined_envelope():
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
        make_summary(
            tool_calls=(make_tool_call(ref="tc1", sql=PAYROLL_SQL),), content_hash="hash-ratio"
        ),
        candidate_id=CID,
    )
    return replace(
        env,
        entity_scan={
            "result": "pass",
            "hits": [],
            "scanned_fields": ["intent"],
            "scanner": "regex+ner",
        },
    )


async def _client(turns: list[ModelTurnResult]):
    """ONE store, ONE inbox, BOTH planes — which is the point. A test that built the reviser
    over a different store than the completer would pass every assertion below while the two
    halves talked about different candidates."""
    store = InMemoryCandidateStore()
    await store.put(_declined_envelope())
    model = _ScriptedClient(turns)
    inbox = ReviewInbox(
        store,
        completer=ParameterizationCompleter(
            store=store,
            known_rules=frozenset(),
            rule_index=None,
            stages=(
                GeneralizeStage(catalog_schema=build_sqlglot_schema_from_catalog(_CATALOG)),
                LeakageGateStage(candidate_store=store),
                WriterStage(sampler=lambda env: False),
            ),
        ),
        reviser=BlueprintReviser(
            model_client=model,  # type: ignore[arg-type]
            known_rules=frozenset(),
            catalog_schema=build_sqlglot_schema_from_catalog(_CATALOG),
        ),
    )
    return TestClient(create_inbox_app(inbox=inbox, write_plane="offline")), store, model


# --- the seam -----------------------------------------------------------------


async def test_the_model_s_own_entries_go_through_the_full_re_validation(
    enabled: None,
) -> None:
    """⚠ THE ONE THAT MATTERS. The bytes posted to `/complete` are the bytes `/revise` handed
    back — no re-typing, no fixture — so this is the only test in the suite that can fail if
    the two shapes ever stop lining up.

    What it proves at the end is the safety claim: the candidate's `generalization` is the AST
    REWRITE's, the entity scan was re-settled against the CHANGED payload, and the decline
    block is gone. None of that is anything the reviser did — it is what `to_candidate` and the
    write-router stages did to the reviser's output, which is the entire argument for the
    two-step.
    """
    client, store, _ = await _client([_proposal_turn()])

    proposal = client.post(f"/inbox/{CID}/revise", json={"feedback": "region is a question"}, headers=AUTH)
    assert proposal.status_code == 200, proposal.text
    body = proposal.json()
    assert body["entries"] == [_PROPOSED_ENTRY]
    assert body["replace"] is False
    assert [row["kind"] for row in body["diff"]] == ["added", "unchanged", "unchanged", "unchanged"]

    # NOTHING was written by the proposal.
    assert (await store.get(CID)).status == CandidateStatus.NEEDS_PARAMETERIZATION
    assert (await store.get(CID)).decline is not None

    # The reviewer applies it — the EXACT entries and the EXACT flag, unmodified.
    applied = client.post(
        f"/inbox/{CID}/complete",
        json={"entries": body["entries"], "replace": body["replace"]},
        headers=AUTH,
    )

    assert applied.status_code == 200, applied.text
    assert applied.json()["outcome"] == "completed"
    assert applied.json()["decline"] is None

    env = await store.get(CID)
    assert env.status != CandidateStatus.NEEDS_PARAMETERIZATION
    assert env.decline is None
    # KEPT, not cleared: the reviser reads it, so a candidate stays editable after a
    # completion instead of exactly once. See `completion.py::_completed`.
    assert env.revalidation is not None
    # The write router RAN — the template is the rewrite's, derived from the accepted SQL.
    template = env.payload["generalization"]["sql_template"]
    assert "{region}" in template
    assert "SELECT" in template.upper()
    # Every literal predicate is now classified: three from the model, one from the reviser.
    assert len(env.payload["parameterization"]) == 4
    # ...and the scan was re-settled against the payload that changed.
    assert env.entity_scan["result"] != "pending"


async def test_a_proposal_that_does_not_cover_the_sql_comes_back_declined(
    enabled: None,
) -> None:
    """⚠ THE COMPLEMENT, and the more important half of "one validation path".

    A model can propose a plausible-looking entry for a predicate that is not in the accepted
    SQL. `to_candidate`'s D97 totality walk does not care where the entries came from: the real
    predicate is still uncovered, the candidate is still declined, and the row stays on the
    queue with a FRESH decline detail.

    Design §C.3: *"A model is trusted with less than a human, so it certainly does not get a
    shortcut around them."* This test is what makes that sentence checkable.
    """
    invented = param_slot("cost_centre", slot_type="entity", value="CC-9")
    client, store, _ = await _client([_proposal_turn([invented])])

    body = client.post(f"/inbox/{CID}/revise", json={"feedback": "?"}, headers=AUTH).json()
    assert body["entries"] == [invented]

    applied = client.post(
        f"/inbox/{CID}/complete",
        json={"entries": body["entries"], "replace": body["replace"]},
        headers=AUTH,
    )

    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["outcome"] == "declined"
    assert result["status"] == CandidateStatus.NEEDS_PARAMETERIZATION
    assert result["decline"]["reason"] == "totality_violation"
    # The fresh detail still names the predicate nobody covered — the reviser's suggestion did
    # not launder it.
    assert "region" in result["decline"]["detail"]

    env = await store.get(CID)
    assert env.status == CandidateStatus.NEEDS_PARAMETERIZATION
    assert "generalization" not in env.payload
    # The reviewer's work is KEPT: the merged payload is persisted even though it failed, so
    # the next attempt starts from what is already there.
    assert len(env.payload["parameterization"]) == 4


async def test_a_replace_proposal_is_applied_as_a_replace(enabled: None) -> None:
    """`replace` is the destructive branch and it crosses TWO hops — the reviser sets it, the
    browser echoes it, the completer acts on it. A hop that dropped it would silently turn a
    correction into an append and leave the wrong entry in place, which is the failure the
    flag exists to fix.

    Proposed here as a COMPLETE list, which is what `replace=true` means, so the candidate
    completes on the replacement alone.
    """
    complete_list = [*_MISSING_REGION, _PROPOSED_ENTRY]
    client, store, _ = await _client([_proposal_turn(complete_list, replace=True)])

    body = client.post(f"/inbox/{CID}/revise", json={"feedback": "start over"}, headers=AUTH).json()
    assert body["replace"] is True
    # Under replace, an entry absent from the proposal reads as REMOVED — the half of the
    # operation a reviewer most needs to see before applying it.
    assert {row["kind"] for row in body["diff"]} == {"added", "unchanged"}

    applied = client.post(
        f"/inbox/{CID}/complete",
        json={"entries": body["entries"], "replace": body["replace"]},
        headers=AUTH,
    )

    assert applied.status_code == 200, applied.text
    assert applied.json()["outcome"] == "completed"
    env = await store.get(CID)
    # FOUR, not seven: the proposal replaced the list rather than being appended to it.
    assert len(env.payload["parameterization"]) == 4


async def test_the_reviser_is_shown_the_unredacted_accepted_sql_and_the_decline(
    enabled: None,
) -> None:
    """§C.2's asymmetry, asserted where it is real rather than in the engine's own unit test:
    *"the human sees the redacted view; the model sees what the extractor saw."*

    A reviser shown `department = '[redacted]'` has nothing to re-role. The corollary is the
    placement rule the same section states — the engine runs SERVER-SIDE in the inbox service
    and receives nothing from the browser but the feedback string — so this also pins that the
    reviewer's own words arrive, and arrive inside the brief rather than spliced into the
    system message.
    """
    client, _, model = await _client([_proposal_turn()])

    client.post(
        f"/inbox/{CID}/revise",
        json={"feedback": "region should be a slot, not frozen"},
        headers=AUTH,
    )

    messages, tools = model.calls[0]
    assert [m["role"] for m in messages] == ["system", "user"]
    brief = messages[1]["content"]
    assert "region = 'NA'" in brief  # the accepted SQL's literal, unredacted
    assert "region should be a slot, not frozen" in brief  # the reviewer's own words
    assert "totality_violation" in brief or _DECLINE_DETAIL.split("\n")[0] in brief
    # The reviewer's words are in the USER message, never in the instruction message.
    assert "region should be a slot" not in messages[0]["content"]
    # The tool the model is forced into offers no way to write SQL.
    assert [t["name"] for t in tools] == ["propose_parameterization"]
    assert "sql_template" not in str(tools[0]["parameters"]["properties"])


async def test_no_suggestion_leaves_the_form_exactly_as_it_was(enabled: None) -> None:
    """The ordinary failure: the model returned nothing usable. It is a 200 with a reason, the
    candidate is untouched, and — the part worth pinning at this level — the reviewer can
    still complete the form by hand immediately afterwards."""
    client, store, _ = await _client([ModelTurnResult(tool_calls=[])])
    before = (await store.get(CID)).to_doc()

    proposal = client.post(f"/inbox/{CID}/revise", json={"feedback": "help"}, headers=AUTH)

    assert proposal.status_code == 200
    assert proposal.json()["entries"] == []
    assert proposal.json()["reason"]
    assert (await store.get(CID)).to_doc() == before

    applied = client.post(
        f"/inbox/{CID}/complete", json={"entries": [_PROPOSED_ENTRY]}, headers=AUTH
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["outcome"] == "completed"


async def test_revise_stays_available_after_the_form_has_been_completed(enabled: None) -> None:
    """The status guard holds AFTER the round trip — and `in_review` is now INSIDE it.

    This test used to assert a 409 here, on the grounds that `_completed` cleared
    `revalidation` so there was nothing to propose against. Both halves changed together and
    deliberately: a completion keeps the snapshot, and the review queue is revisable, because
    a reviewer who can SEE a bad role on a card is exactly who should be able to fix it
    (design §C.4). A completed form lands back in the pipeline and the router decides where —
    wherever that is, the guard is `REVISABLE_STATUSES`, not "is this still a form".

    What is still refused is `validated`/`promoted`, pinned in `test_revise_route.py`: those
    have passed golden replay and may be landed, so editing one is a re-verify-and-re-land,
    which is what `retract` is for.
    """
    client, _, _ = await _client([_proposal_turn(), _proposal_turn()])

    body = client.post(f"/inbox/{CID}/revise", json={"feedback": "f"}, headers=AUTH).json()
    client.post(
        f"/inbox/{CID}/complete",
        json={"entries": body["entries"], "replace": body["replace"]},
        headers=AUTH,
    )

    again = client.post(f"/inbox/{CID}/revise", json={"feedback": "f"}, headers=AUTH)

    # 200 now, not 409 — and the snapshot surviving the round trip is WHY: a proposal built
    # from nothing was the old failure mode, and it is still refused (an envelope with no
    # snapshot returns a reason, never a proposal). See the docstring.
    assert again.status_code == 200, again.text
    assert "no re-validation snapshot" not in (again.json().get("reason") or "")
