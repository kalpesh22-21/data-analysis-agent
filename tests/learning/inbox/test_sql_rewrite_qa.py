"""§C.5 ADVERSARIAL — is an assistant-REWRITTEN blueprint really checked like a mined one?

The requirement in the reviewer's own words: *"make sure the ask-assistant landed blueprint is
also validated with all the same checks we do for a candidate extracted by extractor"*, and
specifically that the frozen run-date check bites a rewritten query exactly as it bites a
session-mined one.

`test_completion_sql_rewrite.py` pins the HAPPY shapes of that claim at the completer. This file
attacks it, and it attacks it where the reviewer actually is: THROUGH THE ROUTES. Every guard on
this plane exists twice — once in `revise` (which only PROPOSES, and can therefore afford to
discard) and once in the completer (which WRITES) — and the interesting question for every one
of them is what a client that skips the first half gets. `apply_revision`/`complete` accept a
`sql` from anybody holding the reviewer token; the reviser is a typing aid, not a gate.

THREE THINGS THIS FILE ESTABLISHES.

  1. The round trip really is end to end: `revise(allow_sql)` → `apply_revision(sql)` leaves a
     row whose static validation was computed from the NEW query, stamped `authored`, held at
     `in_review` even when every check passes — the asymmetry that IS the §C.5 safety argument.

  2. The checks a rewrite can newly fail all fire through HTTP: the frozen run date (in three
     spellings, including the one the F3 gap lets through — pinned as current behaviour, not as
     approval), `SELECT *`, an off-catalog table, `scratch.*`, and a DDL statement.

  3. That the write path is NOT weaker than the propose path — which is where this file found
     its defects, and what it now pins as fixed. Three guards used to live only in `revise`
     (composite, size cap, read-only), so a client posting straight to `apply_revision` walked
     past all three. Each is now enforced at the write boundary too, and the tests that recorded
     the gaps assert the refusal AND an unchanged store, because a guard that fires after the
     snapshot has been replaced returns the right status code and has still done the damage.
"""

from __future__ import annotations

import hashlib
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
from data_agent.learning.inbox.completion import REWRITE_TOOL_CALL_REF
from data_agent.learning.inbox.service import create_inbox_app
from data_agent.learning.leakage import LeakageGateStage
from data_agent.learning.mint.models import MAX_SQL_CHARS
from data_agent.learning.revise import BlueprintReviser
from data_agent.learning.writer import WriterStage
from data_agent.learning.writer.routing import derive_inbox_reason, route_candidate
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from tests._catalog_fixture import fixture_catalog

from ..extractor.helpers import blueprint_raw, make_summary, make_tool_call
from ..revise.test_reviser import ScriptedClient

TOKEN = "reviewer-secret"
AUTH = {"X-Reviewer-Token": TOKEN}
CID = mint_review_candidate_id("hash-rewrite-qa", 0)

_CATALOG = build_sqlglot_schema_from_catalog(fixture_catalog())
_T = "dbpcm_warehouse.payroll"

# The query the SESSION ran. Two literal predicates, both classified below.
ACCEPTED_SQL = (
    f"SELECT SUM(amount) AS total FROM {_T} "
    "WHERE register_type = 'EARN' AND department_code = '0420'"
)
# A CLEAN rewrite reading a column the accepted query never touched, so `uses` and the template
# both visibly CHANGE — which is how a test can tell the static validation was recomputed from
# the new query rather than carried over from the old one.
CLEAN_REWRITE = (
    f"SELECT SUM(amount) AS total, MAX(type_rate) AS top_rate FROM {_T} "
    "WHERE register_type = 'EARN' AND department_code = '0420' AND type_code = 'REG'"
)


def _inline(column: str, value: str, why: str = "part of what this blueprint means") -> dict:
    return {
        "locator": {"table": _T, "column": column, "value": value},
        "role": "inline",
        "why": why,
    }


def _slot(column: str, value: str, *, table: str = _T) -> dict:
    return {
        "locator": {"table": table, "column": column, "value": value},
        "role": "slot",
        "slot": {
            "name": column,
            "type": "entity",
            "binds_to": f"{table}.{column}",
            "required": True,
        },
    }


ACCEPTED_ENTRIES = [
    _inline("register_type", "EARN", "defines the metric earnings"),
    _slot("department_code", "0420"),
]
CLEAN_ENTRIES = [*ACCEPTED_ENTRIES, _inline("type_code", "REG", "regular pay only")]


