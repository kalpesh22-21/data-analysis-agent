"""BFF review-inbox proxy — the flag gate + server-side token attach (UI Slice 2 §3).

Coverage gap the service suite can't reach: `ui/server.py` proxies the browser's
`/api/inbox/*` DATA calls to the dedicated inbox service, holding `REVIEWER_TOKEN`
server-side and attaching it on the hop so the browser never sees it. These tests pin:

  * flag OFF (`REVIEW_INBOX_ENABLED` != "1") ⇒ every inbox surface 404s AT THE BFF,
    BEFORE any network hop (defense in depth — the service enforces the same flag);
  * flag ON ⇒ the proxy attaches `X-Reviewer-Token` server-side and propagates the
    upstream status + JSON body verbatim (a 409/503 reaches the browser unchanged);
  * an unknown action verb 404s at the BFF (the `_INBOX_ACTIONS` allowlist).

The inbox service itself is never started — `httpx.AsyncClient` is faked so no socket
is opened; the point is the BFF plumbing, not the service (covered in
`tests/learning/inbox/test_inbox_service.py`).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import ui.server as server
from fastapi.testclient import TestClient


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    """A drop-in for `httpx.AsyncClient` that records the outbound request instead of
    opening a socket. Shared list `captured` collects (method, url, headers)."""

    captured: list[dict] = []
    # The body the fake upstream answers with. Settable because not every inbox route
    # answers the list shape: `promote` answers a `PromotionEmit` (yaml + PR metadata),
    # and the proxy has to carry THAT back unchanged.
    payload: dict = {"items": [], "count": 0}

    # The timeout the client was constructed with, per instance. Recorded because the
    # inbox proxy varies it by action and the only way to assert that end-to-end is to see
    # what the proxy actually handed httpx — see
    # `test_the_revise_hop_is_constructed_with_the_long_read_timeout`.
    timeouts: list[object] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        _FakeAsyncClient.timeouts.append(kwargs.get("timeout"))

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def request(
        self, method: str, url: str, *, headers: dict, json: object = None
    ) -> _FakeResponse:
        # `json` is captured, not ignored: the fail-to-review `complete` action is the
        # one call that carries a body, and a double that dropped it would keep this
        # suite green while the reviewer's entries never left the BFF.
        type(self).captured.append(
            {"method": method, "url": url, "headers": headers, "json": json}
        )
        return _FakeResponse(200, type(self).payload)


@pytest.fixture
def fake_httpx(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict]]:
    _FakeAsyncClient.captured = []
    _FakeAsyncClient.timeouts = []
    _FakeAsyncClient.payload = {"items": [], "count": 0}
    monkeypatch.setattr(server.httpx, "AsyncClient", _FakeAsyncClient)
    yield _FakeAsyncClient.captured


@pytest.fixture
def client() -> TestClient:
    return TestClient(server.app)


# --- flag OFF ⇒ 404 at the BFF, no network hop -------------------------------


def test_flag_off_page_and_api_routes_404(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    monkeypatch.delenv("REVIEW_INBOX_ENABLED", raising=False)
    assert client.get("/inbox").status_code == 404  # the HTML page
    assert client.get("/api/inbox").status_code == 404
    assert client.get("/api/inbox/health").status_code == 404
    assert client.post("/api/inbox/candidate::x::0/approve").status_code == 404
    # Not one request reached the (would-be) inbox service.
    assert fake_httpx == []


# --- flag ON ⇒ proxy attaches the token server-side --------------------------


def test_proxy_attaches_reviewer_token_server_side(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setattr(server, "REVIEWER_TOKEN", "bff-held-secret")

    resp = client.get("/api/inbox")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "count": 0}
    assert len(fake_httpx) == 1
    hop = fake_httpx[0]
    assert hop["method"] == "GET"
    assert hop["url"].endswith("/inbox")
    # The shared secret is attached on the hop — the browser never sent it.
    assert hop["headers"]["X-Reviewer-Token"] == "bff-held-secret"


def test_list_forwards_valid_status_query_param(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setattr(server, "REVIEWER_TOKEN", "bff-held-secret")

    resp = client.get("/api/inbox", params={"status": "rejected"})
    assert resp.status_code == 200
    hop = fake_httpx[0]
    # The validated status rides through to the upstream /inbox as a query param.
    assert hop["url"].endswith("/inbox?status=rejected")
    assert hop["headers"]["X-Reviewer-Token"] == "bff-held-secret"


def test_list_rejects_unknown_status_at_bff(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    # A status outside `_INBOX_LIST_STATUSES` is a 400 at the BFF — never proxied.
    # (`validated` used to be the example here; it is a listable status since the
    # promotion hop was exposed, so the example moved to states that are still not.)
    assert client.get("/api/inbox", params={"status": "retired"}).status_code == 400
    assert client.get("/api/inbox", params={"status": "quarantined"}).status_code == 400
    assert fake_httpx == []


def test_proxy_action_attaches_token_and_hits_service(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setattr(server, "REVIEWER_TOKEN", "bff-held-secret")

    resp = client.post("/api/inbox/candidate::abc::0/reject")
    assert resp.status_code == 200
    hop = fake_httpx[0]
    assert hop["method"] == "POST"
    # The id is percent-encoded (`::` → `%3A%3A`) before re-interpolation so it is one
    # unambiguous upstream path segment (the service decodes it back). No raw `::`.
    assert hop["url"].endswith("/inbox/candidate%3A%3Aabc%3A%3A0/reject")
    assert "candidate::abc::0" not in hop["url"]
    assert hop["headers"]["X-Reviewer-Token"] == "bff-held-secret"


def test_the_complete_body_rides_through_to_the_service(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """`complete` is the one inbox action that carries a body — the reviewer's
    parameterization entries. The BFF forwards it VERBATIM: the shape is the inbox
    service's contract, and under that the extractor's own readers, which are the only
    ones that can name the fix when an entry is wrong."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setattr(server, "REVIEWER_TOKEN", "bff-held-secret")
    body = {"entries": [{"locator": {"table": "db.t", "column": "c", "value": "x"},
                         "role": "inline", "why": "metric-defining"}], "replace": True}

    resp = client.post("/api/inbox/candidate::abc::review-0/complete", json=body)

    assert resp.status_code == 200
    hop = fake_httpx[0]
    assert hop["url"].endswith("/inbox/candidate%3A%3Aabc%3A%3Areview-0/complete")
    assert hop["json"] == body
    assert hop["headers"]["X-Reviewer-Token"] == "bff-held-secret"


