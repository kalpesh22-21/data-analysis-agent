"""Uploads reuse the runtime's persisted scope token without exposing it to the UI."""
import httpx
import pytest
from fastapi.testclient import TestClient

from data_agent.runtime import app as app_module
from data_agent.runtime.catalog.session_catalog import SessionCatalog
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.session.memory_store import InMemorySessionStore


@pytest.mark.parametrize('path', ['/upload', '/upload/analyze'])
def test_upload_attaches_scope_and_preserves_multipart(monkeypatch, path):
    monkeypatch.setattr(app_module, 'verify_jwt', lambda *a, **kw: frozenset())
    calls = []
    async def scope(self, *, jwt, session_id):
        assert (jwt, session_id) == ('identity-token', 'session')
        return 'server-scope-token'
    monkeypatch.setattr(SessionCatalog, 'scope_token', scope)
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return httpx.Response(200, json={'row_count': 3})
    monkeypatch.setattr(app_module.httpx, 'AsyncClient', Client)
    app = app_module.create_app(
        settings=RuntimeSettings(_env_file=None, catalog_source='mcp', retrieval_enabled=False,
                                 mcp_url='http://mcp.test/mcp', scratch_enabled=True),
        session_store=InMemorySessionStore(), mcp_client=FakeMCPClient(tools=[], scripted={}),
        model_client=ScriptedModelClient([]),
    )
    response = TestClient(app).post(path, content=b'original multipart bytes', headers={
        'Authorization':'Bearer identity-token', 'X-Session-Id':'session',
        'Content-Type':'multipart/form-data; boundary=original',
    })
    assert response.status_code == 200
    assert response.json() == {'row_count':3}
    url, kwargs = calls[0]
    assert url == 'http://mcp.test/scratch/v1/' + ('analyze' if path.endswith('analyze') else 'upload')
    assert kwargs['headers']['X-Catalog-Scope'] == 'server-scope-token'
    assert kwargs['headers']['X-Session-Id'] == 'session'
    assert kwargs['content'] == b'original multipart bytes'
    assert 'server-scope-token' not in response.text
