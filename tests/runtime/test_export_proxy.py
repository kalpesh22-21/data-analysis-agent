"""Snapshot proxy auth, scope propagation, response contract and stream cleanup."""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from data_agent.runtime import app as app_module
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.export_proxy import export_router

SID = "11111111-1111-4111-8111-111111111111"
AUTH = {"Authorization": "Bearer test-token", "X-Session-Id": "session-1"}


class Catalog:
    def __init__(self):
        self.calls = []

    async def scope_token(self, **kwargs):
        self.calls.append(kwargs)
        return "server-scope"


def client(monkeypatch, handler, *, base="http://snapshot.test", catalog=True, resolver=None):
    monkeypatch.setattr(app_module, "verify_jwt", lambda *a, **kw: frozenset())
    settings = RuntimeSettings(clickhouse_api_rest_base_url=base)
    scope = Catalog() if catalog else None
    app = FastAPI()
    app.include_router(
        export_router(
            settings, app_module._extract_credentials, scope, transport=httpx.MockTransport(handler),
            resolve_table_sql=resolver
        )
    )
    return TestClient(app), scope


def test_create_and_refresh_preserve_body_and_add_verified_scope(monkeypatch):
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(
            201,
            content=b'{ "snapshot_id": "replacement" }',
            headers={"Content-Type": "application/json"},
        )

    api, catalog = client(monkeypatch, upstream)
    body = ('{ "sql": "SELECT 1", "refresh_of": "' + SID + '" }').encode()
    response = api.post(
        "/query/snapshots",
        content=body,
        headers={**AUTH, "X-Catalog-Scope": "forged", "Content-Type": "application/json"},
    )
    assert response.status_code == 201
    assert response.content == b'{ "snapshot_id": "replacement" }'
    assert seen[0].content == body
    assert seen[0].headers["Authorization"] == AUTH["Authorization"]
    assert seen[0].headers["X-Session-Id"] == "session-1"
    assert seen[0].headers["X-Catalog-Scope"] == "server-scope"
    assert catalog.calls == [{"jwt": "test-token", "session_id": "session-1"}]
    assert seen[0].extensions["timeout"]["read"] == 150.0


@pytest.mark.parametrize("code", [400, 403, 404, 409, 410, 429, 503])
def test_errors_status_and_retry_after_are_preserved(monkeypatch, code):
    body = b'{"detail":{"error":"unavailable","code":"TEST_CODE"}}'
    api, _ = client(
        monkeypatch,
        lambda request: httpx.Response(
            code, content=body, headers={"Retry-After": "2", "Content-Type": "application/json"}
        ),
    )
    response = api.get(f"/query/snapshots/{SID}/page?offset=0&limit=100", headers=AUTH)
    assert response.status_code == code
    assert response.content == body
    assert response.headers["Retry-After"] == "2"
    assert response.headers["Cache-Control"] == "no-store"


def test_query_string_and_presigned_url_remain_unchanged(monkeypatch):
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"files": [{"url": "https://s3.test/file?sig=a%2Fb"}]})

    api, _ = client(monkeypatch, upstream)
    result = api.get(f"/query/snapshots/{SID}/download-urls?x=a%2Fb&x=2", headers=AUTH)
    assert seen[0].url.query == b"x=a%2Fb&x=2"
    assert result.json()["files"][0]["url"] == "https://s3.test/file?sig=a%2Fb"


class CsvStream(httpx.AsyncByteStream):
    closed = False

    async def __aiter__(self):
        yield b"id,name\n"
        yield b"1,Alice\n"

    async def aclose(self):
        self.closed = True


def test_download_stream_and_cleanup(monkeypatch):
    stream = CsvStream()
    api, _ = client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            stream=stream,
            headers={
                "Content-Type": "text/csv",
                "Content-Disposition": 'attachment; filename="part.csv"',
            },
        ),
    )
    response = api.get(f"/query/snapshots/{SID}/download?part=1", headers=AUTH)
    assert response.content == b"id,name\n1,Alice\n"
    assert response.headers["Content-Disposition"] == 'attachment; filename="part.csv"'
    assert stream.closed


