"""Session-owned catalog snapshots persisted with the scope token in SessionStore.

No default catalog fallback. A CAS lease coordinates cold initialization across
workers; refresh commits the catalog and token in one document replacement.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import replace

import httpx
from fastapi import HTTPException

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.auth.jwt_verify import verify_jwt_claims
from data_agent.runtime.catalog.export_client import load_catalog_handles_from_export
from data_agent.runtime.mcp._transport import side_channel_headers


def identity_hash(claims: dict) -> str:
    stable = {k: v for k, v in claims.items() if k not in {"exp", "iat", "nbf", "sid_hash", "session_id", "column_scope"}}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class SessionCatalog:
    def __init__(self, settings, store, *, transport=None):
        self.settings, self.store, self.transport = settings, store, transport
        self.url = settings.mcp_url.rsplit('/', 1)[0] + '/session/catalog'

    def owner(self, token):
        claims = verify_jwt_claims(token, jwks_url=self.settings.jwks_url,
                                   issuer=self.settings.jwt_issuer, audience=self.settings.jwt_audience)
        return identity_hash(claims)

    async def ensure(self, *, jwt: str, session_id: str, force=False):
        owner = self.owner(jwt)
        lease = uuid.uuid4().hex
        deadline = time.monotonic() + 65
        baseline = (await self.store.get_or_create_session(session_id)).catalog_session
        baseline_token = baseline.get('scope_token') if baseline else None
        if baseline and baseline.get('owner') != owner:
            raise HTTPException(403, detail='Session belongs to a different identity.')
        if baseline and not force and baseline.get('scope_token') and baseline.get('expires_at', 0) > time.time() + 30:
            return baseline
        while time.monotonic() < deadline:
            now = time.time()
            def claim(old):
                old = old or {}
                if old.get('owner') not in (None, owner):
                    raise HTTPException(403, detail='Session belongs to a different identity.')
                valid = old.get('scope_token') and old.get('expires_at', 0) > now + 30
                refreshed = old.get('scope_token') != baseline_token
                if valid and (not force or refreshed):
                    return old
                if old.get('lease_until', 0) > now:
                    return old
                return {**old, 'owner': owner, 'lease': lease, 'lease_until': now + 60}
            state = await self.store.mutate_catalog_session(session_id, claim)
            if state.get('lease') != lease:
                if state.get('scope_token') and state.get('expires_at', 0) > now + 30 and (not force or state.get('scope_token') != baseline_token):
                    return state
                await asyncio.sleep(0.05)
                continue
            try:
                async with asyncio.timeout(45), httpx.AsyncClient(timeout=45, transport=self.transport) as client:
                    response = await client.post(self.url, headers=side_channel_headers(jwt, session_id))
                if response.status_code >= 400:
                    raise HTTPException(503, detail='Session catalog initialization failed.')
                snapshot = response.json()
                if (snapshot.get('owner') != owner or snapshot.get('session_id') != session_id
                        or not isinstance(snapshot.get('scope_token'), str)
                        or not isinstance(snapshot.get('columns'), list)
                        or not all(isinstance(c, str) for c in snapshot['columns'])
                        or snapshot.get('expires_at', 0) <= time.time() + 30):
                    raise ValueError('Invalid session catalog')
                load_catalog_handles_from_export(snapshot)
                def commit(old):
                    if not old or old.get('lease') != lease:
                        raise HTTPException(409, detail='Session catalog initialization lease expired.')
                    return snapshot
                return await self.store.mutate_catalog_session(session_id, commit)
            except BaseException as exc:
                def release(old):
                    if old and old.get('lease') == lease:
                        return {k: v for k, v in old.items() if k not in ('lease', 'lease_until')}
                    return old
                await self.store.mutate_catalog_session(session_id, release)
                if isinstance(exc, (HTTPException, asyncio.CancelledError)):
                    raise
                raise HTTPException(503, detail='Session catalog initialization failed.') from None
        raise HTTPException(503, detail='Session catalog initialization timed out.')

    async def credentials(self, credentials: RuntimeCredentials):
        snapshot = await self.ensure(jwt=credentials.jwt, session_id=credentials.session_id)
        # Existing replay code uses empty=unrestricted. A non-column sentinel
        # represents deny-all, so an empty scoped catalog can never widen access.
        scope = frozenset(snapshot['columns']) or frozenset({'__no_catalog_access__'})
        return replace(credentials, column_scope=scope)

    async def scope_token(self, *, jwt, session_id):
        return (await self.ensure(jwt=jwt, session_id=session_id))['scope_token']

    async def catalog_handle(self, credentials):
        snapshot = await self.ensure(jwt=credentials.jwt, session_id=credentials.session_id)
        return load_catalog_handles_from_export(snapshot)[0]