def test_an_oversized_completion_body_is_413_before_any_hop(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """The BFF buffers this body, so the cap is the BFF's own resource protection —
    the same rule the upload route already follows. Rejected HERE, with no network hop:
    forwarding it first would mean the memory was already spent."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setattr(server, "INBOX_BODY_MAX_BYTES", 512)

    resp = client.post(
        "/api/inbox/candidate::abc::review-0/complete",
        json={"entries": [{"why": "x" * 2000}]},
    )

    assert resp.status_code == 413
    assert fake_httpx == []


def test_a_body_within_the_cap_still_passes(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """The control, so the cap cannot be set to zero by accident and look healthy."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setattr(server, "INBOX_BODY_MAX_BYTES", 4096)

    resp = client.post(
        "/api/inbox/candidate::abc::review-0/complete", json={"entries": []}
    )

    assert resp.status_code == 200
    assert len(fake_httpx) == 1


def test_unknown_action_verb_404s_at_bff(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    # A verb outside the {approve,reject,retract,complete,verify,promote} allowlist never
    # reaches the service.
    assert client.post("/api/inbox/candidate::x::0/delete").status_code == 404
    assert client.post("/api/inbox/candidate::x::0/publish").status_code == 404
    assert fake_httpx == []


# --- fail-to-review: the one action that carries a body (QA) ---------------------------


def test_the_complete_body_is_forwarded_verbatim(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """The reviewer's parameterization entries have to survive the hop UNRESHAPED. The
    BFF deliberately holds no schema for them — the inbox service validates the body and,
    behind it, the extractor's own readers do — so what is pinned here is that it passes
    the object through unchanged rather than that it understands it. A BFF that dropped or
    normalized this body would leave the reviewer's work in the browser and the candidate
    declining for entries it never received."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setattr(server, "REVIEWER_TOKEN", "bff-held-secret")
    body = {
        "entries": [
            {
                "locator": {"table": "payroll.payroll_fact", "column": "region", "value": "NA"},
                "role": "inline",
                "why": "the report is defined for one region",
            }
        ],
        "replace": True,
    }

    resp = client.post("/api/inbox/candidate::abc::review-0/complete", json=body)

    assert resp.status_code == 200
    hop = fake_httpx[0]
    assert hop["method"] == "POST"
    assert hop["url"].endswith("/inbox/candidate%3A%3Aabc%3A%3Areview-0/complete")
    assert hop["json"] == body
    assert hop["headers"]["X-Reviewer-Token"] == "bff-held-secret"


def test_complete_without_a_json_body_is_400_and_never_proxied(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    resp = client.post(
        "/api/inbox/candidate::abc::review-0/complete",
        content=b"not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert fake_httpx == []


def test_every_other_action_still_hops_without_a_body(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """`complete` and `promote` are the only actions with a body. The others must keep
    sending none — a `null` body on an upstream route that takes no body is the kind of
    thing that works until a stricter server rejects it."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    for action in ("approve", "reject", "retract", "verify"):
        assert client.post(f"/api/inbox/candidate::abc::0/{action}").status_code == 200
    assert [hop["json"] for hop in fake_httpx] == [None, None, None, None]


def test_the_fail_to_review_work_list_is_a_permitted_status(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """The new listing has to be reachable through the BFF, and the allowlist has to stay
    an allowlist: a neighbouring lifecycle state is still a 400 that never leaves the
    process."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setattr(server, "REVIEWER_TOKEN", "bff-held-secret")

    ok = client.get("/api/inbox", params={"status": "needs_parameterization"})

    assert ok.status_code == 200
    assert fake_httpx[0]["url"].endswith("/inbox?status=needs_parameterization")
    assert client.get("/api/inbox", params={"status": "extracted"}).status_code == 400
    assert client.get("/api/inbox", params={"status": "quarantined"}).status_code == 400
    assert len(fake_httpx) == 1  # neither rejected status left the BFF


def test_the_complete_route_is_gated_by_the_flag_like_every_other(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """The flag gate is checked BEFORE the body is read, so a dormant deployment answers
    404 for a completion attempt rather than 400-ing on its payload — the surface does not
    exist, and it must not leak that it might."""
    monkeypatch.delenv("REVIEW_INBOX_ENABLED", raising=False)
    resp = client.post(
        "/api/inbox/candidate::abc::review-0/complete", json={"entries": []}
    )
    assert resp.status_code == 404
    assert fake_httpx == []


# --- the promotion hop: verify + promote through the BFF (M4-P1) ---------------


def test_verify_and_promote_are_proxied_with_the_reviewer_token(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """The promotion hop existed in the service and was unreachable from the browser: the
    BFF allowlist stopped at {approve,reject,retract,complete}, so a reviewer had no way
    to vouch for an auto-landed node or get its canon YAML. Both verbs now reach the
    service on the same server-held-token hop as every other action."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setattr(server, "REVIEWER_TOKEN", "bff-held-secret")

    for action in ("verify", "promote"):
        assert (
            client.post(f"/api/inbox/candidate::abc::0/{action}").status_code == 200
        ), action

    assert [hop["url"].rsplit("/", 1)[-1] for hop in fake_httpx] == ["verify", "promote"]
    for hop in fake_httpx:
        assert hop["method"] == "POST"
        # Same id encoding as every other action — one unambiguous path segment.
        assert "candidate%3A%3Aabc%3A%3A0" in hop["url"]
        assert hop["headers"]["X-Reviewer-Token"] == "bff-held-secret"


def test_the_promote_response_body_passes_through_untouched(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """`promote` is the one action whose RESPONSE is not the `{candidate_id, type, status,
    reason}` action shape — it is a `PromotionEmit`: the MCP-format YAML plus the metadata
    a human opens the PR with. The proxy must stay body-agnostic; a BFF that projected
    the action shape onto it would drop the YAML, which is the entire payload of the hop."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setattr(server, "REVIEWER_TOKEN", "bff-held-secret")
    emit = {
        "yaml": "id: bp_abc\nintent: headcount by department\nsql: SELECT 1\n",
        "filename": "headcount-by-department.yaml",
        "target_path": "app/corpus/data/blueprints/",
        "suggested_branch": "learning/promote/bp_abc",
        "commit_message": "corpus: promote learning blueprint bp_abc",
        "note": (
            "regenerate the corpus SHA sidecars (tools/check_corpus_parity.py --write) "
            "in the PR"
        ),
    }
    _FakeAsyncClient.payload = emit

    resp = client.post("/api/inbox/candidate::abc::0/promote")

    assert resp.status_code == 200
    # Byte-for-byte the upstream object: every field, unreshaped, YAML included.
    assert resp.json() == emit


def test_the_optional_promote_body_rides_through_to_the_service(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """`promote` accepts `{doc_id, title}` knowledge refinements. They are the human's
    naming of a document whose landed id is non-semantic, so they must reach the service
    verbatim — and a bodyless promote (the normal case) must still send NO body, not a
    `null` one."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setattr(server, "REVIEWER_TOKEN", "bff-held-secret")
    body = {"doc_id": "payroll-cutoff", "title": "Payroll cutoff is the 25th"}

    with_body = client.post("/api/inbox/candidate::abc::0/promote", json=body)
    without_body = client.post("/api/inbox/candidate::abc::0/promote")

    assert with_body.status_code == 200
    assert without_body.status_code == 200
    assert fake_httpx[0]["json"] == body
    assert fake_httpx[1]["json"] is None


def test_an_oversized_promote_body_is_413_before_any_hop(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """The body cap is the BFF's own resource protection and applies to EVERY body it
    buffers — `promote` is now one of them, so it cannot be the way around the cap."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setattr(server, "INBOX_BODY_MAX_BYTES", 512)

    resp = client.post(
        "/api/inbox/candidate::abc::0/promote", json={"title": "x" * 2000}
    )

    assert resp.status_code == 413
    assert fake_httpx == []


def test_the_promotable_list_is_a_permitted_status(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """`validated` is the promotable set — auto-landed learning nodes awaiting a human
    verify/promote. The service has listed them all along (`_LISTABLE_STATUSES`); the BFF
    allowlist was what made them unreachable, so a reviewer could not see the queue the
    two new verbs act on."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setattr(server, "REVIEWER_TOKEN", "bff-held-secret")

    resp = client.get("/api/inbox", params={"status": "validated"})

    assert resp.status_code == 200
    assert fake_httpx[0]["url"].endswith("/inbox?status=validated")
    assert fake_httpx[0]["headers"]["X-Reviewer-Token"] == "bff-held-secret"
    assert "validated" in server._INBOX_LIST_STATUSES


def test_the_promotion_verbs_are_gated_by_the_flag_like_every_other(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """A dormant deployment must not grow a promotion surface: the flag gate is checked
    before the allowlist and before any body is read."""
    monkeypatch.delenv("REVIEW_INBOX_ENABLED", raising=False)
    assert client.post("/api/inbox/candidate::abc::0/verify").status_code == 404
    assert (
        client.post("/api/inbox/candidate::abc::0/promote", json={"title": "t"}).status_code
        == 404
    )
    assert fake_httpx == []


def test_the_promoted_list_is_a_permitted_status(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """`promoted` is terminal, and listable anyway: `promote` re-emits from it with no
    status move, so the YAML behind an abandoned PR stays recoverable — but only for a
    caller who can still find the row. With the status unlistable, that affordance
    existed in `inbox.py` and was reachable by curl and by nothing else."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setattr(server, "REVIEWER_TOKEN", "bff-held-secret")

    resp = client.get("/api/inbox", params={"status": "promoted"})

    assert resp.status_code == 200
    assert fake_httpx[0]["url"].endswith("/inbox?status=promoted")
    assert fake_httpx[0]["headers"]["X-Reviewer-Token"] == "bff-held-secret"
    assert "promoted" in server._INBOX_LIST_STATUSES


def test_the_bff_and_service_status_allowlists_agree(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """Two allowlists, one contract. A status the BFF forwards but the service will not
    list is a 400 the reviewer sees as an empty tab; a status the service lists but the
    BFF blocks is a queue nobody can open — which is exactly how `validated` and
    `promoted` stayed invisible. Pinned as an EQUALITY so neither side can drift alone."""
    from data_agent.learning.inbox.service import _LISTABLE_STATUSES

    assert set(server._INBOX_LIST_STATUSES) == set(_LISTABLE_STATUSES)


# --- revise (design §C): a THIRD response shape on this route ----------------


def test_the_revise_body_is_forwarded_verbatim(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """The reviewer's sentence is the whole input, and the BFF holds no schema for it — same
    posture as `complete`. What is pinned is that it survives the hop unreshaped, and that the
    server-held reviewer token rides with it so the browser never sees one."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setattr(server, "REVIEWER_TOKEN", "bff-held-secret")
    body = {"feedback": "register_type spans two catalog rules, so it cannot cite one"}

    resp = client.post("/api/inbox/candidate::abc::review-0/revise", json=body)

    assert resp.status_code == 200
    hop = fake_httpx[0]
    assert hop["method"] == "POST"
    assert hop["url"].endswith("/inbox/candidate%3A%3Aabc%3A%3Areview-0/revise")
    assert hop["json"] == body
    assert hop["headers"]["X-Reviewer-Token"] == "bff-held-secret"


def test_an_oversized_revise_body_is_413_before_any_hop(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """A free-text field is the easiest place to paste a novel into. The cap is the BFF's OWN
    resource and nobody downstream can give it back, which is why it is enforced before the
    hop rather than after it."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setattr(server, "INBOX_BODY_MAX_BYTES", 128)

    resp = client.post(
        "/api/inbox/candidate::abc::review-0/revise",
        json={"feedback": "x" * 5000},
    )

    assert resp.status_code == 413
    assert fake_httpx == []


def test_revise_answers_a_proposal_shape_not_an_action_result(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """⚠ THREE response shapes now ride this one route: the `{candidate_id, status, reason}`
    every adjudication verb answers, `promote`'s YAML emit, and this — a PROPOSAL, carrying no
    status at all because nothing was written. The BFF must keep passing bodies through
    without assuming any of them."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    proposal = {
        "entries": [{"locator": {"table": "t", "column": "c", "value": "v"}, "role": "inline"}],
        "replace": False,
        "rationale": "it guards the ratio's own denominator",
        "reason": "",
        "diff": [{"kind": "added", "locator": "t.c", "before": "", "after": "t.c = v → inline"}],
    }
    _FakeAsyncClient.payload = proposal

    resp = client.post("/api/inbox/candidate::abc::review-0/revise", json={"feedback": "x"})

    assert resp.status_code == 200
    # Byte-for-byte the upstream object: entries, flag, rationale and diff, unreshaped.
    assert resp.json() == proposal
    # ...and NO status, because nothing was written. A BFF that projected the action shape
    # onto this would invent one.
    assert "status" not in resp.json()


def test_the_revise_hop_is_constructed_with_the_long_read_timeout(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """⚠ ASSERTED THROUGH THE PROXY, not on the helper.

    The first version of this fix tested `_hop_timeout` in isolation, and it passed while the
    call site was wrong: the edit landed on the FIRST `timeout=10.0` in the file, which is the
    chat-history proxy, leaving the inbox proxy untouched AND `history()` referencing a
    `path` variable it does not have — a NameError on a route that had nothing to do with the
    change. A helper test cannot see any of that; this one can, because it reads what the
    proxy actually handed httpx.
    """
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    client.post("/api/inbox/candidate::abc::review-0/revise", json={"feedback": "f"})
    assert len(_FakeAsyncClient.timeouts) == 1
    timeout = _FakeAsyncClient.timeouts[0]
    assert timeout.read == server._INBOX_MODEL_HOP_TIMEOUT_SECONDS
    # CONNECT stays short: a service that is DOWN must still fail fast. A scalar timeout
    # would have set all four phases and traded the fast-failure case for the slow-success one.
    assert timeout.connect == server._INBOX_CONNECT_TIMEOUT_SECONDS


def test_a_non_model_hop_keeps_the_fast_crud_timeout(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    client.post("/api/inbox/candidate::abc::0/approve")
    assert _FakeAsyncClient.timeouts[0].read == server._INBOX_HOP_TIMEOUT_SECONDS


def test_the_chat_history_proxy_still_builds_its_own_client(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """The route the misplaced edit broke. It shares no state with the inbox proxy and must
    not have acquired a dependency on one."""
    import inspect

    source = inspect.getsource(server.history)
    assert "_hop_timeout" not in source, "history() has no `path`; this would NameError"


def test_the_model_backed_hop_outlasts_the_upstreams_own_deadline() -> None:
    """⚠ Found live: "Ask the assistant" rendered `502 Inbox service unreachable` while the
    upstream was still answering — it logged `POST /v1/responses 200` after the browser had
    given up.

    `revise` is the only route on this surface whose upstream calls a model. The reviser gives
    itself `learning_revise_timeout_seconds` (30s) and converts its OWN expiry into a 200
    carrying "the assistant timed out; try again" — a result a reviewer can act on. A BFF
    deadline shorter than that preempts it: the proposal is produced and discarded, and the
    error blames the wrong component.

    So the invariant is a RELATIONSHIP, not a number — the hop must outlast the upstream's own
    deadline with headroom, and every other action keeps the fast CRUD timeout because a quick
    failure is the right answer there.
    """
    from data_agent.learning.config import LearningSettings

    reviser_deadline = LearningSettings().learning_revise_timeout_seconds
    assert server._hop_timeout("/inbox/abc/revise").read > reviser_deadline, (
        "the BFF would abandon the hop while the reviser is still working"
    )
    for path in ("/inbox/abc/approve", "/inbox/abc/complete", "/inbox?status=in_review"):
        assert server._hop_timeout(path).read == server._INBOX_HOP_TIMEOUT_SECONDS, path


def test_every_model_backed_action_is_in_the_body_allowlist() -> None:
    """A model-backed action carries a prompt, so it needs a body. Pinned as a subset check so
    a second such route cannot be added to one list and forgotten in the other."""
    assert server._INBOX_MODEL_ACTIONS <= server._INBOX_BODY_ACTIONS
    assert server._INBOX_MODEL_ACTIONS <= server._INBOX_ACTIONS
    # The timeout set is a SUPERSET, and the gap is the point: it also covers model-backed
    # routes that are not per-candidate actions (`mint`), which must get the long read budget
    # WITHOUT being admitted to the action allowlist above.
    assert server._INBOX_MODEL_ACTIONS <= server._INBOX_MODEL_PATH_SEGMENTS


# --- the knowledge edit pair + the user-knowledge surface (knowledge-edit design) ------


def test_the_knowledge_edit_actions_are_reachable_and_carry_their_bodies(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """Both halves of the knowledge edit are per-candidate actions, so they ride the SAME
    allowlisted, token-attaching hop as every other verb — and both are meaningless without a
    body: `revise_knowledge` carries the reviewer's sentence, `apply_knowledge` carries the
    whole edited payload. A verb missing from `_INBOX_BODY_ACTIONS` would hop with `None` and
    the reviewer's work would never leave the browser."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setattr(server, "REVIEWER_TOKEN", "bff-held-secret")
    feedback = {"feedback": "state it for the class, not the person"}
    payload = {
        "payload": {
            "statement": "a transit allowance is paid monthly",
            "knowledge_type": "business_rule",
            "related_terms": ["transit allowance"],
            "structured": {"column": "payroll.payroll_fact.allowance"},
            "scope": "payroll",
        }
    }

    assert (
        client.post("/api/inbox/candidate::kn::0/revise_knowledge", json=feedback).status_code
        == 200
    )
    assert (
        client.post("/api/inbox/candidate::kn::0/apply_knowledge", json=payload).status_code
        == 200
    )

    assert [hop["url"].rsplit("/", 1)[-1] for hop in fake_httpx] == [
        "revise_knowledge",
        "apply_knowledge",
    ]
    assert fake_httpx[0]["json"] == feedback
    # VERBATIM: the five surfaces are the inbox service's contract (and the extractor's reader
    # behind it), and a second schema here would be a second vocabulary for the same mistake.
    assert fake_httpx[1]["json"] == payload
    for hop in fake_httpx:
        assert hop["headers"]["X-Reviewer-Token"] == "bff-held-secret"
        assert hop["url"].endswith(f"/inbox/candidate%3A%3Akn%3A%3A0/{hop['url'].rsplit('/', 1)[-1]}")


def test_the_knowledge_reviser_gets_the_model_read_budget(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """`revise_knowledge` calls a model upstream, exactly like `revise`, and the upstream
    converts its OWN expiry into a 200 the reviewer can act on. A BFF deadline shorter than
    that preempts it and blames the wrong component — the failure this budget was split for.
    The write half is a store operation and keeps the fast CRUD timeout."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    client.post("/api/inbox/candidate::kn::0/revise_knowledge", json={"feedback": "f"})
    client.post("/api/inbox/candidate::kn::0/apply_knowledge", json={"payload": {}})
    assert _FakeAsyncClient.timeouts[0].read == server._INBOX_MODEL_HOP_TIMEOUT_SECONDS
    assert _FakeAsyncClient.timeouts[1].read == server._INBOX_HOP_TIMEOUT_SECONDS
    # ...and the invariant the existing allowlist test enforces still holds with it in the set.
    assert "revise_knowledge" in server._INBOX_MODEL_ACTIONS
    assert server._INBOX_MODEL_ACTIONS <= server._INBOX_ACTIONS
    assert server._INBOX_MODEL_ACTIONS <= server._INBOX_BODY_ACTIONS
    assert server._INBOX_MODEL_ACTIONS <= server._INBOX_MODEL_PATH_SEGMENTS


def test_the_knowledge_actions_are_in_both_allowlists() -> None:
    """Pinned as a set membership rather than through a request, so removing either verb from
    one list and not the other fails HERE — where the two lists are visible together — instead
    of as a 404 on a card that renders fine."""
    for action in ("revise_knowledge", "apply_knowledge"):
        assert action in server._INBOX_ACTIONS, action
        assert action in server._INBOX_BODY_ACTIONS, action


def test_the_user_knowledge_list_requires_a_user_id(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """⚠ AN EMPTY user_id IS NOT "EVERY USER". The store's only read is per-user (design §D.1,
    the deliberate D17 exception), and this surface exists to show ONE named user's private
    facts to a reviewer. A request without one means nothing, so it is refused here and never
    becomes a hop the service has to interpret."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setattr(server, "REVIEWER_TOKEN", "bff-held-secret")

    assert client.get("/api/inbox/user_knowledge").status_code == 400
    assert client.get("/api/inbox/user_knowledge", params={"user_id": "   "}).status_code == 400
    assert fake_httpx == []

    resp = client.get("/api/inbox/user_knowledge", params={"user_id": "u-1042", "limit": 50})

    assert resp.status_code == 200
    hop = fake_httpx[0]
    assert hop["method"] == "GET"
    # Forwarded as an URLENCODED query param, so a user id carrying `&`, `=` or a space cannot
    # add structure to the upstream query string.
    assert hop["url"].endswith("/inbox/user_knowledge?user_id=u-1042&limit=50")
    assert hop["headers"]["X-Reviewer-Token"] == "bff-held-secret"


def test_a_user_id_with_reserved_characters_is_encoded_not_interpolated(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    client.get("/api/inbox/user_knowledge", params={"user_id": "a&limit=9999"})
    assert fake_httpx[0]["url"].endswith("/inbox/user_knowledge?user_id=a%26limit%3D9999")


def test_promote_reaches_the_user_knowledge_route_not_the_candidate_one(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """⚠ THE SHADOWING TRAP, named in design §D.4 and pinned here.

    `/api/inbox/user_knowledge/promote` ALSO reads as `candidate_id="user_knowledge",
    action="promote"`, and `promote` is in the action allowlist — so a catch-all declared first
    would answer this path, and the BFF would ask the inbox service to promote a candidate by
    that name. Both routes even produce the same upstream URL, which is exactly why the URL
    cannot be the assertion: what is checked is WHICH endpoint the router resolves."""
    from starlette.routing import Match

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/inbox/user_knowledge/promote",
        "path_params": {},
        "root_path": "",
        "headers": [],
    }
    matched = [
        route
        for route in server.app.router.routes
        if route.matches(scope)[0] == Match.FULL
    ]
    assert matched, "nothing matches the promote path at all"
    assert matched[0].endpoint.__name__ == "inbox_user_knowledge_promote", (
        "the candidate catch-all answers first — the new route must be DECLARED before it"
    )

    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setattr(server, "REVIEWER_TOKEN", "bff-held-secret")
    body = {"user_id": "u-1042", "record_id": "userknow::u-1042::c1"}

    resp = client.post("/api/inbox/user_knowledge/promote", json=body)

    assert resp.status_code == 200
    hop = fake_httpx[0]
    assert hop["method"] == "POST"
    assert hop["url"].endswith("/inbox/user_knowledge/promote")
    # ...and NOT the percent-encoded per-candidate spelling a catch-all would have produced for
    # a candidate whose id contained anything reserved.
    assert "%3A" not in hop["url"]
    assert hop["json"] == body
    assert hop["headers"]["X-Reviewer-Token"] == "bff-held-secret"


def test_an_oversized_user_promote_body_is_413_before_any_hop(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """The cap is the BFF's own resource and nobody downstream can give it back, so the new
    write surface cannot become the way around it."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setattr(server, "INBOX_BODY_MAX_BYTES", 128)

    resp = client.post(
        "/api/inbox/user_knowledge/promote",
        json={"user_id": "u-1042", "record_id": "x" * 5000},
    )

    assert resp.status_code == 413
    assert fake_httpx == []


def test_a_non_json_promote_body_is_400_and_never_proxied(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    resp = client.post(
        "/api/inbox/user_knowledge/promote",
        content=b"not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert fake_httpx == []


def test_the_user_knowledge_surface_is_gated_by_the_flag_like_every_other(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    """A dormant deployment must not grow a surface onto one user's private facts: the flag is
    checked BEFORE the user_id validation and before any body is read, so both routes answer
    404 rather than 400 — the surface does not exist, and it must not leak that it might."""
    monkeypatch.delenv("REVIEW_INBOX_ENABLED", raising=False)
    assert client.get("/api/inbox/user_knowledge", params={"user_id": "u-1042"}).status_code == 404
    assert client.get("/api/inbox/user_knowledge").status_code == 404
    assert (
        client.post(
            "/api/inbox/user_knowledge/promote", json={"user_id": "u", "record_id": "r"}
        ).status_code
        == 404
    )
    assert (
        client.post("/api/inbox/candidate::kn::0/revise_knowledge", json={"feedback": "f"}).status_code
        == 404
    )
    assert fake_httpx == []
