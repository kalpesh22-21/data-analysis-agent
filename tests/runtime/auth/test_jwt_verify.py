"""Unit tests for auth/jwt_verify.py (Layer 1 — self-signed test JWKS fixture, no live IdP)."""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from data_agent.runtime.auth.jwt_verify import JWTVerificationError, verify_jwt

ISSUER = "https://issuer.test"
AUDIENCE = "data-agent-runtime"


@pytest.fixture(scope="module")
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _make_token(private_key, *, claims: dict | None = None, **overrides) -> str:
    now = int(time.time())
    payload = {
        "sub": "user-1",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 3600,
    }
    if claims:
        payload.update(claims)
    payload.update(overrides)
    return jwt.encode(payload, private_key, algorithm="RS256")


def _resolver(public_key):
    return lambda token: public_key  # noqa: ARG005


def test_valid_token_with_column_scope_returns_correct_frozenset(keypair) -> None:
    private_key, public_key = keypair
    scope = ["dbpcm_warehouse.employee.Department", "dbpcm_warehouse.employee.EmployeeCode"]
    token = _make_token(private_key, claims={"column_scope": scope})

    result = verify_jwt(
        token,
        jwks_url="unused",
        issuer=ISSUER,
        audience=AUDIENCE,
        signing_key_resolver=_resolver(public_key),
    )

    assert result == frozenset(scope)


def test_column_scope_as_json_string_is_parsed(keypair) -> None:
    private_key, public_key = keypair
    token = _make_token(private_key, claims={"column_scope": '["a.b.c"]'})

    result = verify_jwt(
        token,
        jwks_url="unused",
        issuer=ISSUER,
        audience=AUDIENCE,
        signing_key_resolver=_resolver(public_key),
    )

    assert result == frozenset({"a.b.c"})


def test_missing_column_scope_claim_is_allow_all(keypair) -> None:
    private_key, public_key = keypair
    token = _make_token(private_key)  # no column_scope claim at all

    result = verify_jwt(
        token,
        jwks_url="unused",
        issuer=ISSUER,
        audience=AUDIENCE,
        signing_key_resolver=_resolver(public_key),
    )

    assert result == frozenset()


def test_empty_column_scope_list_is_allow_all(keypair) -> None:
    private_key, public_key = keypair
    token = _make_token(private_key, claims={"column_scope": []})

    result = verify_jwt(
        token,
        jwks_url="unused",
        issuer=ISSUER,
        audience=AUDIENCE,
        signing_key_resolver=_resolver(public_key),
    )

    assert result == frozenset()


def test_malformed_column_scope_json_string_is_allow_all(keypair) -> None:
    private_key, public_key = keypair
    token = _make_token(private_key, claims={"column_scope": "{not json]"})

    result = verify_jwt(
        token,
        jwks_url="unused",
        issuer=ISSUER,
        audience=AUDIENCE,
        signing_key_resolver=_resolver(public_key),
    )

    assert result == frozenset()


def test_bad_signature_is_rejected(keypair) -> None:
    _, public_key = keypair
    other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _make_token(other_private_key)  # signed with a DIFFERENT key

    with pytest.raises(JWTVerificationError) as exc_info:
        verify_jwt(
            token,
            jwks_url="unused",
            issuer=ISSUER,
            audience=AUDIENCE,
            signing_key_resolver=_resolver(public_key),
        )
    assert exc_info.value.code == "INVALID_AUTH"


def test_wrong_issuer_is_rejected(keypair) -> None:
    private_key, public_key = keypair
    token = _make_token(private_key, iss="https://someone-else.example")

    with pytest.raises(JWTVerificationError) as exc_info:
        verify_jwt(
            token,
            jwks_url="unused",
            issuer=ISSUER,
            audience=AUDIENCE,
            signing_key_resolver=_resolver(public_key),
        )
    assert exc_info.value.code == "INVALID_ISSUER"


def test_wrong_audience_is_rejected(keypair) -> None:
    private_key, public_key = keypair
    token = _make_token(private_key, aud="someone-else")

    with pytest.raises(JWTVerificationError) as exc_info:
        verify_jwt(
            token,
            jwks_url="unused",
            issuer=ISSUER,
            audience=AUDIENCE,
            signing_key_resolver=_resolver(public_key),
        )
    assert exc_info.value.code == "INVALID_AUDIENCE"


def test_expired_token_is_rejected(keypair) -> None:
    private_key, public_key = keypair
    now = int(time.time())
    token = _make_token(private_key, iat=now - 10_000, exp=now - 9_000)

    with pytest.raises(JWTVerificationError) as exc_info:
        verify_jwt(
            token,
            jwks_url="unused",
            issuer=ISSUER,
            audience=AUDIENCE,
            signing_key_resolver=_resolver(public_key),
        )
    assert exc_info.value.code == "TOKEN_EXPIRED"


def test_empty_token_is_rejected() -> None:
    with pytest.raises(JWTVerificationError) as exc_info:
        verify_jwt("", jwks_url="unused", issuer=ISSUER, audience=AUDIENCE)
    assert exc_info.value.code == "MISSING_AUTH"
