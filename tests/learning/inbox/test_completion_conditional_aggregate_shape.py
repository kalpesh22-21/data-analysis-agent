"""The reviewer completion path on the H5/H6 shape: it lands a blueprint, and it never 500s.

`/inbox/{id}/complete` maps `InboxTransitionError` (404/409), `CompletionRaceError`
(409), `CompletionInputError` (422) and `CompletionUnavailableError` (503). Anything else
is a 500 — and on the base the write-router pipeline raised `sqlglot.ParseError` from
inside `GeneralizeStage` on a form that was correctly filled in, because the candidate's
rule predicates live inside `sumIf` conditions and deleting them left
`sumIf(p.amount) ... WHERE  GROUP BY` (ISSUES H5/H6).

That 500 was permanent, not transient: the same entries produce the same parse failure on
every retry, so the row was unclearable by completion and the reviewer was told nothing
except that the server broke. The shape here is the LIVE one — the deductions-to-earnings
ratio the totality hint was written for, and the candidate whose decline steers a reviewer
toward `role: rule` for exactly those predicates.

TWO THINGS ARE PINNED, and they came from different fixes:

  * the reviewer's work LANDS. `role=rule` keeps the predicate now, so the completed form
    generalizes cleanly — the hint that sent them to `role: rule` was good advice all
    along, and the rewrite is what made it wrong;
  * the endpoint answers the reviewer whatever S4 concludes. The containment seams
    (`builder`'s fail-soft hash input, the stage's enforced "S4 never raises") are what
    make that true for the NEXT shape nobody predicted, and they are tested against
    injected faults in `tests/learning/generalize/test_rewrite_fragility_seams.py`.
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
from data_agent.learning.extractor.grounding import (
    known_rule_ids_from_catalog,
    rule_index_from_catalog,
)
from data_agent.learning.extractor.models import Decline
from data_agent.learning.generalize import GeneralizeStage
from data_agent.learning.inbox import ParameterizationCompleter, ReviewInbox
from data_agent.learning.inbox.service import create_inbox_app
from data_agent.learning.leakage import LeakageGateStage
from data_agent.learning.writer import WriterStage
from tests._catalog_fixture import fixture_catalog

from ..extractor.helpers import blueprint_raw, make_summary, make_tool_call

TOKEN = "reviewer-secret"
AUTH = {"X-Reviewer-Token": TOKEN}

_CATALOG = fixture_catalog()
_KNOWN = known_rule_ids_from_catalog(_CATALOG)
_INDEX = rule_index_from_catalog(_CATALOG)
_CID = mint_review_candidate_id("hash-ratio", 0)
_PAYROLL = "dbpcm_warehouse.payroll"

_RATIO_SQL = (
    "SELECT p.employee_code, "
    "sumIf(p.amount, p.register_type = 'DDUCT') / sumIf(p.amount, p.register_type = 'EARN') "
    "AS ratio "
    "FROM dbpcm_warehouse.payroll AS p "
    "WHERE p.register_type IN ('DDUCT','EARN') "
    "GROUP BY p.employee_code"
)


def _entry(value: str, **rest) -> dict:
    return {
        "locator": {"table": _PAYROLL, "column": "register_type", "value": value},
        **rest,
    }


# The live decline: the model covered the `IN` list and left the two `sumIf` predicates
# unaccounted for, and both corrective rounds went elsewhere.
_UNCOVERED = blueprint_raw(
    intent="ratio of deductions to earnings per employee",
    parameterization=[
        _entry("DDUCT,EARN", role="inline", why="the ratio is defined over these types")
    ],
    source_refs=("tc1",),
)

# What the decline's hint asks the reviewer for: a catalog rule per uncovered predicate.
_HUMAN_ENTRIES = [
    _entry("DDUCT", role="rule", rule_id="employee_deductions"),
    _entry("EARN", role="rule", rule_id="gross_earnings"),
]


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_TOKEN", TOKEN)


def _summary():
    return make_summary(
        tool_calls=(make_tool_call(ref="tc1", sql=_RATIO_SQL),), content_hash="hash-ratio"
    )


def _declined_envelope():
    env = build_declined_envelope(
        Decline(
            type="blueprint",
            reason="totality_violation",
            detail="register_type = 'DDUCT' has no parameterization entry",
            correctable=True,
            corrections_attempted=2,
            correction_history=(),
            raw_payload=_UNCOVERED,
        ),
        _summary(),
        candidate_id=_CID,
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


async def _client():
    store = InMemoryCandidateStore()
    await store.put(_declined_envelope())
    stages = (
        GeneralizeStage(catalog_schema=build_sqlglot_schema_from_catalog(_CATALOG)),
        LeakageGateStage(candidate_store=store),
        WriterStage(sampler=lambda env: False),
    )
    completer = ParameterizationCompleter(
        store=store, known_rules=_KNOWN, rule_index=_INDEX, stages=stages
    )
    inbox = ReviewInbox(store, completer=completer)
    return TestClient(create_inbox_app(inbox=inbox, write_plane="offline")), store


async def test_the_completed_ratio_form_lands_a_blueprint_and_never_500s(enabled) -> None:
    """The endpoint contract on the shape that broke it, and the reviewer's work landing.

    The form is what the decline asked for: a catalog rule per uncovered `sumIf`
    predicate. On the base that raised `ParseError` through the endpoint (a 500 in a
    deployed server); the first fix made it an in-band `fail_to_review`, which answered
    the reviewer but still threw their work away. Keeping the predicates makes it a
    validated blueprint — 200, out of `needs_parameterization`, generalization stamped
    `ok`."""
    client, store = await _client()

    resp = client.post(
        f"/inbox/{_CID}/complete", json={"entries": _HUMAN_ENTRIES}, headers=AUTH
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "completed"
    assert body["status"] != CandidateStatus.NEEDS_PARAMETERIZATION

    env = await store.get(_CID)
    generalization = env.payload["generalization"]
    assert generalization["static_validation"]["outcome"] == "ok"
    template = generalization["sql_template"]
    # The two metrics the deleting rewrite collapsed into identical one-argument calls.
    assert "sumIf(p.amount, p.register_type = \'DDUCT\')" in template
    assert "sumIf(p.amount, p.register_type = \'EARN\')" in template
    # And the reviewer's two rule citations are recorded beside them.
    assert set(generalization["uses_rules"]) == {"employee_deductions", "gross_earnings"}
