"""auth/jwt_verify.py — local JWKS verification (design §11 sub-decision B).

Defense-in-depth only: this module's verification result never gates a live
MCP tool call — `clickhouse-api`'s own `app/auth_jwt.py::validate_token`
remains the sole *live* enforcement boundary (D57/D80). This module exists
solely so the runtime's own `context/scope_filter.py` D44 replay-filter can
trust the `column_scope` it uses to decide what history to re-show the
*same* already-authenticated user; a forged/stale JWT here is low-severity
(self-serves the same session, not cross-tenant) — see design §11 for the
full rationale.

Mirrors `clickhouse-api`'s own pattern (PyJWT + `PyJWKClient`, RS256, JWKS)
so the two services can never disagree on *how* a JWT is validated — same
library, same algorithm pinning, same issuer/audience/expiry checks. One
deliberate divergence (LOCKED per the orchestrator brief):
`clickhouse-api`'s live enforcement REJECTS a token with a missing/empty
`column_scope` claim (fail-closed — it gates live query execution); this
module treats a missing/empty/unparseable claim as `frozenset()`
(allow-all), matching `scope_filter.py`'s own D80(b) semantics exactly —
appropriate here because this only shapes replayed history for the same
user, never a live query.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import jwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

_ALGORITHMS = ["RS256"]

# One PyJWKClient per JWKS URL, reused for the life of the process (mirrors
# clickhouse-api's own module-level cache — avoids refetching keys per call).
_jwk_clients: dict[str, PyJWKClient] = {}


class JWTVerificationError(Exception):
    """Raised when a token fails signature/issuer/audience/expiry verification."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _jwk_client(jwks_url: str) -> PyJWKClient:
    client = _jwk_clients.get(jwks_url)
    if client is None:
        client = PyJWKClient(jwks_url, cache_keys=True)
        _jwk_clients[jwks_url] = client
    return client


def _extract_column_scope(claims: dict[str, Any]) -> frozenset[str]:
    """Decode the `column_scope` claim -> `frozenset[str]`.

    Missing / unparseable / wrong-shaped -> `frozenset()` (allow-all), per
    the module docstring's LOCKED divergence from clickhouse-api's stricter
    live-enforcement behavior.
    """
    raw_scope = claims.get("column_scope")
    if raw_scope is None:
        return frozenset()

    if isinstance(raw_scope, str):
        try:
            scope_list = json.loads(raw_scope)
        except (json.JSONDecodeError, ValueError):
            logger.info("column_scope claim is not valid JSON; treating as allow-all.")
            return frozenset()
    elif isinstance(raw_scope, list):
        scope_list = raw_scope
    else:
        logger.info(
            "column_scope claim has unexpected type %s; treating as allow-all.",
            type(raw_scope).__name__,
        )
        return frozenset()

    if not isinstance(scope_list, list) or not all(isinstance(item, str) for item in scope_list):
        logger.info("column_scope claim is not a list of strings; treating as allow-all.")
        return frozenset()

    return frozenset(scope_list)


def verify_jwt(
    token: str,
    *,
    jwks_url: str,
    issuer: str,
    audience: str,
    signing_key_resolver: Callable[[str], Any] | None = None,
) -> frozenset[str]:
    """Verify *token* against *jwks_url*/*issuer*/*audience* and return its `column_scope`.

    *signing_key_resolver*, if given, replaces the real `PyJWKClient` HTTP
    fetch — the Layer-1 test seam (design §8: "test JWKS fixture, self-signed
    test key pair"). `app.py` never passes it (real `PyJWKClient` path).

    Raises:
        JWTVerificationError: on an empty token, an unreachable/unmatched
            JWKS key, or any signature/issuer/audience/expiry failure.
    """
    if not token or not token.strip():
        raise JWTVerificationError("Empty bearer token.", code="MISSING_AUTH")

    try:
        if signing_key_resolver is not None:
            signing_key = signing_key_resolver(token)
        else:
            signing_key = _jwk_client(jwks_url).get_signing_key_from_jwt(token).key
    except jwt.PyJWKClientError as exc:
        raise JWTVerificationError(
            "Unable to verify token signing key.", code="JWKS_UNAVAILABLE"
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise JWTVerificationError("Invalid token.", code="INVALID_AUTH") from exc

    try:
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=_ALGORITHMS,
            audience=audience,
            issuer=issuer,
            leeway=60,  # tolerate up to 60s of clock skew on exp/nbf/iat
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise JWTVerificationError("Token has expired.", code="TOKEN_EXPIRED") from exc
    except jwt.InvalidAudienceError as exc:
        raise JWTVerificationError(
            "Token audience is not accepted.", code="INVALID_AUDIENCE"
        ) from exc
    except jwt.InvalidIssuerError as exc:
        raise JWTVerificationError("Token issuer is not accepted.", code="INVALID_ISSUER") from exc
    except jwt.InvalidTokenError as exc:
        logger.info("JWT validation failed: %s", exc)
        raise JWTVerificationError("Invalid token.", code="INVALID_AUTH") from exc

    return _extract_column_scope(claims)


__all__ = ["JWTVerificationError", "verify_jwt"]
