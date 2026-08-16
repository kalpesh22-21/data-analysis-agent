"""Shared transport primitives for the MCP HTTP SIDE-CHANNELS.

Three clients talk to plain HTTP custom routes on the MCP host rather than through
the MCP tool protocol — `catalog/export_client.py` (`/catalog/export`),
`retrieval/corpus_client.py` (the corpus exports) and `mcp/scratch_client.py` (the
D93 scratch materialize/drop routes) — plus `mcp/real_client.py`, which sends the
same headers on the tool plane. All four sat behind the SAME `JWTAuthMiddleware` and
each carried its own copy of the credential binding, the service-key switch, and the
error-body parse.

That mattered because two of the three duplicated things are SECURITY surface:

  * `side_channel_headers` is the D5 credential binding. Every side-channel rides the
    read plane's own binding — the JWT authenticates WHO, `X-Session-Id` carries the
    session binding (and, for scratch, D92-NAMES the table). A client that spelled the
    header differently would authenticate against a middleware that is not looking at
    that header, i.e. fail; one that spelled it the same but only SOMETIMES would be
    worse.
  * `auth_headers` is the service-key ESCAPE from that binding: a non-request principal
    (the singleton hydrator daemon, the decoupled runtime catalog fetch) sends
    `X-Service-Key` INSTEAD of the Bearer/session pair. "Instead" is the load-bearing
    word — when a service key is configured the per-request `jwt`/`session_id` are
    IGNORED and never reach the wire. One implementation of that switch means one place
    to audit that a user JWT cannot leak onto a daemon's connection.

`error_from_response` is the third: parsing `{code, error}` out of an endpoint's error
body, with the status-line fallback when the body is absent or unparseable.

**The exception classes stay per-client on purpose.** `CatalogClientError`,
`CorpusClientError` and `ScratchClientError` are what every `except` site names, and
the three failures have genuinely different consequences (the catalog degrades to an
empty handle, the corpus degrades the hydrate, scratch fails the composite node back to
the raw loop). They subclass `SideChannelError` so they share the `(code, message)`
shape without collapsing into one catch-all.
"""

from __future__ import annotations

import httpx


class SideChannelError(Exception):
    """Base for an MCP side-channel call that was rejected or returned an unusable body.

    Carries the endpoint's stable *code* when the JSON error body supplies one (e.g.
    ``SCRATCH_TOO_LARGE``), so a caller can branch on the failure without parsing prose.
    Callers catch the per-client SUBCLASS, not this."""

    def __init__(self, code: str | None, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def side_channel_headers(jwt: str, session_id: str) -> dict[str, str]:
    """The D5 credential binding every MCP call rides — tool plane and side channels
    alike. The JWT authenticates WHO; `X-Session-Id` carries the session binding (which
    on the scratch route also NAMES the table, D92). Neither is ever reflected back into
    a handle, a corpus node, or a model-visible message."""
    return {"Authorization": f"Bearer {jwt}", "X-Session-Id": session_id}


def auth_headers(*, service_key: str | None, jwt: str, session_id: str) -> dict[str, str]:
    """The auth headers for one side-channel fetch: the static service key when one is
    configured, else the per-request Bearer/session pair.

    When *service_key* is set the per-request *jwt*/*session_id* are IGNORED — they do
    not reach the wire. That is what lets a NON-request principal (the singleton hydrator
    daemon) authenticate an export with no user JWT in hand."""
    if service_key:
        return {"X-Service-Key": service_key}
    return side_channel_headers(jwt, session_id)


def error_from_response(
    resp: httpx.Response, *, error_class: type[SideChannelError], description: str
) -> SideChannelError:
    """Build *error_class* from a >=400 response, preferring the endpoint's own
    `{"code": ..., "error": ...}` body over the status line.

    *description* names the endpoint for the fallback message (e.g. `"scratch
    endpoint"`), which reads `"<description> returned HTTP <status>"`.

    A body that does not parse, or does not parse to a dict, is NOT an error here — the
    HTTP status already established that the call failed, and the response is under the
    server's control, not ours. `except Exception` rather than `except ValueError`
    because `resp.json()` is not contractually limited to `ValueError` (an encoding
    failure raises otherwise), and losing the real HTTP status to a decoder's choice of
    exception type would turn a clean fail-closed degrade into an unhandled raise."""
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
