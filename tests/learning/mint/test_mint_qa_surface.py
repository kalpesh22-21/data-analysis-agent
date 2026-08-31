"""QA sweep over the minting SURFACE — the HTTP route, the BFF and the trial-run token.

Tests marked `# DEFECT` fail against the implementation as it stands. The rest close gaps.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.inbox import ReviewInbox
from data_agent.learning.inbox.service import MintBlueprintRequest
from data_agent.learning.promotion.token_minter import SuppliedTokenMinter

from .test_mint_engine import DEPT_SQL
from .test_mint_service import AUTH, BODY, build_client

TOKEN = "reviewer-secret"


@pytest.fixture(autouse=True)
def _reviewer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same fixture `test_mint_service.py` declares — an autouse fixture does not travel
    with an imported helper, and without it every route answers 404."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_TOKEN", TOKEN)


# --- the HTTP body: the composite half of the feature never crosses the wire ---------------


def test_the_request_model_carries_the_declared_dag() -> None:
    """# DEFECT — composite minting is unreachable over HTTP.

    `MintBlueprintRequest` declares six fields and `nodes` is not among them. Pydantic's default
    is to IGNORE unknown keys, so the DAG the page sends (`mint.html` builds `nodes:
    readNodes()`) is dropped before `MintRequest.from_doc` ever sees it — silently, with a 200.
    Everything the engine, the schema module and half the shipped tests do for composites is
    dead code from the browser's point of view.

    """
    assert "nodes" in MintBlueprintRequest.model_fields
    # `submitted_by` was DELETED rather than wired: nothing sent it and nothing read it, so a
    # field that only ever carried a default was noise on an access-controlled write path.


def test_a_composite_submission_mints_a_composite_and_not_a_single() -> None:
    """A multi-step submission must cross HTTP as a DAG, not be flattened into one query.

    `nodes` is now declared on the body model, so the steps reach `MintRequest`. Asserted with
    INDEPENDENT steps, because a step that consumes an earlier one is refused today — see
    `test_a_consuming_step_is_refused_over_http`."""
    from .test_mint_engine import composite_turn

    # A COMPOSITE DRAFT turn: `sql_mode="none"` offers the drafting tool, so the default
    # classify reply would 502 as off-contract rather than exercising the path.
    client, store = build_client([composite_turn()])
    body = {
        **BODY,
        "sql_mode": "none",
        "sql": "",
        "nodes": [
            {"step_intent": "total earnings for the department", "output_name": "dept_total"},
            {"step_intent": "total earnings company-wide", "output_name": "company_total"},
        ],
    }

    response = client.post("/inbox/mint", json=body, headers=AUTH)

    assert response.status_code == 200, response.json()
    stored = asyncio.run(store.get(response.json()["candidate_id"]))
    assert stored.payload["kind"] == "composite"
    assert [n["order"] for n in stored.payload["composes"]] == [0, 1]


def test_a_consuming_step_mints_over_http() -> None:
    """The DAG with a real edge, end to end across the HTTP boundary."""
    from .test_mint_engine import SCALAR_CONSUME_SQL, composite_turn

    client, store = build_client(
        [composite_turn(nodes=[{"order": 0, "sql": DEPT_SQL},
                               {"order": 1, "sql": SCALAR_CONSUME_SQL}])]
    )
    body = {
        **BODY,
        "sql_mode": "none",
        "sql": "",
        "nodes": [
            {"step_intent": "dept total", "output_name": "dept_total"},
            {"step_intent": "share", "feeds_from": [0]},
        ],
    }

    response = client.post("/inbox/mint", json=body, headers=AUTH)

    assert response.status_code == 200, response.json()
    stored = asyncio.run(store.get(response.json()["candidate_id"]))
    assert stored.payload["composes"][1]["consumes"] == {"dept_total": "$0.dept_total"}

def test_an_exact_composite_over_http_uses_the_experts_per_step_sql() -> None:
    """`exact` mode across HTTP: every step carries its own vouched-for query, and the model is
    handed the classify tool, which has no field to rewrite any of them. The old failure was a
    400 blaming an empty `sql` box the expert had filled in per step."""
    from .test_mint_engine import SCALAR_CONSUME_SQL

    client, store = build_client()
    response = client.post(
        "/inbox/mint",
        json={
            **BODY,
            "sql_mode": "exact",
            "sql": "",
            "nodes": [
                {"step_intent": "dept", "output_name": "dept_total", "sql": DEPT_SQL},
                {"step_intent": "share", "feeds_from": [0], "sql": SCALAR_CONSUME_SQL},
            ],
        },
        headers=AUTH,
    )

    assert response.status_code == 200, response.json()
    snapshot = asyncio.run(store.get(response.json()["candidate_id"])).revalidation
    assert snapshot.sql_by_ref == {"mint0": (DEPT_SQL,), "mint1": (SCALAR_CONSUME_SQL,)}

def _ui_server():
    import ui.server as server

    return server


def test_the_bff_proxies_the_three_minting_routes() -> None:
    """# DEFECT — the minting page is unreachable end to end.

    `mint.html` calls `/api/inbox/mint`, `/api/inbox/mint/schema` and
    `/api/inbox/mint/prior_art`; `inbox.html` links to `/mint`. None of those four routes exist
    on the BFF. `_MINT_HTML` is defined in `ui/server.py` and never referenced, and
    `_INBOX_MODEL_PATH_SEGMENTS` buys a 180s timeout for a path segment nothing can reach.

    `POST /api/inbox/mint/prior_art` does match the per-candidate action route
    (`candidate_id="mint"`, `action="prior_art"`), which is not in `_INBOX_ACTIONS`, so it 404s.
    """
    paths = {
        (m, getattr(r, "path", ""))
        for r in _ui_server().app.routes
        for m in (getattr(r, "methods", None) or set())
    }
    assert ("GET", "/mint") in paths
    assert ("POST", "/api/inbox/mint") in paths
    assert ("GET", "/api/inbox/mint/schema") in paths
    assert ("POST", "/api/inbox/mint/prior_art") in paths


def test_the_inbox_page_link_target_exists() -> None:
    """# DEFECT — `inbox.html` renders `<a href="/mint">Author a blueprint</a>`, which 404s."""
    from pathlib import Path

    html = (Path(__file__).parents[3] / "ui" / "static" / "inbox.html").read_text()
    assert 'href="/mint"' in html  # the link is there …
    paths = {getattr(r, "path", "") for r in _ui_server().app.routes}
    assert "/mint" in paths  # … and this is what it needs