def test_download_errors_still_return_json(monkeypatch):
    api, _ = client(
        monkeypatch,
        lambda request: httpx.Response(404, json={"detail": {"code": "SNAPSHOT_NOT_FOUND"}}),
    )
    response = api.get(f"/query/snapshots/{SID}/download?part=1", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "SNAPSHOT_NOT_FOUND"


def test_network_errors_are_canned(monkeypatch):
    def fail(request):
        raise httpx.ConnectError("SECRET backend detail")

    api, _ = client(monkeypatch, fail)
    response = api.post("/query/snapshots", json={"sql": "SELECT 1"}, headers=AUTH)
    assert response.status_code == 502
    assert response.json()["error_code"] == "EXPORT_UNAVAILABLE"
    assert "SECRET" not in response.text


def test_auth_and_configuration_fail_without_upstream_call(monkeypatch):
    api, _ = client(monkeypatch, lambda r: pytest.fail("must not dispatch"), base="")
    assert api.post("/query/snapshots", json={"sql": "SELECT 1"}).status_code == 401
    assert (
        api.post(
            "/query/snapshots", json={}, headers={"Authorization": "Bearer test-token"}
        ).status_code
        == 400
    )
    response = api.post("/query/snapshots", json={}, headers=AUTH)
    assert response.status_code == 501
    assert response.json()["error_code"] == "EXPORT_NOT_CONFIGURED"
    assert RuntimeSettings(mcp_url="http://elsewhere/mcp").clickhouse_api_rest_base() == ""


def test_no_unscoped_fallback(monkeypatch):
    api, _ = client(monkeypatch, lambda r: pytest.fail("must not dispatch"), catalog=False)
    assert api.post("/query/snapshots", json={}, headers=AUTH).status_code == 503


def test_invalid_json_is_not_exposed(monkeypatch):
    api, _ = client(monkeypatch, lambda request: httpx.Response(500, text="PRIVATE DEBUG HTML"))
    response = api.post("/query/snapshots", json={}, headers=AUTH)
    assert response.status_code == 502
    assert "PRIVATE" not in response.text


def test_broken_stream_is_closed_and_not_reported_as_success(monkeypatch):
    class BrokenStream(CsvStream):
        async def __aiter__(self):
            yield b"id\n"
            raise httpx.ReadError("connection lost")

    stream = BrokenStream()
    api, _ = client(monkeypatch, lambda request: httpx.Response(200, stream=stream))
    with pytest.raises(httpx.ReadError):
        api.get(f"/query/snapshots/{SID}/download?part=1", headers=AUTH)
    assert stream.closed


@pytest.mark.parametrize("suffix", ["page?offset=100", "download-urls", "download?part=1"])
def test_scope_change_refreshes_catalog_and_returns_replacement(monkeypatch, suffix):
    calls = []

    async def ensure(self, **kwargs):
        self.calls.append(kwargs)
        return {"scope_token": "new-server-scope"}

    monkeypatch.setattr(Catalog, "ensure", ensure, raising=False)
    replacement = {
        "snapshot_id": "22222222-2222-4222-8222-222222222222",
        "columns": ["id"],
        "row_count": 2,
        "created_at": "now",
        "expires_at": "later",
    }

    def upstream(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(409, json={"detail": {"code": "SNAPSHOT_SCOPE_CHANGED"}})
        assert request.url.path == f"/query/snapshots/{SID}/rebuild"
        assert request.headers["X-Catalog-Scope"] == "new-server-scope"
        assert request.url.query == b""
        return httpx.Response(201, json=replacement)

    api, catalog = client(monkeypatch, upstream)
    response = api.get(f"/query/snapshots/{SID}/{suffix}", headers=AUTH)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SNAPSHOT_REPLACED"
    assert response.json()["snapshot"] == replacement
    assert response.json()["reset_table"] is True
    assert len(calls) == 2
    assert catalog.calls[-1]["force"] is True


@pytest.mark.parametrize(
    "status,code",
    [(403, "COLUMN_NOT_ALLOWED"), (409, "SNAPSHOT_IN_PROGRESS"), (409, "SNAPSHOT_SCOPE_CHANGED")],
)
def test_recovery_denial_is_returned_without_retry_loop(monkeypatch, status, code):
    async def ensure(self, **kwargs):
        return {"scope_token": "new-scope"}

    monkeypatch.setattr(Catalog, "ensure", ensure, raising=False)
    calls = []

    def upstream(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(409, json={"detail": {"code": "SNAPSHOT_SCOPE_CHANGED"}})
        return httpx.Response(status, json={"detail": {"code": code}}, headers={"Retry-After": "2"})

    api, _ = client(monkeypatch, upstream)
    response = api.get(f"/query/snapshots/{SID}/page", headers=AUTH)
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
    assert response.headers["Retry-After"] == "2"
    assert len(calls) == 2


async def _selected_table(credentials, ref):
    assert credentials.session_id == "session-1"
    if ref != "0:selected":
        raise PermissionError
    return {"sql": "SELECT 1", "column_formats": {}}


def test_snapshot_creation_resolves_approved_table_reference(monkeypatch):
    import json
    seen = []

    def upstream(request):
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"snapshot_id": SID})

    api, _ = client(monkeypatch, upstream, resolver=_selected_table)
    response = api.post("/query/snapshots", json={"table_ref": "0:selected"}, headers=AUTH)
    assert response.status_code == 201
    assert seen == [{"sql": "SELECT 1", "column_formats": {}}]
    response = api.post("/query/snapshots", json={"table_ref": "0:unselected"}, headers=AUTH)
    assert response.status_code == 404
    assert len(seen) == 1
    response = api.post("/query/snapshots", json={"table_ref": "0:selected", "sql": "SELECT 2"}, headers=AUTH)
    assert response.status_code == 400
    assert len(seen) == 1


def test_snapshot_uses_approved_formats_instead_of_caller_overrides(monkeypatch):
    import json
    trusted = {"ZIP": {"kind": "identifier"}}

    async def resolve(credentials, ref):
        assert ref == "approved"
        return {"sql": "SELECT zip_code AS ZIP FROM employee", "column_formats": trusted}

    def handler(request):
        body = json.loads(request.content)
        assert body["column_formats"] == trusted
        assert "table_ref" not in body
        return httpx.Response(201, json={"column_formats": trusted})

    api, _ = client(monkeypatch, handler, resolver=resolve)
    response = api.post('/query/snapshots', headers=AUTH,
                        json={"table_ref":"approved", "column_formats":{"ZIP":{"kind":"integer"}}})
    assert response.status_code == 201
    assert response.json()["column_formats"] == trusted
