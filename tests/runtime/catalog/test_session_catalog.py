"""Session catalog persistence, ownership, concurrent initialization and refresh."""
import asyncio
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.catalog.session_catalog import SessionCatalog, identity_hash
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import SessionDoc
from data_agent.runtime.mcp.real_client import RealMCPClient
from tests._catalog_fixture import load_catalog_export


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr('data_agent.runtime.catalog.session_catalog.verify_jwt_claims',
                        lambda token, **_: {'sub': token, 'clientcode': token})
    store = InMemorySessionStore()
    settings = RuntimeSettings(_env_file=None, mcp_url='http://mcp.test/mcp')
    calls = []
    async def serve(request):
        calls.append(request)
        await asyncio.sleep(.01)
        user = request.headers['Authorization'].split()[1]
        snapshot = load_catalog_export()
        snapshot.update(scratch_database='tenant_uploads', owner=identity_hash({'sub':user,'clientcode':user}),
                        session_id=request.headers['X-Session-Id'], scope_token=f'signed-{len(calls)}',
                        expires_at=int(time.time())+3600, columns=['dbpcm_warehouse.employee.employee_code'])
        return httpx.Response(200, json=snapshot)
    transport = httpx.MockTransport(serve)
    return store, settings, calls, transport


async def test_snapshot_survives_process_restart_without_refetch(rig):
    store, settings, calls, transport = rig
    first = SessionCatalog(settings, store, transport=transport)
    snapshot = await first.ensure(jwt='alice', session_id='s1')
    assert len(calls) == 1
    # Round-trip exactly the Couchbase document shape into a fresh worker/store.
    restored = SessionDoc.from_doc((await store.get_or_create_session('s1')).to_doc())
    restarted_store = InMemorySessionStore()
    restarted_store._docs['s1'] = restored
    restarted_store._versions['s1'] = 0
    restarted = SessionCatalog(settings, restarted_store, transport=transport)
    assert await restarted.ensure(jwt='alice', session_id='s1') == snapshot
    assert len(calls) == 1
    assert 'signed-' not in repr(restored)


async def test_two_workers_cold_start_calculate_once(rig):
    store, settings, calls, transport = rig
    workers = [SessionCatalog(settings, store, transport=transport) for _ in range(8)]
    snapshots = await asyncio.gather(*(w.ensure(jwt='alice', session_id='s1') for w in workers))
    assert len(calls) == 1
    assert all(s == snapshots[0] for s in snapshots)


async def test_sessions_do_not_share_catalog_and_owner_cannot_change(rig):
    store, settings, calls, transport = rig
    manager = SessionCatalog(settings, store, transport=transport)
    await manager.ensure(jwt='alice', session_id='s1')
    await manager.ensure(jwt='bob', session_id='s2')
    assert len(calls) == 2
    with pytest.raises(HTTPException) as error:
        await manager.ensure(jwt='bob', session_id='s1')
    assert error.value.status_code == 403
    assert len(calls) == 2


async def test_expiry_and_explicit_refresh_replace_pair(rig):
    store, settings, calls, transport = rig
    manager = SessionCatalog(settings, store, transport=transport)
    a = await manager.ensure(jwt='alice', session_id='s')
    b = await manager.ensure(jwt='alice', session_id='s', force=True)
    assert a['scope_token'] != b['scope_token']
    store._docs['s'].catalog_session['expires_at'] = 0
    c = await manager.ensure(jwt='alice', session_id='s')
    assert c['scope_token'] != b['scope_token']
    assert len(calls) == 3


async def test_failed_refresh_preserves_previous_pair_but_fails_request(rig):
    store, settings, calls, transport = rig
    manager = SessionCatalog(settings, store, transport=transport)
    original = await manager.ensure(jwt='alice', session_id='s')
    manager.transport = httpx.MockTransport(lambda _: httpx.Response(503))
    with pytest.raises(HTTPException):
        await manager.ensure(jwt='alice', session_id='s', force=True)
    assert store._docs['s'].catalog_session == original


async def test_failure_has_no_default_catalog_fallback(rig):
    store, settings, calls, _ = rig
    manager = SessionCatalog(settings, store, transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    with pytest.raises(HTTPException):
        await manager.ensure(jwt='alice', session_id='s')
    assert 'catalog' not in store._docs['s'].catalog_session


async def test_expired_lease_can_be_recovered(rig):
    store, settings, calls, transport = rig
    owner = identity_hash({'sub':'alice','clientcode':'alice'})
    await store.mutate_catalog_session('s', lambda _: {'owner':owner,'lease':'dead','lease_until':0})
    manager = SessionCatalog(settings, store, transport=transport)
    await manager.ensure(jwt='alice', session_id='s')
    assert len(calls) == 1
    assert 'lease' not in store._docs['s'].catalog_session


async def test_scope_token_attached_only_at_transport_boundary():
    client = RealMCPClient('http://mcp.test/mcp')
    client.scope_token_provider = AsyncMock(return_value='signed-scope')
    headers = await client._scoped_headers('user-jwt','s')
    assert headers == {'Authorization':'Bearer user-jwt','X-Session-Id':'s','X-Catalog-Scope':'signed-scope'}
    client.scope_token_provider.assert_awaited_once_with(jwt='user-jwt',session_id='s')


async def test_empty_session_catalog_is_deny_all(rig):
    store, settings, calls, transport = rig
    manager = SessionCatalog(settings, store, transport=transport)
    snapshot = await manager.ensure(jwt='alice', session_id='s')
    await store.mutate_catalog_session('s', lambda _: {**snapshot, 'columns':[], 'catalog':{}})
    credentials = await manager.credentials(RuntimeCredentials('s','alice',frozenset()))
    assert credentials.column_scope == frozenset({'__no_catalog_access__'})
    assert (await manager.catalog_handle(credentials)).schema == {}


async def test_credentials_restore_api_namespace_from_cached_snapshot(rig):
    from data_agent.sqlparse.scratch_namespace import bind_scratch_database, reset_scratch_database, scratch_databases
    store, settings, calls, transport = rig
    manager = SessionCatalog(settings, store, transport=transport)
    await manager.ensure(jwt='alice', session_id='s')
    token = bind_scratch_database(None)
    try:
        credentials = RuntimeCredentials(session_id='s', jwt='alice', column_scope=frozenset())
        await manager.credentials(credentials)
        assert scratch_databases() == frozenset({'scratch', 'tenant_uploads'})
        assert len(calls) == 1
    finally:
        reset_scratch_database(token)


async def test_old_cached_snapshot_is_refreshed_to_discover_namespace(rig):
    store, settings, calls, transport = rig
    manager = SessionCatalog(settings, store, transport=transport)
    await manager.ensure(jwt='alice', session_id='s')
    del store._docs['s'].catalog_session['scratch_database']
    snapshot = await manager.ensure(jwt='alice', session_id='s')
    assert snapshot['scratch_database'] == 'tenant_uploads'
    assert len(calls) == 2
