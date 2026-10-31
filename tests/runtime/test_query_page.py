"""The public page endpoint now proxies saved-result creation/paging."""
import httpx

from tests.runtime.test_export_proxy import AUTH, SID, client


def test_query_page_requires_snapshot_service_not_mcp(monkeypatch):
    api, _ = client(monkeypatch, lambda request: httpx.Response(200), base="")
    response = api.post("/query/page", headers=AUTH, json={"sql": "SELECT 1"})
    assert response.status_code == 501
    assert response.json()["error_code"] == "EXPORT_NOT_CONFIGURED"


def test_page_does_not_wrap_or_limit_sql_before_snapshot_creation(monkeypatch):
    import json
    sql = "SELECT x FROM t ORDER BY x"
    def upstream(request):
        if request.method == "POST":
            assert json.loads(request.content) == {"sql": sql}
            return httpx.Response(201, json={"snapshot_id": SID})
        assert request.url.path == f"/query/snapshots/{SID}/page"
        return httpx.Response(200, json={"snapshot_id": SID, "rows": [[1]], "has_more": True})
    api, _ = client(monkeypatch, upstream)
    assert api.post("/query/page", headers=AUTH, json={"sql": sql, "limit": 1}).status_code == 200
