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

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

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
        return _FakeResponse(200, {"items": [], "count": 0})


@pytest.fixture
def fake_httpx(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict]]:
    _FakeAsyncClient.captured = []
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
    # A status outside {in_review, rejected} is a 400 at the BFF — never proxied.
    assert client.get("/api/inbox", params={"status": "validated"}).status_code == 400
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
    # A verb outside the {approve,reject,retract,complete} allowlist never reaches the
    # service.
    assert client.post("/api/inbox/candidate::x::0/delete").status_code == 404
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
    """`complete` is the ONLY action with a body. The others must keep sending none —
    a `null` body on an upstream route that takes no body is the kind of thing that
    works until a stricter server rejects it."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    for action in ("approve", "reject", "retract"):
        assert client.post(f"/api/inbox/candidate::abc::0/{action}").status_code == 200
    assert [hop["json"] for hop in fake_httpx] == [None, None, None]


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