# --- the route's own guards -----------------------------------------------------------------


def test_every_minting_route_is_behind_the_reviewer_token() -> None:
    client, _ = build_client()

    assert client.get("/inbox/mint/schema").status_code == 401
    assert client.post("/inbox/mint/prior_art", json={"question": "q"}).status_code == 401
    assert client.post("/inbox/mint", json=BODY).status_code == 401


def test_a_malformed_body_is_refused_without_reaching_the_model() -> None:
    client, _ = build_client()

    bad_json = client.post(
        "/inbox/mint",
        content=b"{not json",
        headers={**AUTH, "Content-Type": "application/json"},
    )
    wrong_types = client.post(
        "/inbox/mint", json={"question": "q", "tables": "not-a-list"}, headers=AUTH
    )

    assert bad_json.status_code in (400, 422)
    assert wrong_types.status_code in (400, 422)


def test_an_oversized_submission_is_a_400_not_a_500() -> None:
    client, _ = build_client()

    for body in (
        {**BODY, "question": "q" * 2_001},
        {**BODY, "sql": "SELECT 1 -- " + "x" * 20_001, "sql_mode": "exact"},
        {**BODY, "tables": [f"db.t{i}" for i in range(33)]},
        {**BODY, "steps": [f"step {i}" for i in range(25)]},
    ):
        response = client.post("/inbox/mint", json=body, headers=AUTH)
        assert response.status_code == 400, (body.keys(), response.status_code)


def test_the_prior_art_route_answers_200_with_no_minter_and_no_body() -> None:
    client, _ = build_client(with_minter=False)

    response = client.post("/inbox/mint/prior_art", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"prior_art": []}


