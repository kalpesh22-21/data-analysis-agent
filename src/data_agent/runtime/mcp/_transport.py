"""Shared transport primitives for the MCP HTTP SIDE-CHANNELS.

`side_channel_headers` is the D5 credential binding every MCP call rides; spelling
those headers differently authenticates against a middleware that is not reading them.
`auth_headers` is the service-key ESCAPE from it — with a service key configured the
per-request `jwt`/`session_id` are IGNORED and never reach the wire, so a non-request
principal can authenticate with no user JWT. The per-client error subclasses stay
separate because their consequences differ; they share only the `(code, message)` shape.
"""

from __future__ import annotations

import httpx


class SideChannelError(Exception):
    """Base for an MCP side-channel call that was rejected or returned an unusable body.

        Carries the endpoint's stable *code* when the JSON error body supplies one (e.g.
        ``SCRATCH_TOO_LARGE``), so a caller can branch on the failure without parsing prose.
        Callers catch the per-client SUBCLASS, not this.
    """

    def __init__(self, code: str | None, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def side_channel_headers(jwt: str, session_id: str) -> dict[str, str]:
    """The D5 credential binding every MCP call rides — tool plane and side channels alike.

        The JWT authenticates WHO; `X-Session-Id` carries the session binding (which on the
        scratch route also NAMES the table, D92). Neither is ever reflected back into a
        handle, a corpus node, or a model-visible message.
    """
    return {"Authorization": f"Bearer {jwt}", "X-Session-Id": session_id}


def auth_headers(*, service_key: str | None, jwt: str, session_id: str) -> dict[str, str]:
    """The auth headers for one side-channel fetch: the static service key when one is
        configured, else the per-request Bearer/session pair.

        With *service_key* set the per-request *jwt*/*session_id* are IGNORED and never
        reach the wire — that is what lets a NON-request principal (the singleton hydrator
        daemon) authenticate an export with no user JWT in hand.
    """
    if service_key:
        return {"X-Service-Key": service_key}
    return side_channel_headers(jwt, session_id)


def error_from_response(
    resp: httpx.Response, *, error_class: type[SideChannelError], description: str
) -> SideChannelError:
    """Build *error_class* from a >=400 response, preferring the endpoint's own
        `{"code": ..., "error": ...}` body over the status line.

        *description* names the endpoint in the fallback message ("<description> returned
        HTTP <status>"). An unparseable body is not itself an error — the status already
        established the failure — and the parse is guarded by a bare `except Exception`
        because `resp.json()` is not contractually limited to `ValueError`.
    """
    code: str | None = None
    message = f"{description} returned HTTP {resp.status_code}"
    try:
        body = resp.json()
        if isinstance(body, dict):
            code = body.get("code")
            message = body.get("error") or message
    except Exception:  # noqa: BLE001 - a non-JSON error body is still an error
        pass
    return error_class(code, message)


__all__ = [
    "SideChannelError",
    "auth_headers",
    "error_from_response",
    "side_channel_headers",
]
