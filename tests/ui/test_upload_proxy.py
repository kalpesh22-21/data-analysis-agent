"""BFF `POST /api/upload{,/analyze}` raw-multipart proxy — the credential-inject
boundary (UI Slice 4 §4).

Mirrors `test_history_proxy.py`: the BFF proxies the browser's upload (a raw
`multipart/form-data` body + `session_id` query param) to a clickhouse-api scratch
route, looking the JWT up server-side and attaching `Authorization: Bearer <jwt>` +
`X-Session-Id` on the hop so the browser never holds the token (D82/D5). The key
Slice-4 design point: the BFF does NOT parse the multipart form — it forwards the
RAW body bytes verbatim with the browser's ORIGINAL `Content-Type` (boundary
preserved). The upstream status + JSON body propagate as-is (incl. a 413 and a
non-JSON body -> `{"detail": ...}`). No clickhouse-api / ClickHouse is started —
`httpx.AsyncClient` is faked so no socket opens; the point is the BFF plumbing.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import ui.server as server
from fastapi.testclient import TestClient

_SESSION_ID = "s" + "0" * 32
_JWT = "bff-held-jwt"
# A raw multipart body the browser sent — the BFF must forward it byte-for-byte
# without parsing it, and preserve the boundary via the Content-Type header.
_BOUNDARY = "----WebKitFormBoundaryABC123"
_CONTENT_TYPE = f"multipart/form-data; boundary={_BOUNDARY}"
_RAW_BODY = (
    f"--{_BOUNDARY}\r\n"
    'Content-Disposition: form-data; name="file"; filename="people.csv"\r\n'
    "Content-Type: text/csv\r\n\r\n"
    "emp_id,dept\r\n1001,Engineering\r\n"
    f"\r\n--{_BOUNDARY}--\r\n"
).encode()


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None, *, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeAsyncClient:
    """Drop-in for `httpx.AsyncClient` recording the outbound POST (url, headers,
    raw content) instead of opening a socket. `response` is the canned reply."""

    captured: list[dict] = []
    response = _FakeResponse(200, {"columns": [], "row_count": 0, "sample_rows": []})

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def post(self, url: str, *, headers: dict, content: bytes) -> _FakeResponse:
        type(self).captured.append({"url": url, "headers": headers, "content": content})
        return type(self).response


@pytest.fixture
def fake_httpx(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict]]:
    _FakeAsyncClient.captured = []
    _FakeAsyncClient.response = _FakeResponse(200, {"columns": [], "row_count": 0, "sample_rows": []})
    monkeypatch.setattr(server.httpx, "AsyncClient", _FakeAsyncClient)
    # Seed the server-side session->JWT map (the browser never sends the JWT).
    server._SESSIONS[_SESSION_ID] = _JWT
    yield _FakeAsyncClient.captured
    server._SESSIONS.pop(_SESSION_ID, None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(server.app)


def _post_upload(client: TestClient, path: str) -> object:
    return client.post(
        f"{path}?session_id={_SESSION_ID}",
        content=_RAW_BODY,
        headers={"content-type": _CONTENT_TYPE},
    )


def test_scratch_base_swaps_mcp_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # `_scratch_base()` keeps scheme+netloc, swaps the path for `/scratch/v1` — the
    # same derivation the runtime does; the scratch routes live on the MCP host.
    monkeypatch.setattr(server, "MCP_URL", "http://mcp-host:18090/mcp")
    assert server._scratch_base() == "http://mcp-host:18090/scratch/v1"


@pytest.mark.parametrize(
    ("api_path", "upstream_path"),
    [("/api/upload/analyze", "/analyze"), ("/api/upload", "/upload")],
)
def test_upload_proxy_attaches_creds_and_forwards_raw_body(
    fake_httpx: list[dict], client: TestClient, api_path: str, upstream_path: str
) -> None:
    resp = _post_upload(client, api_path)
    assert resp.status_code == 200
    assert resp.json() == {"columns": [], "row_count": 0, "sample_rows": []}
    assert len(fake_httpx) == 1
    hop = fake_httpx[0]
    # Hops to the scratch base on the MCP host, at the right sub-path.
    assert hop["url"] == f"{server._scratch_base()}{upstream_path}"
    # JWT + session-id are attached server-side — the browser sent only the
    # session_id query param, never the JWT.
    assert hop["headers"]["Authorization"] == f"Bearer {_JWT}"
    assert hop["headers"]["X-Session-Id"] == _SESSION_ID
    # The browser's ORIGINAL Content-Type (with boundary) is forwarded verbatim...
    assert hop["headers"]["Content-Type"] == _CONTENT_TYPE
    # ...and the raw multipart body is passed through byte-for-byte, unparsed.
    assert hop["content"] == _RAW_BODY


def test_upload_proxy_propagates_413_verbatim(fake_httpx: list[dict], client: TestClient) -> None:
    _FakeAsyncClient.response = _FakeResponse(
        413, {"error": "parsed rows exceed the cap", "code": "SCRATCH_TOO_LARGE"}
    )
    resp = _post_upload(client, "/api/upload")
    assert resp.status_code == 413
    assert resp.json() == {"error": "parsed rows exceed the cap", "code": "SCRATCH_TOO_LARGE"}


def test_upload_proxy_propagates_400_verbatim(fake_httpx: list[dict], client: TestClient) -> None:
    _FakeAsyncClient.response = _FakeResponse(
        400, {"error": "duplicate role", "code": "UPLOAD_MAPPING_INVALID"}
    )
    resp = _post_upload(client, "/api/upload")
    assert resp.status_code == 400
    assert resp.json() == {"error": "duplicate role", "code": "UPLOAD_MAPPING_INVALID"}


def test_upload_proxy_non_json_body_wrapped_as_detail(
    fake_httpx: list[dict], client: TestClient
) -> None:
    _FakeAsyncClient.response = _FakeResponse(502, None, text="upstream boom")
    resp = _post_upload(client, "/api/upload/analyze")
    assert resp.status_code == 502
    assert resp.json() == {"detail": "upstream boom"}


def test_upload_proxy_unknown_session_404s_before_hop(
    fake_httpx: list[dict], client: TestClient
) -> None:
    resp = client.post(
        "/api/upload?session_id=sunknownsession",
        content=_RAW_BODY,
        headers={"content-type": _CONTENT_TYPE},
    )
    assert resp.status_code == 404
    assert fake_httpx == []  # no network hop for an unminted session


def test_upload_proxy_oversized_content_length_short_circuits(
    fake_httpx: list[dict], client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A declared Content-Length over the front-door cap (+ envelope slack) is
    # rejected before any hop. With UPLOAD_MAX_BYTES=0 the body_cap is the 64 KiB
    # slack, so a >64 KiB body trips the header short-circuit.
    monkeypatch.setattr(server, "UPLOAD_MAX_BYTES", 0)
    big = b"x" * (65536 + 1024)
    resp = client.post(
        f"/api/upload?session_id={_SESSION_ID}",
        content=big,
        headers={"content-type": _CONTENT_TYPE},
    )
    assert resp.status_code == 413
    assert resp.json()["code"] == "UPLOAD_TOO_LARGE"
    assert fake_httpx == []  # no network hop for an over-cap upload


def test_upload_proxy_respects_envelope_slack(
    fake_httpx: list[dict], client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The BFF caps the WHOLE body but the downstream cap is the file part only, so a
    # body that is <= UPLOAD_MAX_BYTES + 64 KiB must pass through (not be 413'd for
    # its multipart envelope). With cap = len(body), the +64 KiB slack admits it.
    monkeypatch.setattr(server, "UPLOAD_MAX_BYTES", len(_RAW_BODY))
    resp = _post_upload(client, "/api/upload")
    assert resp.status_code == 200
    assert len(fake_httpx) == 1
    assert fake_httpx[0]["content"] == _RAW_BODY


def test_upload_proxy_chunked_over_cap_bailed_from_stream(
    fake_httpx: list[dict], client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A chunked body (no Content-Length — the header short-circuit can't help) that
    # exceeds the cap must be rejected from the bounded STREAM read, not buffered
    # whole. httpx sends an iterator body with Transfer-Encoding: chunked (no
    # Content-Length), so the 413 here can only come from the stream-loop guard.
    monkeypatch.setattr(server, "UPLOAD_MAX_BYTES", 0)  # body_cap == 64 KiB

    def gen():
        # 3 * 32 KiB = 96 KiB > 64 KiB cap; the guard should fire mid-stream.
        for _ in range(3):
            yield b"y" * (32 * 1024)

    resp = client.post(
        f"/api/upload?session_id={_SESSION_ID}",
        content=gen(),
        headers={"content-type": _CONTENT_TYPE},
    )
    assert resp.status_code == 413
    assert resp.json()["code"] == "UPLOAD_TOO_LARGE"
    assert fake_httpx == []  # no network hop for an over-cap upload
