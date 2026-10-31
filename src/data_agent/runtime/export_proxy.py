"""Authenticated snapshot REST proxy; query execution stays in clickhouse-api."""

from __future__ import annotations

import json
import logging
from uuid import UUID

import anyio
import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, ValidationError, model_validator
from starlette.background import BackgroundTask

from data_agent.runtime.mcp._transport import trace_headers

_logger = logging.getLogger(__name__)
_UNAVAILABLE = {"error": "The query service could not be run.", "error_code": "EXPORT_UNAVAILABLE"}


class QueryPageRequest(BaseModel):
    """SQL creates/reuses a saved result; snapshot_id pages that result."""
    model_config = {"extra": "forbid"}
    refresh_of: str | None = Field(None, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    sql: str | None = None
    snapshot_id: str | None = Field(None, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    limit: int = Field(100, ge=1, le=1000)
    offset: int = Field(0, ge=0)

    @model_validator(mode="after")
    def validate_operation(self):
        if self.sql is not None and not self.sql.strip():
            raise ValueError("SQL must not be blank.")
        if (self.sql is not None) == (self.snapshot_id is not None):
            raise ValueError("Supply exactly one of sql or snapshot_id.")
        if self.snapshot_id and self.refresh_of:
            raise ValueError("Paging a saved result cannot request refresh.")
        if self.refresh_of and self.offset:
            raise ValueError("Refresh starts at offset zero.")
        return self



def export_router(settings, extract_credentials, session_catalog, *, transport=None):
    router = APIRouter(prefix="/query", tags=["Answer Snapshots"])

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
            **trace_headers(),
            "Authorization": authorization,
            "X-Session-Id": credentials.session_id,
            "X-Catalog-Scope": scope,
        }
        if request.method == "POST":
            headers["Content-Type"] = request.headers.get("content-type", "application/json")
        body = await request.body() if request.method == "POST" else None
        requested_snapshot_id = None
        if request.method == "POST":
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                return JSONResponse(status_code=400, content={"error": "Invalid query request."})
            if isinstance(payload, dict):
                requested_snapshot_id = payload.get("snapshot_id")
                if "table_ref" in payload:
                    return JSONResponse(status_code=400, content={"error": "Supply SQL or a snapshot_id, not table_ref."})
        page_request = None
        if path == "/page":
            try:
                page_request = QueryPageRequest.model_validate(payload)
            except ValidationError:
                return JSONResponse(status_code=422, content={"error": "Supply SQL or a snapshot_id, valid paging bounds, and refresh_of only with SQL at offset zero."})
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
            url = httpx.URL(f"{base}/query{path}").copy_with(
                query=request.scope.get("query_string", b"")
            )
            created = None
            method = request.method
            if page_request is not None:
                if page_request.sql is not None:
                    url = httpx.URL(f"{base}/query/snapshots")
                    body = json.dumps(page_request.model_dump(include={"sql", "refresh_of"}, exclude_none=True)).encode()
                else:
                    method, body = "GET", None
                    url = httpx.URL(f"{base}/query/snapshots/{page_request.snapshot_id}/page").copy_merge_params(
                        {"limit": page_request.limit, "offset": page_request.offset})
            outgoing = client.build_request(method, url, headers=headers, content=body)
            upstream = await client.send(outgoing, stream=True)
            if page_request is not None and page_request.sql is not None and upstream.is_success:
                await upstream.aread()
                created = upstream.json()
                snapshot_id = str(UUID(created["snapshot_id"]))
                await upstream.aclose()
                outgoing = client.build_request(
                    "GET", f"{base}/query/snapshots/{snapshot_id}/page", headers=headers,
                    params={"limit": page_request.limit, "offset": page_request.offset})
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
                (request.method == "GET" or path == "/page" and requested_snapshot_id)
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
                    snapshot_id = str(UUID(requested_snapshot_id if path == "/page" else path.split("/")[2]))
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
            if page_request is not None and upstream.is_success:
                payload["reset_table"] = bool(page_request.refresh_of)
                if created is not None and "created_at" in created:
                    payload["created_at"] = created["created_at"]
                content = json.dumps(payload).encode()
            response_headers["content-type"] = "application/json"
            return Response(content, status_code=upstream.status_code, headers=response_headers)
        except httpx.HTTPError as exc:
            _logger.warning("snapshot proxy failed failure_class=%s", type(exc).__name__)
            await close()
            return JSONResponse(status_code=502, content=_UNAVAILABLE)
        except (ValueError, KeyError, TypeError) as exc:
            _logger.warning("snapshot response invalid failure_class=%s", type(exc).__name__)
            await close()
            return JSONResponse(status_code=502, content=_UNAVAILABLE)
        except BaseException:
            await close()
            raise
        finally:
            if upstream is not None and not (download and upstream.is_success):
                await close()

    @router.post("/page")
    async def query_page(
        request: Request,
        authorization: str | None = Header(default=None),
        x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    ):
        return await proxy(request, "/page", authorization, x_session_id)

    @router.post("/snapshots")
    async def create(
        request: Request,
        authorization: str | None = Header(default=None),
        x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    ):
        return await proxy(request, "/snapshots", authorization, x_session_id)

    @router.get("/snapshots/{snapshot_id}/page")
    async def page(
        snapshot_id: UUID,
        request: Request,
        authorization: str | None = Header(default=None),
        x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    ):
        return await proxy(request, f"/snapshots/{snapshot_id}/page", authorization, x_session_id)

    @router.get("/snapshots/{snapshot_id}/download-urls")
    async def urls(
        snapshot_id: UUID,
        request: Request,
        authorization: str | None = Header(default=None),
        x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    ):
        return await proxy(request, f"/snapshots/{snapshot_id}/download-urls", authorization, x_session_id)

    @router.get("/snapshots/{snapshot_id}/download")
    async def download(
        snapshot_id: UUID,
        request: Request,
        authorization: str | None = Header(default=None),
        x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    ):
        return await proxy(
            request, f"/snapshots/{snapshot_id}/download", authorization, x_session_id, download=True
        )

    return router