def _declined_envelope(
    *, kind: str = "single", parameterization=None, composes=None, tool_calls=None
):
    """A persisted row whose accepted SQL is `ACCEPTED_SQL`, scan settled `pass`."""
    raw = blueprint_raw(
        parameterization=[] if parameterization is None else parameterization,
        source_refs=("tc1",) if composes is None else ("tc1", "tc2"),
        kind=kind,
    )
    if composes is not None:
        raw["payload"]["composes"] = composes
    env = build_declined_envelope(
        Decline(
            type="blueprint",
            reason="totality_violation",
            detail="no entry for 2 literal predicate(s)",
            correctable=True,
            corrections_attempted=1,
            raw_payload=raw,
        ),
        make_summary(
            tool_calls=(
                tool_calls
                if tool_calls is not None
                else (make_tool_call(ref="tc1", sql=ACCEPTED_SQL),)
            ),
            content_hash="hash-rewrite-qa",
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


def _stages(store):
    """The blueprint half of the frozen write-router order, as `_build_completer` assembles it."""
    return (
        GeneralizeStage(catalog_schema=_CATALOG),
        LeakageGateStage(candidate_store=store),
        WriterStage(sampler=lambda env: False),
    )


def _static(env) -> dict:
    return (env.payload.get("generalization") or {}).get("static_validation") or {}


def _gen(env) -> dict:
    return env.payload.get("generalization") or {}


async def _completer(env=None):
    store = InMemoryCandidateStore()
    env = env if env is not None else _declined_envelope()
    await store.put(env)
    return (
        ParameterizationCompleter(store=store, known_rules=frozenset(), stages=_stages(store)),
        store,
        env,
    )


def rewrite_turn(sql: str, *, entries=None) -> ModelTurnResult:
    """One scripted reviser turn that returns a replacement query."""
    return ModelTurnResult(
        tool_calls=[
            ToolCallRequest(
                id="r1",
                name="propose_parameterization",
                arguments={
                    "entries": CLEAN_ENTRIES if entries is None else entries,
                    "rationale": "the feedback cannot be met by re-roling; replacing the query",
                    "sql": sql,
                },
            )
        ]
    )


def plain_turn(entries=None) -> ModelTurnResult:
    """A reviser turn with NO `sql` — what the assistant may return when `allow_sql` is off."""
    return ModelTurnResult(
        tool_calls=[
            ToolCallRequest(
                id="r1",
                name="propose_parameterization",
                arguments={
                    "entries": CLEAN_ENTRIES if entries is None else entries,
                    "replace": True,
                    "rationale": "every literal predicate of the accepted query, classified",
                },
            )
        ]
    )


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_TOKEN", TOKEN)


async def _app(env=None, *, turns=()):
    """The routed surface a reviewer actually drives: reviser + completer over one store."""
    store = InMemoryCandidateStore()
    await store.put(env if env is not None else _declined_envelope())
    scripted = ScriptedClient(list(turns))
    inbox = ReviewInbox(
        store,
        completer=ParameterizationCompleter(
            store=store, known_rules=frozenset(), stages=_stages(store)
        ),
        reviser=BlueprintReviser(model_client=scripted),
    )
    app = create_inbox_app(inbox=inbox, write_plane="offline")
    return TestClient(app), store, scripted


# --- 1. the full two-step, through the routes --------------------------------


async def test_the_revise_then_apply_round_trip_is_authored_recomputed_and_held(
    enabled: None,
) -> None:
    """⚠ THE WHOLE FEATURE, END TO END, AND THE ONE ASSERTION THAT MATTERS MOST IS THE LAST.

    A reviewer ticks the box, the assistant returns a replacement query, the reviewer applies it,
    and the row that comes out is checked from scratch: `sql_by_ref` keyed by the one honest ref a
    hand-authored query has, `authored=True`, a durable badge on the payload, and a
    `static_validation` computed from the NEW SQL — provably so, because `uses` now carries a
    column the accepted query never read.

    And then it is HELD. Every static check passes; the scan is clean; dedup has nothing to say.
    A MINED candidate in exactly this state auto-lands as `candidate`. This one is `in_review`
    with reason `hand_authored`, because a person's assistant wrote the query rather than a
    warehouse answering a user. That asymmetry is the §C.5 safety argument, and it is worth
    asserting through HTTP rather than at the completer: the routes are what the reviewer drives,
    and a two-step whose halves disagreed would still pass a completer-only test.
    """
    env = replace(_declined_envelope(), status=CandidateStatus.IN_REVIEW, decline=None)
    client, store, scripted = await _app(env, turns=[rewrite_turn(CLEAN_REWRITE)])

    proposed = client.post(
        f"/inbox/{CID}/revise",
        json={"feedback": "it should exclude overtime", "allow_sql": True},
        headers=AUTH,
    )
    assert proposed.status_code == 200, proposed.text
    assert proposed.json()["sql_changed"] is True
    assert proposed.json()["sql"] == CLEAN_REWRITE
    assert proposed.json()["replace"] is True
    # NOTHING was written by the proposal — the two-step's guarantee.
    assert (await store.get(CID)).revalidation.sql_by_ref == {"tc1": (ACCEPTED_SQL,)}

    applied = client.post(
        f"/inbox/{CID}/apply_revision",
        json={"entries": proposed.json()["entries"], "sql": proposed.json()["sql"]},
        headers=AUTH,
    )

    assert applied.status_code == 200, applied.text
    assert applied.json()["outcome"] == "completed"
    assert applied.json()["sql_rewritten"] is True

    stored = await store.get(CID)
    assert stored.revalidation.authored is True
    assert stored.revalidation.reconstructed is False
    assert stored.revalidation.sql_by_ref == {REWRITE_TOOL_CALL_REF: (CLEAN_REWRITE,)}
    assert [p.tool_call_ref for p in stored.revalidation.evidence] == [REWRITE_TOOL_CALL_REF]
    assert stored.payload["sql_rewrite"]["by"] == "assistant"
    assert stored.payload["sql_rewrite"]["previous_sql_sha256"] == hashlib.sha256(
        ACCEPTED_SQL.encode("utf-8")
    ).hexdigest()
    assert stored.payload["source_tool_call_refs"] == [REWRITE_TOOL_CALL_REF]

    # RECOMPUTED FROM THE NEW SQL, not carried: `type_rate`/`type_code` exist only in the rewrite.
    checks = _static(stored)
    assert checks["outcome"] == "ok"
    assert all(
        checks[name] is True
        for name in (
            "explain_ok",
            "binds_to_subset_uses",
            "dag_ok",
            "read_only_select",
            "date_literal_ok",
        )
    )
    assert f"{_T}.type_rate" in _gen(stored)["uses"]
    assert f"{_T}.type_code" in _gen(stored)["uses"]
    assert "{department_code}" in _gen(stored)["sql_template"]

    # ...and HELD, for the authored reason specifically.
    assert stored.status == CandidateStatus.IN_REVIEW
    assert derive_inbox_reason(stored) == "hand_authored"
    decision = route_candidate(stored, sampled_for_inbox=False)
    assert (decision.status, decision.control, decision.reason) == (
        CandidateStatus.IN_REVIEW,
        "route_inbox",
        "hand_authored",
    )
    assert scripted.calls  # the assistant really was asked


async def test_the_same_round_trip_on_the_form_queue_forces_replace_and_drops_old_entries(
    enabled: None,
) -> None:
    """⚠ `complete` APPENDS BY DEFAULT, and that is exactly wrong for a rewrite.

    The reviewer's page has one checkbox and a client may simply not send it. Every pre-existing
    entry describes the OLD query, so an append leaves a parameterization half about a string
    nobody has — which would then decline for `totality_violation` naming predicates the reviewer
    never wrote. `ReviewInbox` forces the mode the completer would otherwise refuse, so the
    request succeeds and the array is EXACTLY what was posted.
    """
    env = _declined_envelope(parameterization=ACCEPTED_ENTRIES)
    client, store, _ = await _app(env)

    resp = client.post(
        f"/inbox/{CID}/complete",
        json={"entries": CLEAN_ENTRIES, "sql": CLEAN_REWRITE, "replace": False},
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["sql_rewritten"] is True
    stored = await store.get(CID)
    values = [e["locator"]["value"] for e in stored.payload["parameterization"]]
    assert values == ["EARN", "0420", "REG"]  # replaced, not doubled
    assert len(values) == len(CLEAN_ENTRIES)


# --- 2. the frozen run date, through the routes ------------------------------


DATED_FUNCTION_ARG = (
    f"SELECT SUM(amount) AS total FROM {_T} WHERE register_type = 'EARN' "
    "AND dateDiff('day', pay_date, toDateTime64('2026-09-01 00:00:00', 3)) < 30"
)
DATED_PROJECTION = (
    f"SELECT SUM(amount) AS total, toDate('2026-09-01') AS as_of FROM {_T} "
    "WHERE register_type = 'EARN'"
)


@pytest.mark.parametrize(
    ("sql", "entries"),
    [
        (
            DATED_FUNCTION_ARG,
            [
                _inline("register_type", "EARN", "defines the metric earnings"),
                _inline("pay_date", "30", "a 30-day trailing window"),
            ],
        ),
        (DATED_PROJECTION, [_inline("register_type", "EARN", "defines the metric")]),
    ],
    ids=["function_argument", "bare_date_in_projection"],
)
async def test_a_run_date_frozen_into_a_rewrite_fails_the_date_check_over_http(
    enabled: None, sql: str, entries: list
) -> None:
    """⚠ THE REQUIREMENT, STATED AS A TEST: the frozen-run-date check applies to a REWRITTEN
    query exactly as it does to a session-mined one.

    Two spellings, because the incident's own spelling being caught says nothing about the next
    one's: the run date buried in a `dateDiff` argument (the original incident) and a bare ISO
    date sitting in a PROJECTION, which no comparison adjudicates at all. Neither is a predicate
    the parameterization walk can see, which is the whole reason this check is structural.

    A blueprint carrying either answers a DIFFERENT question every day it ages, silently — the
    D97 wrong-answer class. It routes to a human with `frozen_date_literal` as the machine reason
    S7 keys on, so the tag names the fault rather than leaving a reviewer to diff two queries.
    """
    client, store, _ = await _app()

    resp = client.post(
        f"/inbox/{CID}/complete", json={"entries": entries, "sql": sql}, headers=AUTH
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["sql_rewritten"] is True
    stored = await store.get(CID)
    checks = _static(stored)
    assert checks["date_literal_ok"] is False
    assert checks["outcome"] == "fail_to_review"
    # THE TAG NAMES IT — this is the value the router and the triage dashboards key on.
    assert checks["reason"] == "frozen_date_literal"
    assert stored.status == CandidateStatus.IN_REVIEW
    assert derive_inbox_reason(stored) == "fail_to_review"


async def test_the_dated_rewrite_can_never_be_approved(enabled: None) -> None:
    """The reason tag would be decoration if a reviewer could click past it. `static_not_ok` is
    checked BEFORE the golden replay, so a frozen-date rewrite is refused without any warehouse
    round trip — and the refusal is a state error, not a landing-plane degrade."""
    client, store, _ = await _app()
    client.post(
        f"/inbox/{CID}/complete",
        json={
            "entries": [
                _inline("register_type", "EARN", "defines the metric earnings"),
                _inline("pay_date", "30", "a 30-day trailing window"),
            ],
            "sql": DATED_FUNCTION_ARG,
        },
        headers=AUTH,
    )
    assert (await store.get(CID)).status == CandidateStatus.IN_REVIEW

    resp = client.post(f"/inbox/{CID}/approve", json={"token": "warehouse-tok"}, headers=AUTH)

    assert resp.status_code == 409, resp.text
    assert "static_not_ok" in resp.json()["detail"]
    assert (await store.get(CID)).status == CandidateStatus.IN_REVIEW


F3_INLINE_DATE = (
    f"SELECT SUM(amount) AS total FROM {_T} "
    "WHERE register_type = 'EARN' AND pay_date >= '2026-09-01'"
)


async def test_a_run_date_in_a_comparison_still_passes_the_date_check_the_f3_gap(
    enabled: None,
) -> None:
    """⚠ THE KNOWN GAP, PINNED AS CURRENT BEHAVIOUR — NOT AS APPROVAL.

    `check_no_frozen_date_literal` asks one question: did S3 ADJUDICATE this literal? A date on
    the constant side of an ordinary comparison did get adjudicated — it is a literal predicate,
    and here it is classified `inline` — so the check passes. But `inline` means FROZEN: this
    blueprint asks "since 2026-09-01" for ever, which is the same wrong-answer class the check
    exists to stop, arriving through the one door it deliberately leaves open (the F3 inline-
    window freeze, deferred by the frozen-date-literal slice).

    What DOES stop it today is incidental rather than designed, and this test says so out loud:
    the inline date survives into `generalization.sql_template`, the leakage regex battery
    classifies any `20xx-xx-xx` as a `date` entity, and the candidate quarantines. So it is held
    for a human — by the LEAKAGE gate, on a false-positive-shaped finding, not by the date check.
    Remove that coincidence (say, by narrowing the `date` detector) and this rewrite would be a
    clean `hand_authored` hold with a permanently frozen window inside it.

    When F3 lands, this test should FLIP: `date_literal_ok` becomes False and the reason becomes
    `frozen_date_literal`. It is written so that flip is a one-line, deliberate edit.
    """
    client, store, _ = await _app()

    resp = client.post(
        f"/inbox/{CID}/complete",
        json={
            "entries": [
                _inline("register_type", "EARN", "defines the metric earnings"),
                _inline("pay_date", "2026-09-01", "the reporting window opens here"),
            ],
            "sql": F3_INLINE_DATE,
        },
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    stored = await store.get(CID)
    assert _static(stored)["date_literal_ok"] is True  # ⚠ the gap
    assert _static(stored)["outcome"] == "ok"
    # The frozen date is really in the landed template, not templated out.
    assert "2026-09-01" in _gen(stored)["sql_template"]
    # ...and the only thing holding it is the leakage regex reading that date as an entity.
    assert stored.status == CandidateStatus.IN_REVIEW
    assert stored.entity_scan["result"] == "quarantine"
    assert [hit["kind"] for hit in stored.entity_scan["hits"]] == ["date"]
    assert derive_inbox_reason(stored) == "leakage_near_miss"


# --- 3. non-SELECT rewrites ---------------------------------------------------


NON_SELECT = {
    "insert": f"INSERT INTO {_T} (amount) VALUES (1)",
    "drop": f"DROP TABLE {_T}",
    "multi_statement": (
        f"SELECT SUM(amount) AS total FROM {_T} WHERE register_type = 'EARN'; "
        f"DROP TABLE {_T}"
    ),
    "into_outfile": (
        f"SELECT SUM(amount) AS total FROM {_T} WHERE register_type = 'EARN' "
        "INTO OUTFILE '/tmp/exfil.csv'"
    ),
}


@pytest.mark.parametrize("sql", sorted(NON_SELECT.values()))
async def test_the_reviser_discards_a_non_select_rewrite_before_offering_it(sql: str) -> None:
    """PROPOSE TIME: a query that is not a single read-only SELECT costs a sentence on a 200 and
    is never shown to the reviewer at all — adjudicated by `check_read_only_select`, the SAME
    function that will read the derived template later. Extends the existing coverage to the two
    shapes an exfiltration attempt would actually use: a trailing statement after a legitimate
    SELECT, and `INTO OUTFILE`."""
    reviser = BlueprintReviser(model_client=ScriptedClient([rewrite_turn(sql)]))
    env = _declined_envelope()

    proposal = await reviser.propose(env, feedback="rewrite it", allow_sql=True)

    assert proposal.sql_changed is False
    assert proposal.sql == ""
    assert proposal.entries == ()
    assert "read-only SELECT" in proposal.reason


@pytest.mark.parametrize("sql", sorted(NON_SELECT.values()))
async def test_a_non_select_posted_straight_to_apply_is_refused_at_the_write_boundary(
    enabled: None, sql: str
) -> None:
    """⚠ FIXED — the asymmetry this test was written to record is gone. `revise` discarded these
    and the write path stored them; now `complete`/`apply_revision` run the SAME
    `check_read_only_select` on the accepted SQL before anything is snapshotted, so a client with
    the reviewer token gets a 422 instead of a candidate whose stored accepted SQL is a
    `DROP TABLE`.

    CONTAINMENT WAS NEVER THE PROBLEM, which is why this was a defect worth fixing rather than a
    breach worth panicking about: the completer parses SQL and never executes it, the row could
    not be approved (`static_not_ok`), and nothing reached ClickHouse. The cost was what the
    string BECAME once stored — it is quoted back verbatim as "THE ACCEPTED SQL (the query that
    ran)" in the next revise brief, and rendered on a review card. Refusing at the boundary is
    the difference between a system that contains a hostile string and one that does not hold it.

    THE STORE IS UNTOUCHED, asserted rather than assumed: a guard that refuses AFTER
    `_rewritten_snapshot` has replaced `sql_by_ref` would return the right status code and still
    have done the damage.
    """
    client, store, _ = await _app()
    before = (await store.get(CID)).to_doc()

    resp = client.post(
        f"/inbox/{CID}/complete",
        json={"entries": [_inline("register_type", "EARN", "the metric")], "sql": sql},
        headers=AUTH,
    )

    assert resp.status_code == 422, resp.text
    assert "read-only SELECT" in resp.json()["detail"]
    stored = await store.get(CID)
    assert stored.to_doc() == before
    assert stored.revalidation.sql_by_ref == {"tc1": (ACCEPTED_SQL,)}
    assert stored.revalidation.authored is False


@pytest.mark.parametrize(
    "key", ["insert", "drop", "multi_statement"], ids=["insert", "drop", "multi_statement"]
)
async def test_a_ddl_rewrite_never_reaches_the_one_route_that_would_execute_it(
    enabled: None, key: str
) -> None:
    """⚠ THE ONE ROUTE THAT WOULD EXECUTE A REWRITTEN QUERY, and it can no longer be reached.

    `trial_run` binds the stored template and sends it to ClickHouse under the REVIEWER'S OWN
    token — the single surface on this plane where a poisoned `sql_template` becomes a real
    statement. This test used to walk the whole path (post DDL → it is stored → the trial refuses
    on `no_template`/`no_uses_scope`) and pin those two refusals as BACKSTOPS. They still exist
    and are still tested where they belong; what has changed is that the door in front of them is
    shut, so the backstops are no longer the only thing standing there.

    What is pinned now is the shape of that shut door: the DDL is refused, and the row still
    carries the SESSION'S query — so the candidate a reviewer trial-runs is the one the warehouse
    already answered, which is the property the whole rewrite feature is careful about.
    """
    client, store, _ = await _app()

    resp = client.post(
        f"/inbox/{CID}/complete",
        json={
            "entries": [_inline("register_type", "EARN", "the metric")],
            "sql": NON_SELECT[key],
        },
        headers=AUTH,
    )

    assert resp.status_code == 422, resp.text
    stored = await store.get(CID)
    assert stored.revalidation.sql_by_ref == {"tc1": (ACCEPTED_SQL,)}
    assert NON_SELECT[key] not in str(stored.to_doc())


STAR_REWRITE = f"SELECT * FROM {_T} WHERE register_type = 'EARN'"


async def test_a_select_star_rewrite_is_refused_on_both_halves_now(enabled: None) -> None:
    """⚠ FIXED, and it is the case that made the fix worth making rather than arguing about.

    `SELECT *` is not DDL. It parses, it produces a template, and its declared footprint is EVERY
    COLUMN OF THE TABLE — so unlike the DDL shapes it used to survive into the store and
    `trial_run` would happily send it to the warehouse. Not an escalation (the trial borrows the
    reviewer's own token and mints nothing), but it was the shape that showed the write path was
    not applying a check the propose path had, on a query the reviewer would never have been
    offered.

    Both halves now run the same `check_read_only_select`, so it is refused wherever it arrives.

    ⚠ THE STANDING OBSERVATION THIS TEST CARRIED IS STILL TRUE AND STILL UNFIXED: `trial_run` has
    no `static_validation.outcome == "ok"` gate. Approve has one; the trial does not, so "this
    candidate failed its checks" is not a reason the trial refuses. A rewrite can no longer
    produce such a row, but any other route to a `fail_to_review` candidate still can. That is a
    finding about `trial_run`, not about §C.5, and it is left where it belongs rather than
    asserted here through a door that is now closed.
    """
    client, store, _ = await _app()
    before = (await store.get(CID)).to_doc()

    resp = client.post(
        f"/inbox/{CID}/complete",
        json={"entries": [_inline("register_type", "EARN", "the metric")], "sql": STAR_REWRITE},
        headers=AUTH,
    )

    assert resp.status_code == 422, resp.text
    assert "SELECT *" in resp.json()["detail"]
    assert (await store.get(CID)).to_doc() == before
    # ...and the reviser refuses the identical string, which is the point: ONE definition of
    # "a query this system will hold", applied wherever one arrives.
    reviser = BlueprintReviser(model_client=ScriptedClient([rewrite_turn(STAR_REWRITE)]))
    proposal = await reviser.propose(_declined_envelope(), feedback="f", allow_sql=True)
    assert proposal.sql_changed is False


# --- 4/5. footprint: off-catalog tables and scratch ---------------------------


async def test_a_rewrite_reading_a_table_the_catalog_does_not_have_fails_closed(
    enabled: None,
) -> None:
    """The provenance extractor cannot qualify a column against a table it has never heard of, so
    `explain_ok` is False and `uses` is EMPTY — the fail-CLOSED direction. It is worth asserting
    that `uses` is empty rather than merely that the outcome failed: an unqualifiable table that
    UNDER-declared its footprint instead would be the shape that quietly passes a scope check."""
    client, store, _ = await _app()

    resp = client.post(
        f"/inbox/{CID}/complete",
        json={
            "entries": [_inline("register_type", "EARN", "the metric")],
            "sql": "SELECT SUM(amount) AS total FROM shadow_db.shadow_payroll "
            "WHERE register_type = 'EARN'",
        },
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    stored = await store.get(CID)
    assert _static(stored)["explain_ok"] is False
    assert _static(stored)["reason"] == "explain_failed"
    assert _gen(stored)["uses"] == []
    assert stored.status == CandidateStatus.IN_REVIEW


async def test_a_rewrite_reading_scratch_fails_closed_as_d64_requires(enabled: None) -> None:
    """⚠ D64: a `scratch.*` read with NO BOUND SESSION is refused, and a single blueprint has no
    session and declares no table intermediate, so every `scratch.*` in a rewrite is that case.

    The mechanism is what matters: `_provenance_uses` passes `declared_scratch=frozenset()` for a
    single template, the extractor raises, and the candidate is stamped `explain_ok=False,
    uses=()`. A rewrite therefore cannot borrow another session's materialized intermediate, and
    it cannot smuggle one in under a semantic-looking name either — both spellings fail the same
    way."""
    client, store, _ = await _app()

    for name in ("s_sess42_earnings", "emp_earnings"):
        resp = client.post(
            f"/inbox/{CID}/complete",
            json={
                "entries": [_inline("register_type", "EARN", "the metric")],
                "sql": f"SELECT SUM(amount) AS total FROM scratch.{name} "
                "WHERE register_type = 'EARN'",
            },
            headers=AUTH,
        )
        assert resp.status_code == 200, resp.text
        stored = await store.get(CID)
        assert _static(stored)["explain_ok"] is False, name
        assert _gen(stored)["uses"] == [], name
        assert stored.status == CandidateStatus.IN_REVIEW
        # Reset to the form queue so the next spelling exercises the same path.
        await store.put(replace(stored, status=CandidateStatus.NEEDS_PARAMETERIZATION))


async def test_a_rewrite_may_move_the_blueprint_onto_a_different_catalog_table(
    enabled: None,
) -> None:
    """⚠ CURRENT BEHAVIOUR, ASSERTED SO IT IS A DECISION RATHER THAN AN ACCIDENT.

    A rewrite is not constrained to the tables the SESSION touched. Point it at another catalog
    table entirely and every check passes: the template resolves, `uses` is recomputed WHOLESALE
    from the new query, and the blueprint now declares a footprint the session never had. Nothing
    compares the new `uses` against the old one.

    That is defensible only because of `hand_authored`: the row cannot auto-land, so a human sees
    the changed footprint on the card before anything is promoted. It is also the sharpest reason
    that hold must never be weakened — it is the ONLY thing standing between a rewrite and a
    silently re-pointed blueprint.
    """
    client, store, _ = await _app()

    resp = client.post(
        f"/inbox/{CID}/complete",
        json={
            "entries": [_slot("department_code", "X1", table="dbpcm_warehouse.employee")],
            "sql": "SELECT annual_salary FROM dbpcm_warehouse.employee "
            "WHERE department_code = 'X1'",
        },
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    stored = await store.get(CID)
    assert _static(stored)["outcome"] == "ok"
    assert _gen(stored)["uses"] == [
        "dbpcm_warehouse.employee.annual_salary",
        "dbpcm_warehouse.employee.department_code",
    ]
    assert not any(u.startswith(_T) for u in _gen(stored)["uses"])  # payroll is GONE
    assert stored.status == CandidateStatus.IN_REVIEW
    assert derive_inbox_reason(stored) == "hand_authored"


# --- 6. composite -------------------------------------------------------------


_COMPOSES = [
    {
        "order": 0,
        "source_tool_call_ref": "tc1",
        "output": {"headcount": "scalar"},
        "feeds_from": [],
        "consumes": {},
    },
    {
        "order": 1,
        "source_tool_call_ref": "tc2",
        "output": {},
        "feeds_from": [0],
        "consumes": {"headcount": "$0.headcount"},
    },
]


def _composite_envelope():
    return _declined_envelope(
        kind="composite",
        composes=_COMPOSES,
        tool_calls=(
            make_tool_call(ref="tc1", sql=ACCEPTED_SQL),
            make_tool_call(ref="tc2", sql=f"SELECT count() AS n FROM {_T}"),
        ),
    )


async def test_a_composite_refuses_the_rewrite_at_propose_time() -> None:
    """The reviser will not even ask the model: a composite's SQL lives on its NODES, one query
    per step, so "the SQL" is not a single string a rewrite could be. Pinned here as the CONTRAST
    for the test below — the guard exists, and it exists on the half that writes nothing."""
    reviser = BlueprintReviser(model_client=ScriptedClient([rewrite_turn(CLEAN_REWRITE)]))

    with pytest.raises(Exception) as exc:
        await reviser.propose(_composite_envelope(), feedback="rewrite it", allow_sql=True)

    assert "composite" in str(exc.value)


@pytest.mark.parametrize("route", ["complete", "apply_revision"])
async def test_a_composite_rewrite_posted_directly_is_refused(
    enabled: None, route: str
) -> None:
    """⚠ FIXED — the composite guard used to live ONLY on the propose half, and the write half
    destroyed the candidate.

    `BlueprintReviser.propose` refuses a composite rewrite before the model call, for a reason
    about the DATA rather than the model: a composite has one accepted query PER NODE, so a
    single replacement string is not a thing it can hold. `ParameterizationCompleter.complete`
    had no such check. It called `_rewritten_snapshot`, which does `sql_by_ref={"rewrite0":
    (sql,)}` — REPLACING the whole per-ref map — and then set
    `payload["source_tool_call_refs"] = ["rewrite0"]`.

    So a two-node composite whose snapshot held `{"tc1": ..., "tc2": ...}` came out holding one
    ref and one query, while `payload["kind"]` still said `"composite"` and `payload["composes"]`
    still described two steps wired by a DAG. The accepted SQL of BOTH nodes was gone — recorded
    only as a sha256 of node 1's — so the row could never be completed correctly again, and a
    subsequent `revise` would propose against a query belonging to no node. It did not LAND
    (`dag_ok` false ⇒ `fail_to_review`), which is why this was data loss rather than a leak.

    The guard now sits in the completer beside the `replace_all` one, so BOTH entry points share
    it, and both halves share the sentence explaining why (`COMPOSITE_REWRITE_REASON`).

    WHAT THIS PINS: the refusal, and — the half that would still be a defect on its own — that
    the composite snapshot, its `kind` and its two-node DAG are all exactly as they were.
    """
    env = _composite_envelope()
    if route == "apply_revision":
        env = replace(env, status=CandidateStatus.IN_REVIEW, decline=None)
    client, store, _ = await _app(env)
    before = (await store.get(CID)).revalidation.sql_by_ref
    assert set(before) == {"tc1", "tc2"}  # one accepted query per node

    resp = client.post(
        f"/inbox/{CID}/{route}",
        json={"entries": CLEAN_ENTRIES, "sql": CLEAN_REWRITE},
        headers=AUTH,
    )

    assert resp.status_code >= 400, (
        "a composite rewrite must be refused on the WRITE path too, not only by the reviser; "
        f"got {resp.status_code} {resp.text}"
    )
    stored = await store.get(CID)
    # The damage the refusal would prevent: both nodes' accepted SQL replaced by one string,
    # on a row that still calls itself composite and still carries a two-node DAG.
    assert stored.revalidation.sql_by_ref == before
    assert stored.payload["kind"] == "composite"
    assert len(stored.payload["composes"]) == 2
    assert "sql_rewrite" not in stored.payload


# --- 7/8. size and echo -------------------------------------------------------


_OVERSIZED = (
    f"SELECT SUM(amount) AS total FROM {_T} WHERE register_type = 'EARN' -- "
    + "x" * (MAX_SQL_CHARS + 1)
)


async def test_the_reviser_drops_an_oversized_rewrite_rather_than_truncating_it() -> None:
    """PROPOSE TIME, and the cap is MINT'S cap, imported rather than restated. A truncated query
    is a DIFFERENT query that might still parse, which is the one way this path could hand a
    reviewer something to approve that no model ever wrote."""
    reviser = BlueprintReviser(model_client=ScriptedClient([rewrite_turn(_OVERSIZED)]))

    proposal = await reviser.propose(_declined_envelope(), feedback="f", allow_sql=True)

    assert proposal.sql_changed is False
    assert proposal.sql == ""


async def test_an_oversized_sql_posted_by_a_client_is_refused(enabled: None) -> None:
    """⚠ FIXED — the size cap used to be enforced on the MODEL boundary only, never the CLIENT one.

    `learning/mint` — the plane §C.5 borrows its entire safety argument from — caps a
    hand-authored query at BOTH boundaries: `mint/schema.py` rejects an oversized model response,
    and `MintRequest.__post_init__` rejects an oversized submission from a human. The rewrite path
    had only the first half: `revise/schema.py` dropped an oversized model rewrite, and nothing
    checked the `sql` a client posted — not `CompleteParameterizationRequest`, not
    `ReviewInbox.complete_parameterization`, not `ParameterizationCompleter.complete`.

    The result was not a rejected row. The oversized query below is a perfectly good SELECT with a
    20 001-character comment welded on, so it PASSED every static check and was stored at
    `in_review` as a clean `hand_authored` candidate ready to approve — the same
    unbounded-string-into-a-persisted-payload shape the mint cap exists to stop, reached through
    the door added later, and a plain denial-of-service surface for the store, the card renderer
    and every later prompt that quotes the accepted SQL.

    `complete()` now applies the cap through `max_rewrite_sql_chars()`, the SAME accessor the
    model boundary uses, so the two can never drift. REFUSED, never truncated: a truncated query
    is a different query that might still parse, which is the one way this path could hand a
    reviewer something to approve that nobody wrote.
    """
    client, store, _ = await _app()

    resp = client.post(
        f"/inbox/{CID}/complete",
        json={"entries": [_inline("register_type", "EARN", "the metric")], "sql": _OVERSIZED},
        headers=AUTH,
    )

    assert resp.status_code >= 400, (
        "an oversized rewrite must be refused at the client boundary, as mint refuses one; "
        f"got {resp.status_code} and outcome={resp.json().get('outcome')}"
    )
    stored = await store.get(CID)
    assert stored.revalidation.sql_by_ref == {"tc1": (ACCEPTED_SQL,)}


async def test_posting_the_current_sql_back_is_not_a_rewrite_over_http(enabled: None) -> None:
    """⚠ THE ECHO CASE AT THE ROUTE, where it actually happens: a form that renders the accepted
    SQL in a textarea round-trips it on every submit. Treating that as a rewrite would cost EVERY
    ordinary completion its provenance and its ability to auto-land, in exchange for no change at
    all — so the comparison is against `last_sql`, the same function the reviser showed the model.

    Whitespace-padded on purpose: a textarea adds it, and a byte comparison would call that a
    different query."""
    client, store, _ = await _app()

    resp = client.post(
        f"/inbox/{CID}/complete",
        json={"entries": ACCEPTED_ENTRIES, "sql": f"\n  {ACCEPTED_SQL}\n", "replace": True},
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["sql_rewritten"] is False
    stored = await store.get(CID)
    assert stored.revalidation.sql_by_ref == {"tc1": (ACCEPTED_SQL,)}  # untouched, old ref
    assert stored.revalidation.authored is False
    assert "sql_rewrite" not in stored.payload
    assert REWRITE_TOOL_CALL_REF not in str(stored.payload["source_tool_call_refs"])
    # ...and with no authored stamp the ordinary routing rules decide, not `hand_authored`.
    assert derive_inbox_reason(stored) != "hand_authored"


# --- 10. rewriting a rewrite --------------------------------------------------


SECOND_REWRITE = (
    f"SELECT SUM(amount) AS total FROM {_T} "
    "WHERE register_type = 'DDUCT' AND department_code = '0420'"
)


async def test_a_second_rewrite_re_bases_the_digest_and_leaves_no_stale_refs(
    enabled: None,
) -> None:
    """⚠ THE ONE-REF INVARIANT UNDER REPEAT APPLICATION. `rewrite0` is a fixed name, so a second
    rewrite writes over the first rather than accumulating `rewrite1`, `rewrite2`… — which is
    right (a blueprint has ONE accepted query) but only if nothing else accumulates either.

    Three things are checked because each could rot independently: the digest must re-base onto
    the query being REPLACED (the first rewrite, not the session's original — otherwise the badge
    claims a lineage that skips a step), the evidence must stay a single pointer at the single
    ref, and no `tc1` may survive anywhere in `source_tool_call_refs` or the citations. A stale
    `tc1` would be a citation into a snapshot that no longer holds that ref, which is exactly the
    `no_evidence` decline `_raw_candidate` was written to avoid.
    """
    env = replace(_declined_envelope(), status=CandidateStatus.IN_REVIEW, decline=None)
    client, store, _ = await _app(env)

    first = client.post(
        f"/inbox/{CID}/apply_revision",
        json={"entries": CLEAN_ENTRIES, "sql": CLEAN_REWRITE},
        headers=AUTH,
    )
    assert first.json()["sql_rewritten"] is True
    after_first = await store.get(CID)
    assert after_first.payload["sql_rewrite"]["previous_sql_sha256"] == hashlib.sha256(
        ACCEPTED_SQL.encode("utf-8")
    ).hexdigest()

    second = client.post(
        f"/inbox/{CID}/apply_revision",
        json={
            "entries": [
                _inline("register_type", "DDUCT", "defines the metric deductions"),
                _slot("department_code", "0420"),
            ],
            "sql": SECOND_REWRITE,
        },
        headers=AUTH,
    )

    assert second.status_code == 200, second.text
    assert second.json()["sql_rewritten"] is True
    stored = await store.get(CID)
    # RE-BASED onto the query it replaced, not onto the session's original.
    assert stored.payload["sql_rewrite"]["previous_sql_sha256"] == hashlib.sha256(
        CLEAN_REWRITE.encode("utf-8")
    ).hexdigest()
    assert stored.revalidation.sql_by_ref == {REWRITE_TOOL_CALL_REF: (SECOND_REWRITE,)}
    assert len(stored.revalidation.evidence) == 1
    assert stored.revalidation.evidence[0].tool_call_ref == REWRITE_TOOL_CALL_REF
    assert stored.payload["source_tool_call_refs"] == [REWRITE_TOOL_CALL_REF]
    assert "tc1" not in str(stored.payload["source_tool_call_refs"])
    assert all(p.tool_call_ref != "tc1" for p in stored.revalidation.evidence)
    assert stored.status == CandidateStatus.IN_REVIEW
    assert derive_inbox_reason(stored) == "hand_authored"


# --- 12. the leakage gate over a rewritten query ------------------------------


LEAKY_REWRITE = (
    f"SELECT SUM(amount) AS total FROM {_T} WHERE employee_code = 'E12345'"
)


async def test_a_rewrite_that_inlines_an_entity_is_quarantined_and_then_locks_the_assistant(
    enabled: None,
) -> None:
    """⚠ THE FULL LOOP OF THE ONE GUARD A REWRITE CAN TRIP THAT RE-ROLING CANNOT.

    Re-roling literals can only ever move a value the session already had between `slot` and
    `inline`. A REWRITE can introduce a value that was never in the session at all — here an
    employee code — and classify it `inline`, which freezes it into the template that LANDS.

    The gate sees it because it scans `generalization.sql_template`, not just `intent`, so the
    finding is attributed to the template and the candidate quarantines rather than passing.

    Then the second half, which is the part worth having as one test rather than two: the
    quarantine LOCKS THE ASSISTANT OUT of this row. `propose_revision` refuses before the model
    call, because a proposal necessarily quotes the accepted SQL's literal values — the very ones
    this row is withholding from the card — and a redacted literal would match no predicate
    anyway. The refusal names the flagged FIELD AND KIND and never the span. Without this, a
    reviewer could read the withheld value straight off the assistant's suggestion, through a
    route added to make the form easier.
    """
    client, store, scripted = await _app(turns=[rewrite_turn(CLEAN_REWRITE)])

    resp = client.post(
        f"/inbox/{CID}/complete",
        json={
            "entries": [_inline("employee_code", "E12345", "this employee defines the metric")],
            "sql": LEAKY_REWRITE,
        },
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    stored = await store.get(CID)
    assert stored.entity_scan["result"] == "quarantine"
    assert [(h["field"], h["kind"]) for h in stored.entity_scan["hits"]] == [
        ("generalization.sql_template", "employee_code")
    ]
    assert stored.status == CandidateStatus.IN_REVIEW
    assert derive_inbox_reason(stored) == "leakage_near_miss"

    revised = client.post(
        f"/inbox/{CID}/revise",
        json={"feedback": "make it a slot", "allow_sql": True},
        headers=AUTH,
    )

    assert revised.status_code == 200, revised.text
    body = revised.json()
    assert body["entries"] == []
    assert body["sql"] == ""
    assert body["sql_changed"] is False
    assert "quarantine" in body["reason"]
    assert "sql_template (employee_code)" in body["reason"]
    assert "E12345" not in body["reason"]  # the span is the value being withheld
    assert scripted.calls == []  # the model was never asked, so nothing quoted it


# --- 11. a declined rewrite is what the next attempt works from ---------------


async def test_a_declined_rewrite_becomes_the_sql_the_next_revise_proposes_against(
    enabled: None,
) -> None:
    """⚠ THE FORM AND ITS OWN ERROR MESSAGE MUST AGREE, PROVED THROUGH THE ASSISTANT.

    A rewrite whose entries do not cover every literal predicate declines — and the NEW snapshot
    is persisted anyway, because the entries the reviewer left are written against the NEW query.
    The existing coverage stops at "the store holds the new SQL". This goes one step further and
    asks the only consumer that matters: the next `revise` call must BRIEF the model on the
    rewritten query, or the assistant will keep proposing entries for a query nobody has.

    So the brief is inspected directly. It must carry the rewritten SQL and must NOT carry the
    session's original — a brief containing both would be worse than one containing the wrong
    one, because the model would silently pick.
    """
    # The scripted turn carries NO `sql`: the follow-up revise below does not opt in, and a
    # response with a `sql` field on a request that never licensed one is a 422 by design.
    client, store, scripted = await _app(turns=[plain_turn()])

    declined = client.post(
        f"/inbox/{CID}/complete",
        json={
            # Deliberately short: `type_code`/`department_code` go unclassified.
            "entries": [_inline("register_type", "EARN", "defines the metric earnings")],
            "sql": CLEAN_REWRITE,
        },
        headers=AUTH,
    )
    assert declined.status_code == 200, declined.text
    assert declined.json()["outcome"] == "declined"
    assert declined.json()["decline"]["reason"] == "totality_violation"
    stored = await store.get(CID)
    assert stored.status == CandidateStatus.NEEDS_PARAMETERIZATION
    assert stored.revalidation.sql_by_ref == {REWRITE_TOOL_CALL_REF: (CLEAN_REWRITE,)}
    assert stored.revalidation.authored is True

    assert client.post(
        f"/inbox/{CID}/revise", json={"feedback": "finish the entries"}, headers=AUTH
    ).status_code == 200

    (messages, _tools) = scripted.calls[0]
    brief = "\n".join(m["content"] for m in messages)
    assert CLEAN_REWRITE in brief
    assert ACCEPTED_SQL not in brief


# --- 13. one pipeline, not two that agree -------------------------------------


class _FakePriorArt:
    """A stand-in WITH IDENTITY: the assertions below are about which object the stage holds,
    not about it holding something of the right shape."""


class _FakeJudge:
    pass


def _parity_completer(tmp_path, **collaborators):
    import json as _json

    from data_agent.learning.config import LearningSettings
    from data_agent.learning.inbox.service import _build_completer

    path = tmp_path / "catalog_export.json"
    path.write_text(_json.dumps({"catalog": fixture_catalog()}), encoding="utf-8")

    class _Runtime:
        def catalog_fixture_file(self):
            return path

    return _build_completer(
        LearningSettings(_env_file=None),
        _Runtime(),
        candidate_store=InMemoryCandidateStore(),
        corpus=object(),
        embedding_client=object(),
        **collaborators,
    )


def _stage_by_id(stages, stage_id: str):
    return next(s for s in stages if s.stage_id == stage_id)


def test_the_two_pipelines_are_the_same_classes_not_merely_the_same_ids(tmp_path) -> None:
    """⚠ STAGE IDS ARE STRINGS A STAGE CHOOSES FOR ITSELF. The existing parity test compares
    them, which is the right thing to READ in a log — but a stage class swapped for another
    declaring the same `stage_id` would satisfy it while running different code on the path
    hand-authored SQL enters. Comparing the TYPES closes that, and costs one line."""
    from data_agent.learning.config import LearningSettings
    from data_agent.learning.factory import build_write_router_stages

    consumer = build_write_router_stages(
        LearningSettings(_env_file=None),
        candidate_store=InMemoryCandidateStore(),
        blueprint_corpus=object(),  # type: ignore[arg-type]
        catalog_schema={},
        embedder=object(),  # type: ignore[arg-type]
        include_target_specific=False,
    )
    completion = _parity_completer(tmp_path).stages

    assert [type(s) for s in completion] == [type(s) for s in consumer]
    assert [s.stage_id for s in completion] == ["generalize", "leakage", "dedup", "writer"]


def test_the_completion_leakage_stage_shares_the_completers_own_candidate_store(
    tmp_path,
) -> None:
    """The factory's SHARED-SINGLETON invariant, checked on the one plane where a second store
    would be silent: S5 reads sibling candidates through `candidate_store`, and a leakage stage
    pointed at a different store than the one the completer persists to would scan a population
    that does not include the row being written."""
    completer = _parity_completer(tmp_path)
    assert _stage_by_id(completer.stages, "leakage").candidate_store is completer.store


def test_prior_art_without_a_judge_keeps_the_index_drops_the_judge_and_says_so(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """⚠ THE HALF-WIRED CASE, which is the one a real deployment lands in: the graph is up (so
    there IS a prior-art index) and the coverage judge is off by its own kill-switch.

    Two things must be simultaneously true and neither is implied by the other. The cross-tier
    dedup layer must still RUN — the index is present, so a rewritten blueprint is still compared
    against the MCP canon and the landed learning tier — while S6's layer-3b judged near-miss
    must be OFF and SAID SO. The existing degrade test accepts either warning by substring; this
    one pins which of the two fired, because "some warning appeared" would pass with the prior-art
    line and the judge silently missing.
    """
    index = _FakePriorArt()
    with caplog.at_level("WARNING", logger="data_agent.learning.inbox.service"):
        completer = _parity_completer(tmp_path, prior_art=index, judge=None)

    dedup = _stage_by_id(completer.stages, "dedup")
    assert dedup._prior_art is index  # the cross-tier layer still runs
    assert dedup._judge is None  # ...and layer-3b does not

    messages = [r.message for r in caplog.records]
    judge_lines = [m for m in messages if "NO coverage judge" in m]
    assert judge_lines, messages
    assert "layer-3b" in judge_lines[0]
    # ...and it did NOT also claim the prior-art index was missing.
    assert not any("NO prior-art index" in m for m in messages)


def test_the_half_wired_pipeline_still_matches_the_consumer_built_the_same_way(
    tmp_path,
) -> None:
    """The parity has to hold in the DEGRADED wiring too, or it is a property of the fully-wired
    deployment only — which is not the one that runs. Same inputs, same stages, same objects,
    including the `None`."""
    from data_agent.learning.config import LearningSettings
    from data_agent.learning.factory import build_write_router_stages

    index = _FakePriorArt()
    consumer = build_write_router_stages(
        LearningSettings(_env_file=None),
        candidate_store=InMemoryCandidateStore(),
        blueprint_corpus=object(),  # type: ignore[arg-type]
        catalog_schema={},
        embedder=object(),  # type: ignore[arg-type]
        prior_art=index,
        judge=None,
        include_target_specific=False,
    )
    completion = _parity_completer(tmp_path, prior_art=index, judge=None).stages

    assert [type(s) for s in completion] == [type(s) for s in consumer]
    assert (
        _stage_by_id(completion, "dedup")._prior_art
        is _stage_by_id(consumer, "dedup")._prior_art
    )
    assert _stage_by_id(completion, "dedup")._judge is None
    assert _stage_by_id(consumer, "dedup")._judge is None
