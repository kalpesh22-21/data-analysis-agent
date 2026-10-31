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


def client(monkeypatch, handler, *, base="http://snapshot.test", catalog=True):
    monkeypatch.setattr(app_module, "verify_jwt", lambda *a, **kw: frozenset())
    settings = RuntimeSettings(clickhouse_api_rest_base_url=base)
    scope = Catalog() if catalog else None
    app = FastAPI()
    app.include_router(
        export_router(
            settings, app_module._extract_credentials, scope, transport=httpx.MockTransport(handler)
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


@pytest.mark.parametrize('body', [
    {'sql':'SELECT 1','limit':50},
    {'snapshot_id':SID,'offset':50,'limit':50},
    {'sql':'SELECT 1','refresh_of':SID},
])
def test_unified_page_uses_existing_snapshot_routes_and_verified_scope(monkeypatch, body):
    import json
    seen=[]
    def upstream(request):
        seen.append(request)
        if request.method == 'POST':
            return httpx.Response(201,json={'snapshot_id':SID,'created_at':'2026-10-30T00:00:00Z'})
        return httpx.Response(200,json={'snapshot_id':SID,'rows':[[1]],'column_formats':{'n':{'kind':'integer'}}})
    api,_=client(monkeypatch,upstream)
    response=api.post('/query/page',headers=AUTH,json=body)
    assert response.status_code==200 and response.json()['rows']==[[1]]
    assert len(seen) == (2 if 'sql' in body else 1)
    assert seen[-1].method == 'GET'
    assert seen[-1].url.path == f'/query/snapshots/{SID}/page'
    assert seen[-1].url.params['offset'] == str(body.get('offset',0))
    assert seen[-1].url.params['limit'] == str(body.get('limit',100))
    if 'sql' in body:
        assert seen[0].url.path == '/query/snapshots'
        assert json.loads(seen[0].content) == {k:v for k,v in body.items() if k in {'sql','refresh_of'}}
    assert all(r.headers['X-Catalog-Scope']=='server-scope' for r in seen)
    assert response.json()['reset_table'] == bool(body.get('refresh_of'))


def test_removed_table_handle_rejected_before_upstream(monkeypatch):
    seen=[]
    api,_=client(monkeypatch,lambda request:seen.append(request))
    assert api.post('/query/page',headers=AUTH,json={'table_ref':'old'}).status_code==400
    assert not seen


def test_unified_saved_page_recovers_scope_once_and_resets_offset(monkeypatch):
    calls=[]
    async def ensure(self, **kwargs):
        return {'scope_token':'new-scope'}
    monkeypatch.setattr(Catalog,'ensure',ensure,raising=False)
    def upstream(request):
        calls.append(request)
        if request.url.path==f'/query/snapshots/{SID}/page':
            return httpx.Response(409,json={'detail':{'code':'SNAPSHOT_SCOPE_CHANGED'}})
        assert request.url.path==f'/query/snapshots/{SID}/rebuild'
        assert request.headers['X-Catalog-Scope']=='new-scope'
        return httpx.Response(201,json={'snapshot_id':'22222222-2222-4222-8222-222222222222'})
    api,_=client(monkeypatch,upstream)
    response=api.post('/query/page',headers=AUTH,json={'snapshot_id':SID,'offset':500})
    assert response.status_code==409
    assert response.json()['detail']['code']=='SNAPSHOT_REPLACED'
    assert response.json()['reset_table'] is True
    assert len(calls)==2


@pytest.mark.parametrize('body', [{}, {'sql':' '}, {'sql':'SELECT 1','snapshot_id':SID}, {'sql':'SELECT 1','column_formats':{}}, {'snapshot_id':'bad'}, {'snapshot_id':SID,'limit':1001}, {'sql':'SELECT 1','refresh_of':SID,'offset':1}])
def test_page_rejects_invalid_request_before_upstream(monkeypatch, body):
    seen=[]
    api,_=client(monkeypatch,lambda r:seen.append(r))
    assert api.post('/query/page',headers=AUTH,json=body).status_code==422
    assert not seen


def test_page_creation_error_does_not_fetch_rows(monkeypatch):
    seen=[]
    def upstream(request):
        seen.append(request)
        return httpx.Response(409,json={'detail':{'code':'SNAPSHOT_IN_PROGRESS'}},headers={'Retry-After':'2'})
    api,_=client(monkeypatch,upstream)
    response=api.post('/query/page',headers=AUTH,json={'sql':'SELECT 1'})
    assert response.status_code==409 and response.headers['Retry-After']=='2'
    assert len(seen)==1
