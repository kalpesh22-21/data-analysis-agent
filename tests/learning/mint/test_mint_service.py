"""The minting HTTP surface: `GET /inbox/mint/schema` and `POST /inbox/mint`.

Three things are pinned here that the engine tests cannot reach, because each is a property of
the ROUTE rather than of the minter:

  * a deployment with no minting plane answers the schema route 200 with `available: false`,
    so the page can say so in place of a form whose submit always fails;
  * a draft that does not validate is a 200 carrying the complaint, not a 4xx — the row exists
    and the expert's next step is on the page;
  * every failure mode maps to a status a caller can act on, and the model-off-contract case
    stays 502 with its sentence intact rather than becoming a generic "assistant failed".
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from data_agent.catalog.loader import build_sqlglot_schema_from_catalog
from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.generalize import GeneralizeStage
from data_agent.learning.inbox import ParameterizationCompleter, ReviewInbox
from data_agent.learning.inbox.service import create_inbox_app
from data_agent.learning.leakage import LeakageGateStage
from data_agent.learning.mint import BlueprintMinter
from data_agent.learning.writer import WriterStage
from tests._catalog_fixture import fixture_catalog

from ..extractor.helpers import PAYROLL_SQL, payroll_parameterization
from .test_mint_engine import ScriptedModelClient, classify_turn

TOKEN = "reviewer-secret"
AUTH = {"X-Reviewer-Token": TOKEN}
_CATALOG = fixture_catalog()

BODY = {
    "question": "what were total earnings for a department in a year?",
    "tables": ["payroll.payroll_fact"],
    "steps": ["filter to earnings rows", "sum gross_pay"],
    "assumptions": ["earnings only, not deductions"],
    "sql": PAYROLL_SQL,
    "sql_mode": "exact",
}


@pytest.fixture(autouse=True)
def _reviewer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_TOKEN", TOKEN)


def build_client(turns=None, *, with_minter: bool = True):
    store = InMemoryCandidateStore()
    completer = ParameterizationCompleter(
        store=store,
        known_rules=frozenset(),
        rule_index=None,
        stages=(
            GeneralizeStage(catalog_schema=build_sqlglot_schema_from_catalog(_CATALOG)),
            LeakageGateStage(candidate_store=store),
            WriterStage(sampler=lambda env: False),
        ),
    )
    minter = (
        BlueprintMinter(
            model_client=ScriptedModelClient(turns or [classify_turn()]),
            completer=completer,
            catalog_columns=(
                "payroll.payroll_fact.gross_pay",
                "payroll.payroll_fact.department",
            ),
        )
        if with_minter
        else None
    )
    inbox = ReviewInbox(store, completer=completer, minter=minter)
    return TestClient(create_inbox_app(inbox=inbox, write_plane="offline")), store


# --- the schema route ---------------------------------------------------------------------


def test_the_schema_route_offers_the_tables_and_their_columns() -> None:
    client, _ = build_client()

    body = client.get("/inbox/mint/schema", headers=AUTH).json()

    assert body["available"] is True
    assert body["tables"] == ["payroll.payroll_fact"]
    assert "payroll.payroll_fact.gross_pay" in body["columns"]


def test_a_deployment_without_a_minter_says_so_rather_than_erroring() -> None:
    """200, not 503. "This deployment cannot mint" is a fact about the page, and the UI needs
    it in order to render an explanation in place of the form."""
    client, _ = build_client(with_minter=False)

    response = client.get("/inbox/mint/schema", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"available": False, "tables": [], "columns": []}


def test_minting_without_a_plane_is_a_503() -> None:
    client, _ = build_client(with_minter=False)

    response = client.post("/inbox/mint", json=BODY, headers=AUTH)

    assert response.status_code == 503


# --- the mint route -----------------------------------------------------------------------


def test_a_valid_submission_returns_a_candidate_id_and_nothing_promoted() -> None:
    client, store = build_client()

    body = client.post("/inbox/mint", json=BODY, headers=AUTH).json()

    assert body["outcome"] == "completed"
    assert body["candidate_id"]
    assert body["accepted_sql"] == PAYROLL_SQL
    stored = asyncio.run(store.get(body["candidate_id"]))
    # THE CLAIM THE WHOLE DESIGN RESTS ON: minting files a candidate. It does not promote,
    # and it does not mark anything verified.
    assert stored.verified is False
    assert stored.status not in ("promoted", "validated")


def test_a_draft_that_does_not_validate_is_a_200_carrying_the_complaint() -> None:
    """Not a 4xx. The row exists with the validator's reason on it, and the expert's next
    action — ask the assistant to fix it — happens on that row."""
    dropped = [e for e in payroll_parameterization() if "region" not in str(e)]
    client, _ = build_client([classify_turn(entries=dropped)])

    response = client.post("/inbox/mint", json=BODY, headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "declined"
    assert body["candidate_id"]
    assert body["warnings"]


def test_a_form_that_cannot_be_drafted_from_is_a_400() -> None:
    client, _ = build_client()

    response = client.post(
        "/inbox/mint", json={**BODY, "tables": []}, headers=AUTH
    )

    assert response.status_code == 400
    assert "table" in response.json()["detail"]


def test_exact_mode_without_sql_is_refused_before_any_model_call() -> None:
    """The mode PROMISES a query that ran. Catching it here costs nothing; catching it after
    the model call would bill a turn to learn something the form already knew."""
    client, _ = build_client()

    response = client.post(
        "/inbox/mint", json={**BODY, "sql": ""}, headers=AUTH
    )

    assert response.status_code == 400


def test_a_model_answering_off_contract_is_a_502_with_its_sentence_intact() -> None:
    """The expert did nothing wrong and the deployment is not broken. The sentence explaining
    that the template is derived rather than authored is the useful thing to show."""
    from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest

    off_contract = ModelTurnResult(
        tool_calls=[
            ToolCallRequest(
                id="m1",
                name="classify_blueprint",
                arguments={
                    "intent": "x",
                    "sql": "SELECT 1",
                    "entries": payroll_parameterization(),
                    "rationale": "r",
                },
            )
        ]
    )
    client, _ = build_client([off_contract])

    response = client.post("/inbox/mint", json=BODY, headers=AUTH)

    assert response.status_code == 502
    assert "cannot be rewritten" in response.json()["detail"]


def test_re_minting_a_candidate_that_moved_on_is_a_409() -> None:
    from dataclasses import replace

    from data_agent.learning.candidate.models import CandidateStatus

    client, store = build_client([classify_turn(), classify_turn()])
    first = client.post("/inbox/mint", json=BODY, headers=AUTH).json()
    asyncio.run(
        store.put(
            replace(
                asyncio.run(store.get(first["candidate_id"])),
                status=CandidateStatus.PROMOTED,
            )
        )
    )

    response = client.post("/inbox/mint", json=BODY, headers=AUTH)

    assert response.status_code == 409
    assert first["candidate_id"] in response.json()["detail"]


def test_the_route_is_behind_the_reviewer_token() -> None:
    """Minting WRITES to the access-controlled review queue, so it is guarded exactly like
    every other write here — an unauthenticated door into that store is the whole point of
    the guard being on the router rather than on each handler."""
    client, _ = build_client()

    assert client.post("/inbox/mint", json=BODY).status_code == 401
    assert client.get("/inbox/mint/schema").status_code == 401
