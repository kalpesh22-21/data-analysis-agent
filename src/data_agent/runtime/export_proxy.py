"""Authenticated snapshot REST proxy; query execution stays in clickhouse-api."""

from __future__ import annotations

import json
import logging
from uuid import UUID

import anyio
import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

_logger = logging.getLogger(__name__)
_UNAVAILABLE = {"error": "The query service could not be run.", "error_code": "EXPORT_UNAVAILABLE"}


def export_router(settings, extract_credentials, session_catalog, *, transport=None):
    router = APIRouter(prefix="/query/snapshots", tags=["Answer Snapshots"])

    async def proxy(request, path, authorization, session_id, *, download=False):
        credentials = extract_credentials(
            authorization=authorization, session_id=session_id, settings=settings
        )
        base = settings.clickhouse_api_rest_base()
        if not base:
            return JSONResponse(
                status_code=501,
                content={
                    "error": "Export snapshots are not configured.",
                    "error_code": "EXPORT_NOT_CONFIGURED",
                },
            )
        if session_catalog is None:
            return JSONResponse(status_code=503, content=_UNAVAILABLE)
        # The backend requires its server-minted scope token. Never trust an
        # inbound scope header or derive new permissions in this proxy.
        try:
            scope = await session_catalog.scope_token(
                jwt=credentials.jwt, session_id=credentials.session_id
            )
        except HTTPException:
            raise
        except Exception as exc:
            _logger.warning("snapshot scope unavailable failure_class=%s", type(exc).__name__)
            return JSONResponse(status_code=502, content=_UNAVAILABLE)
        headers = {
            "Authorization": authorization,
            "X-Session-Id": credentials.session_id,
            "X-Catalog-Scope": scope,
        }
        if request.method == "POST":
            headers["Content-Type"] = request.headers.get("content-type", "application/json")
        timeout = 150.0 if request.method == "POST" else 120.0 if download else 60.0
        client = httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False)
        upstream = None

        async def close():
            with anyio.CancelScope(shield=True):
                try:
                    if upstream is not None:
                        await upstream.aclose()
                finally:
                    await client.aclose()

        try:
            url = httpx.URL(f"{base}/query/snapshots{path}").copy_with(
                query=request.scope.get("query_string", b"")
            )
            outgoing = client.build_request(
                request.method,
                url,
                headers=headers,
                content=await request.body() if request.method == "POST" else None,
            )
            upstream = await client.send(outgoing, stream=True)
            response_headers = {
                key: upstream.headers[key]
                for key in ("content-type", "content-disposition", "retry-after")
                if key in upstream.headers
            }
            response_headers["Cache-Control"] = "no-store"
            if download and upstream.is_success:

                async def chunks():
                    try:
                        async for chunk in upstream.aiter_bytes():
                            yield chunk
                    except httpx.HTTPError as exc:
                        # Abort the stream; never turn a truncated CSV into a
                        # successful-looking EOF or append an error JSON row.
                        _logger.warning(
                            "snapshot stream failed failure_class=%s", type(exc).__name__
                        )
                        raise
                    finally:
                        await close()

                return StreamingResponse(
                    chunks(),
                    status_code=upstream.status_code,
                    headers=response_headers,
                    background=BackgroundTask(close),
                )
            content = await upstream.aread()
            try:
                payload = json.loads(content)
            except (ValueError, UnicodeDecodeError):
                return JSONResponse(status_code=502, content=_UNAVAILABLE)
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if (
                request.method == "GET"
                and upstream.status_code == 409
                and isinstance(detail, dict)
                and detail.get("code") == "SNAPSHOT_SCOPE_CHANGED"
            ):
                # One recovery attempt; never replay an old offset or download
                # old bytes. The UI must replace its reference and clear rows.
                await upstream.aclose()
                try:
                    refreshed = await session_catalog.ensure(
                        jwt=credentials.jwt, session_id=credentials.session_id, force=True
                    )
                    headers["X-Catalog-Scope"] = refreshed["scope_token"]
                    snapshot_id = str(UUID(path.split("/")[1]))
                    rebuilt = await client.post(
                        f"{base}/query/snapshots/{snapshot_id}/rebuild",
                        headers=headers,
                        timeout=150.0,
                    )
                    replacement = rebuilt.json()
                except HTTPException as exc:
                    return JSONResponse(
                        status_code=exc.status_code,
                        content={"detail": exc.detail, "reset_table": True},
                        headers=exc.headers,
                    )
                except Exception as exc:
                    _logger.warning("snapshot recovery failed failure_class=%s", type(exc).__name__)
                    return JSONResponse(
                        status_code=502, content={**_UNAVAILABLE, "reset_table": True}
                    )
                recovery_headers = {"Cache-Control": "no-store"}
                if "retry-after" in rebuilt.headers:
                    recovery_headers["Retry-After"] = rebuilt.headers["retry-after"]
                if rebuilt.status_code == 201:
                    return JSONResponse(
                        status_code=409,
                        content={
                            "detail": {
                                "code": "SNAPSHOT_REPLACED",
                                "error": "Your access scope changed. Replace the displayed snapshot.",
                            },
                            "snapshot": replacement,
                            "reset_table": True,
                        },
                        headers=recovery_headers,
                    )
                if isinstance(replacement, dict):
                    replacement = {**replacement, "reset_table": True}
                return JSONResponse(
                    status_code=rebuilt.status_code, content=replacement, headers=recovery_headers
                )
            response_headers["content-type"] = "application/json"
            return Response(content, status_code=upstream.status_code, headers=response_headers)
        except httpx.HTTPError as exc:
            _logger.warning("snapshot proxy failed failure_class=%s", type(exc).__name__)
            await close()
            return JSONResponse(status_code=502, content=_UNAVAILABLE)
        except BaseException:
            await close()
            raise
        finally:
            if upstream is not None and not (download and upstream.is_success):
                await close()

    @router.post("")
    async def create(
        request: Request,
        authorization: str | None = Header(default=None),
        x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    ):
        return await proxy(request, "", authorization, x_session_id)

    @router.get("/{snapshot_id}/page")
    async def page(
        snapshot_id: UUID,
        request: Request,
        authorization: str | None = Header(default=None),
        x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    ):
        return await proxy(request, f"/{snapshot_id}/page", authorization, x_session_id)

    @router.get("/{snapshot_id}/download-urls")
    async def urls(
        snapshot_id: UUID,
        request: Request,
        authorization: str | None = Header(default=None),
        x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    ):
        return await proxy(request, f"/{snapshot_id}/download-urls", authorization, x_session_id)

    @router.get("/{snapshot_id}/download")
    async def download(
        snapshot_id: UUID,
        request: Request,
        authorization: str | None = Header(default=None),
        x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    ):
        return await proxy(
            request, f"/{snapshot_id}/download", authorization, x_session_id, download=True
        )

    return router