def test_an_unknown_table_is_a_400_and_not_a_409() -> None:
    """# DEFECT (wrong status on a new guard).

    The server-side table check raises `MintInputError`, and the route maps EVERY
    `MintInputError` from the engine to 409 — a code the route documents as meaning "the row it
    addresses has moved past the point of re-drafting". A submission naming a table this
    deployment does not offer is plain bad input with no row behind it, so a browser (or any
    client) that branches on 409 to say "open the existing candidate" will show a message about
    a candidate that does not exist.

    Expected: 400 for the input errors and 409 only for the already-minted case, which needs
    two exception types (or a flag) rather than one.
    """
    client, _ = build_client()

    response = client.post("/inbox/mint", json={**BODY, "tables": ["nowhere.at_all"]}, headers=AUTH)

    assert response.status_code == 400, response.json()


# --- the trial-run token ---------------------------------------------------------------------


def _scheduler_with(store, probe):
    """A scheduler holding the DEPLOYMENT principal's probe — the thing a blank token must
    never reach."""
    from data_agent.learning.promotion.scheduler import PromotionScheduler

    class _NoHits:
        async def hit_count(self, artifact_id: str) -> int:
            return 0

    return PromotionScheduler(store, probe=probe, hit_counts=_NoHits())


class RecordingProbe:
    def __init__(self) -> None:
        self.runs: list[tuple] = []

    async def run(self, sql, *, grain_columns, column_scope):
        self.runs.append((sql, grain_columns, column_scope))
        raise AssertionError("the deployment probe must never answer a trial")


class FakeMCP:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call_tool(self, name, args, *, jwt, session_id):
        self.calls.append(jwt)
        raise RuntimeError(f"warehouse said no for {jwt}")


async def _validated_blueprint(store) -> str:
    """One candidate a trial run is allowed on."""
    from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus

    env = CandidateEnvelope(
        candidate_id="candidate::trial",
        type="blueprint",
        status=CandidateStatus.VALIDATED,
        payload={
            "intent": "x",
            "kind": "single",
            "generalization": {
                "sql_template": "SELECT gross_pay FROM payroll.payroll_fact",
                "uses": ["payroll.payroll_fact.gross_pay"],
            },
        },
        source_session="s",
        source_trace="",
        evidence_refs=(),
        extractor_rationale="",
        entity_scan={"result": "clean", "hits": []},
        confidence=0.9,
        proposed_action="add",
        depends_on=(),
        content_hash="sha256:t",
    )
    await store.put(env)
    return env.candidate_id


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["", "   ", "\t\n"])
async def test_a_blank_token_is_refused_and_never_falls_back_to_the_deployment(token) -> None:
    store = InMemoryCandidateStore()
    cid = await _validated_blueprint(store)
    probe = RecordingProbe()
    inbox = ReviewInbox(
        store,
        scheduler=_scheduler_with(store, probe),
        mcp_client=FakeMCP(),
    )

    result = await inbox.trial_run(cid, bindings={}, token=token)

    assert result.ok is False
    assert result.reason == "no_token"
    assert probe.runs == [], "a blank token silently ran as the deployment principal"


@pytest.mark.asyncio
async def test_a_deployment_with_no_warehouse_transport_says_no_token() -> None:
    """# DEFECT (a misleading reason).

    `_probe_for` returns `None` when `self._mcp_client is None`, and `trial_run` maps every
    `None` to `no_token`. The reviewer pasted a perfectly good token and the page tells them to
    "paste a warehouse token to run the trial" (`inbox.html`'s copy for that reason), so they
    retry forever against a deployment that has no runQuery transport wired at all.

    Expected: a distinct reason (`no_transport`/`trial_unavailable`).
    """
    store = InMemoryCandidateStore()
    cid = await _validated_blueprint(store)
    inbox = ReviewInbox(store, mcp_client=None)

    result = await inbox.trial_run(cid, bindings={}, token="a-real-token")

    assert result.reason != "no_token"


