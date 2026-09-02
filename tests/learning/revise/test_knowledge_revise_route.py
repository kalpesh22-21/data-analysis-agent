"""The `/inbox/{id}/revise_knowledge` route + the §E wiring (knowledge-edit design §C.2, §E).

The route's guarantee is its sibling's: **REVISE WRITES NOTHING.** `apply_knowledge` stays the
only write into a knowledge payload, so a model's five fields face the identical intake reader
and the identical leakage re-scan a hand-typed set faces.

The wiring's guarantee is narrower and easy to get wrong: BOTH REVISERS OR NEITHER, from ONE
switch, ONE key, ONE model — because they are not two features but one feature pointed at the
two artifact kinds this queue holds. A deployment where the blueprint card offers an assistant
and the knowledge card silently does not reads to a reviewer as the knowledge one being broken.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateStatus
from data_agent.learning.inbox import ReviewInbox
from data_agent.learning.inbox.knowledge_edit import KnowledgeEditor, stage_scanner
from data_agent.learning.inbox.service import Revisers, _build_reviser, create_inbox_app
from data_agent.learning.leakage.gate import LeakageGateStage
from data_agent.learning.revise import KnowledgeReviser

from ..inbox.test_knowledge_edit import CID, CLEAN, knowledge_candidate
from .test_knowledge_reviser import ScriptedClient, proposal_turn

TOKEN = "reviewer-secret"
AUTH = {"X-Reviewer-Token": TOKEN}


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_TOKEN", TOKEN)


async def _client(*, reviser: KnowledgeReviser | None, env=None):
    store = InMemoryCandidateStore()
    await store.put(env if env is not None else knowledge_candidate())
    gate = (LeakageGateStage(candidate_store=store),)
    inbox = ReviewInbox(
        store,
        knowledge_reviser=reviser,
        knowledge_editor=KnowledgeEditor(store=store, stages=gate),
    )
    return TestClient(create_inbox_app(inbox=inbox, write_plane="offline")), store


def _reviser(turns) -> KnowledgeReviser:
    store = InMemoryCandidateStore()
    return KnowledgeReviser(
        model_client=ScriptedClient(turns),
        scanner=stage_scanner((LeakageGateStage(candidate_store=store),)),
    )


# --- the route --------------------------------------------------------------


async def test_a_proposal_comes_back_with_its_diff_and_the_store_is_untouched(
    enabled: None,
) -> None:
    """⚠ THE ROUTE'S GUARANTEE. A model's five fields reaching the store without passing the
    intake reader and the leakage re-scan is the one failure the two-step exists to prevent."""
    client, store = await _client(reviser=_reviser([proposal_turn()]))
    before = (await store.get(CID)).to_doc()

    resp = client.post(
        f"/inbox/{CID}/revise_knowledge", json={"feedback": "generalise it"}, headers=AUTH
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["payload"]["statement"]
    assert body["rationale"]
    assert body["reason"] == ""
    assert {row["field"] for row in body["diff"]} == {
        "statement",
        "knowledge_type",
        "structured",
        "related_terms",
        "scope",
    }
    # NOTHING moved: same doc, same status, no new candidate.
    assert (await store.get(CID)).to_doc() == before
    assert (await store.get(CID)).status == CandidateStatus.IN_REVIEW
    # ...and the body carries no status, because implying one would be a lie.
    assert "status" not in body


async def test_a_withheld_draft_is_a_200_with_a_reason_and_no_span(enabled: None) -> None:
    """The reviewer clicked the assistant because the card is withholding the flagged text; a
    draft that still carries it would put it straight back on the page."""
    client, _store = await _client(
        reviser=_reviser(
            [proposal_turn(arguments={"statement": "employee E10842 still", "rationale": "r"})]
        )
    )
    resp = client.post(f"/inbox/{CID}/revise_knowledge", json={"feedback": "x"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["payload"] == {}
    assert "statement (employee_code)" in resp.json()["reason"]
    assert "E10842" not in resp.text


async def test_no_suggestion_is_a_200_with_a_reason_not_an_error(enabled: None) -> None:
    from data_agent.runtime.model.client import ModelTurnResult

    client, _store = await _client(
        reviser=_reviser([ModelTurnResult(assistant_text="hmm", tool_calls=[])])
    )
    resp = client.post(f"/inbox/{CID}/revise_knowledge", json={"feedback": "x"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["payload"] == {}
    assert resp.json()["reason"]


async def test_a_forbidden_field_is_a_422_with_the_sentence_verbatim(enabled: None) -> None:
    """The model worked against a contract this system does not have, and a reviewer reading
    that sentence learns something TRUE (a fact has five fields, and they are the five the
    scanner reads) rather than "the assistant failed"."""
    client, _store = await _client(
        reviser=_reviser(
            [proposal_turn(arguments={"statement": CLEAN, "rationale": "r", "user_id": "u"})]
        )
    )
    resp = client.post(f"/inbox/{CID}/revise_knowledge", json={"feedback": "x"}, headers=AUTH)
    assert resp.status_code == 422
    assert "user_id" in resp.json()["detail"]


async def test_no_reviser_wired_is_a_503(enabled: None) -> None:
    """Absent costs a CONVENIENCE, not a capability: the five fields are still editable."""
    client, _store = await _client(reviser=None)
    resp = client.post(f"/inbox/{CID}/revise_knowledge", json={"feedback": "x"}, headers=AUTH)
    assert resp.status_code == 503


async def test_a_blueprint_is_a_409_not_a_proposal(enabled: None) -> None:
    """The assistant is never offered toward an operation `apply_knowledge` would then refuse."""
    client, _store = await _client(
        reviser=_reviser([proposal_turn()]),
        env=knowledge_candidate(type="blueprint", payload={"intent": "x"}),
    )
    resp = client.post(f"/inbox/{CID}/revise_knowledge", json={"feedback": "x"}, headers=AUTH)
    assert resp.status_code == 409


async def test_an_unknown_candidate_is_a_404(enabled: None) -> None:
    client, _store = await _client(reviser=_reviser([proposal_turn()]))
    resp = client.post("/inbox/candidate::nope/revise_knowledge", json={}, headers=AUTH)
    assert resp.status_code == 404


async def test_the_two_routes_compose_propose_then_apply(enabled: None) -> None:
    """THE TWO-STEP, end to end: the assistant proposes, the reviewer applies its payload
    verbatim, and the apply is what re-validates and re-scans."""
    client, store = await _client(reviser=_reviser([proposal_turn()]))

    proposed = client.post(
        f"/inbox/{CID}/revise_knowledge", json={"feedback": "generalise"}, headers=AUTH
    ).json()
    applied = client.post(
        f"/inbox/{CID}/apply_knowledge", json={"payload": proposed["payload"]}, headers=AUTH
    )

    assert applied.status_code == 200
    assert applied.json()["entity_scan"]["result"] == "pass"
    env = await store.get(CID)
    assert env.payload["statement"] == proposed["payload"]["statement"]
    assert env.knowledge_edit is not None


async def test_the_assistants_payload_applies_cleanly_including_its_empty_fields(
    enabled: None,
) -> None:
    """⚠ THE WIRE ALWAYS CARRIES FIVE KEYS so the form has a shape to prefill — which only
    works if applying them VERBATIM passes intake. An empty `scope`/`knowledge_type` and an
    empty `related_terms`/`structured` must all be legal, or the helpful default would make the
    Apply button fail on every proposal that left a field unset."""
    client, _store = await _client(
        reviser=_reviser([proposal_turn(arguments={"statement": CLEAN, "rationale": "r"})])
    )
    proposed = client.post(
        f"/inbox/{CID}/revise_knowledge", json={"feedback": "x"}, headers=AUTH
    ).json()
    assert proposed["payload"] == {
        "statement": CLEAN,
        "knowledge_type": "",
        "structured": {},
        "related_terms": [],
        "scope": "",
    }
    applied = client.post(
        f"/inbox/{CID}/apply_knowledge", json={"payload": proposed["payload"]}, headers=AUTH
    )
    assert applied.status_code == 200


# --- §E wiring --------------------------------------------------------------


class _Settings:
    """The three settings `_build_reviser` reads, and nothing else."""

    def __init__(self, **over):
        self.learning_revise_enabled = True
        self.learning_extractor_api_key = "sk-test"
        self.learning_revise_model = "gpt-test"
        self.learning_extractor_model = "gpt-test"
        self.learning_extractor_base_url = ""
        self.learning_revise_timeout_seconds = 12.0
        self.learning_trace_verbose = False
        self.__dict__.update(over)


class _Runtime:
    def __init__(self, path):
        self._path = path

    def catalog_fixture_file(self):
        return self._path


def _catalog_file(tmp_path):
    import json

    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"catalog": {}}), encoding="utf-8")
    return path


def test_the_switch_off_builds_neither_reviser(tmp_path) -> None:
    revisers = _build_reviser(
        _Settings(learning_revise_enabled=False), _Runtime(_catalog_file(tmp_path))
    )
    assert revisers == Revisers()


def test_no_api_key_builds_neither_reviser(tmp_path) -> None:
    revisers = _build_reviser(
        _Settings(learning_extractor_api_key=""), _Runtime(_catalog_file(tmp_path))
    )
    assert (revisers.blueprint, revisers.knowledge) == (None, None)


def test_an_unreadable_catalog_builds_neither_reviser(tmp_path) -> None:
    """⚠ A REAL COST, stated rather than hidden: only the BLUEPRINT reviser needs the catalog.
    Refusing both keeps §E's one switch, and the alternative is an assistant that appears on one
    card and not the other for a reason no reviewer could infer."""
    revisers = _build_reviser(_Settings(), _Runtime(tmp_path / "missing.json"))
    assert (revisers.blueprint, revisers.knowledge) == (None, None)


def test_with_a_scanner_both_are_built_and_share_one_model_client(tmp_path) -> None:
    """ONE client, one model, one timeout, one tracer — two would open two pools to the same
    endpoint and could drift apart on a setting nobody meant to split."""
    store = InMemoryCandidateStore()
    revisers = _build_reviser(
        _Settings(),
        _Runtime(_catalog_file(tmp_path)),
        knowledge_scanner=stage_scanner((LeakageGateStage(candidate_store=store),)),
    )
    assert revisers.blueprint is not None
    assert revisers.knowledge is not None
    assert revisers.knowledge.model_client is revisers.blueprint.model_client
    assert revisers.knowledge.model == revisers.blueprint.model
    assert revisers.knowledge.timeout_seconds == revisers.blueprint.timeout_seconds == 12.0


def test_without_a_scanner_the_knowledge_half_is_not_built_at_all(tmp_path) -> None:
    """⚠ NOT BUILT, rather than built-and-permanently-silent. A knowledge reviser with no
    scanner withholds every draft, so wiring one would answer 200-with-a-reason for ever and
    look like a model that never has an idea; `None` is what makes the page say "no assistant"."""
    revisers = _build_reviser(_Settings(), _Runtime(_catalog_file(tmp_path)))
    assert revisers.blueprint is not None
    assert revisers.knowledge is None


def test_the_verbose_trace_gate_reaches_both(tmp_path) -> None:
    """A human's free text plus model prose about an UNREDACTED payload, on both sides."""
    store = InMemoryCandidateStore()
    revisers = _build_reviser(
        _Settings(learning_trace_verbose=True),
        _Runtime(_catalog_file(tmp_path)),
        knowledge_scanner=stage_scanner((LeakageGateStage(candidate_store=store),)),
    )
    assert revisers.blueprint.trace_verbose is True
    assert revisers.knowledge.trace_verbose is True
