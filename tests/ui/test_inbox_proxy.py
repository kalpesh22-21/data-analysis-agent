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

    async def request(self, method: str, url: str, *, headers: dict) -> _FakeResponse:
        type(self).captured.append({"method": method, "url": url, "headers": headers})
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


def test_unknown_action_verb_404s_at_bff(
    monkeypatch: pytest.MonkeyPatch, fake_httpx: list[dict], client: TestClient
) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    # A verb outside the {approve,reject,retract} allowlist never reaches the service.
    assert client.post("/api/inbox/candidate::x::0/delete").status_code == 404
    assert fake_httpx == []