@pytest.mark.asyncio
async def test_a_clean_trial_never_echoes_the_token(caplog) -> None:
    """Gap closed: on the paths the inbox itself controls, nothing carries the credential."""

    class QuietMCP:
        async def call_tool(self, name, args, *, jwt, session_id):
            raise RuntimeError("COLUMN_SCOPE_VIOLATION: payroll.payroll_fact.ssn")

    store = InMemoryCandidateStore()
    cid = await _validated_blueprint(store)
    inbox = ReviewInbox(store, mcp_client=QuietMCP())
    secret = "eyJ-super-secret-bearer"

    with caplog.at_level(logging.DEBUG):
        result = await inbox.trial_run(cid, bindings={}, token=secret)

    assert secret not in str(result.to_wire())
    assert secret not in repr(result)
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_a_transport_error_mentioning_the_token_is_relayed_verbatim(caplog) -> None:
    """# DEFECT (hardening — an unredacted relay of arbitrary exception text).

    `trial_run` copies `str(exc)` into `TrialRunResult.detail` (which `to_wire` ships to the
    browser) and logs `%r` of the same exception. Neither is filtered, so the token stays secret
    only for as long as EVERY transport, retry wrapper and HTTP library on the path happens not
    to mention it — an invariant nothing here owns and nothing tests. `SuppliedTokenMinter` goes
    to the trouble of overriding `__repr__` for exactly this failure mode, and then the value is
    handed to code that can print anything.

    Expected: the detail is redacted against the supplied token before it is put on the wire or
    in a log.
    """
    store = InMemoryCandidateStore()
    cid = await _validated_blueprint(store)
    inbox = ReviewInbox(store, mcp_client=FakeMCP())
    secret = "eyJ-super-secret-bearer"

    with caplog.at_level(logging.DEBUG):
        result = await inbox.trial_run(cid, bindings={}, token=secret)

    assert secret not in str(result.to_wire()), "the token was returned to the browser"
    assert secret not in caplog.text, "the token was written to the log"


@pytest.mark.asyncio
async def test_the_supplied_token_is_the_one_the_probe_presents() -> None:
    store = InMemoryCandidateStore()
    cid = await _validated_blueprint(store)
    mcp = FakeMCP()
    inbox = ReviewInbox(store, mcp_client=mcp)

    await inbox.trial_run(cid, bindings={}, token="  padded-token  ")

    assert mcp.calls == ["padded-token"], "the pasted token is used verbatim, stripped"


@pytest.mark.asyncio
async def test_the_supplied_minter_ignores_the_column_scope_and_hides_the_token() -> None:
    minter = SuppliedTokenMinter("s3cret")

    assert await minter.mint(["a.b.c"], session_id="x") == "s3cret"
    assert await minter.mint([], session_id="y") == "s3cret"
    assert "s3cret" not in repr(minter)
    assert not hasattr(minter, "__dict__"), "__slots__ keeps it off a dataclass-style repr"
    with pytest.raises(ValueError):
        SuppliedTokenMinter("   ")


def test_the_trial_wire_no_longer_advertises_a_tenant() -> None:
    from data_agent.learning.inbox.inbox import TrialRunResult

    wire = TrialRunResult(ok=False, reason="no_token").to_wire()

    assert "tenant" not in wire
    assert wire["reason"] == "no_token"


# --- prior art: a warning may never break the page ------------------------------------------


class BadCardIndex:
    async def search(self, text, *, kinds=("blueprint",), limit=5):
        class Card:
            id = "bp::1"
            tier = "mcp"
            status = "active"
            intent = "something"
            verified = True
            confidence = None  # a card the mapper cannot round()

        return [Card()]


@pytest.mark.asyncio
async def test_a_card_the_mapper_cannot_read_does_not_break_minting() -> None:
    """# DEFECT (the fail-open promise is narrower than it reads).

    `find_prior_art` wraps only `search()` in the `try`. The card→dict projection runs OUTSIDE
    it, so any card the mapper cannot handle (`round(None, 3)`) raises straight out of `mint`
    and the expert loses the draft — for a WARNING the docstring says "may never break the
    page".
    """
    from .test_mint_engine import classify_turn, exact_request, make_minter

    minter, _, _ = make_minter([classify_turn()])
    minter.prior_art = BadCardIndex()

    result = await minter.mint(exact_request())

    assert result.candidate_id
