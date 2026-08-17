"""Layer-1 — the offline replay token carries the three warehouse TENANT claims.

Why this file exists at all
---------------------------
`HttpTokenMinter` posted `user_name`/`column_scope`/`ttl_seconds`/`session_id` and NO
`claims`. The MCP requires `clientcode`/`proc_center`/`jti` on every call
(`app/auth_jwt.py::validate_token` → `403 MISSING_TENANT_CLAIM`, before any tool
runs), so every golden replay was rejected at the transport, `MCPWarehouseProbe.run`
raised, and `golden_replay` degraded to `probe_unavailable` — the SAME clean hold a
healthy fail-closed run produces. The structural gate on every promotion was dark and
nothing in the suite could see it, because no test asserted the shape of the request
body and the fake minter has no body at all.

So the proofs here are about the BYTES ON THE WIRE (`httpx.MockTransport`), not about
a collaborator being called:
  * the mint body carries all three claims with the configured values;
  * `TenantClaims` refuses, at construction, every value the MCP would reject —
    derived from what auth_jwt/clickhouse_client actually do with them, not from a
    guess about what a tenant code looks like;
  * a `ValueError` (not `TokenMintError`) on bad config, because a `TokenMintError`
    on this path is swallowed into the very hold that hid the defect;
  * `tenant` is required, so no future wiring site can silently omit it again.
"""

from __future__ import annotations

import json

import httpx
import pytest

from data_agent.learning.promotion.token_minter import (
    HttpTokenMinter,
    TenantClaims,
    TokenMintError,
)

_TENANT = TenantClaims(clientcode="CLIENT_A", proc_center="PC01", jti="TESTJTI001")
_SCOPE = ["dbpcm_warehouse.employee.employee_code"]


def _minter(handler, **kwargs) -> HttpTokenMinter:
    return HttpTokenMinter(
        "http://token.test/token",
        "issuer-key",
        tenant=kwargs.pop("tenant", _TENANT),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


# --- the mint body ---------------------------------------------------------------


async def test_mint_body_carries_the_three_tenant_claims() -> None:
    """The exact three claim NAMES the MCP's CLICKHOUSE_TENANT_SETTINGS maps. A
    renamed or dropped key is a 403 on every replay."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"access_token": "jwt-x"})

    token = await _minter(handler).mint(_SCOPE, session_id="learning-replay-1")

    assert token == "jwt-x"
    assert seen[0]["claims"] == {
        "clientcode": "CLIENT_A",
        "proc_center": "PC01",
        "jti": "TESTJTI001",
    }


async def test_mint_body_keeps_scope_and_session_binding_alongside_the_claims() -> None:
    """The claims are ADDITIVE — the D57 scope and the Slice-1 sid binding are still
    exactly what they were (a fix that widened the scope would be worse than the bug)."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"access_token": "jwt-x"})

    await _minter(handler).mint(_SCOPE, session_id="learning-replay-2")

    body = seen[0]
    assert body["column_scope"] == _SCOPE
    assert body["session_id"] == "learning-replay-2"
    assert body["user_name"] == "learning-scheduler"
    assert body["ttl_seconds"] == 300


async def test_empty_scope_still_refuses_before_any_request() -> None:
    """The allow-all backstop is untouched by the claims work: an empty `uses`
    refuses to mint and never reaches the transport."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls.append(request)
        return httpx.Response(200, json={"access_token": "jwt-x"})

    with pytest.raises(TokenMintError):
        await _minter(handler).mint([], session_id="learning-replay-3")
    assert calls == []


async def test_empty_scope_mints_only_when_the_caller_opts_in_explicitly() -> None:
    """`allow_unscoped=True` is the REQUEST path's opt-out (D80b: a resolved entitlement
    of `[]` means "no column restriction", not "no scope was computed"). It exists so
    `ui/server.py` can share this transport; the default above is what every learning
    -plane site keeps."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"access_token": "jwt-x"})

    token = await _minter(handler, allow_unscoped=True).mint([], session_id="ui-1")

    assert token == "jwt-x"
    assert seen[0]["column_scope"] == []


async def test_ttl_none_omits_the_field_so_the_idp_default_applies() -> None:
    """A request-path session token must outlive a conversation, and the IdP already
    owns that number (`token_ttl_seconds`). Omitted, NOT null: an absent key is the
    exact body the hand-rolled mint sites posted."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"access_token": "jwt-x"})

    await _minter(handler, ttl_seconds=None).mint(_SCOPE, session_id="ui-2")

    assert "ttl_seconds" not in seen[0]


# --- TenantClaims validation (derived from the downstream reads) ------------------


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_claim_refused(blank: str) -> None:
    """`auth_jwt.validate_token` rejects a claim that is blank-after-strip. Building
    one here would mint a token guaranteed to 403 and hold silently."""
    with pytest.raises(ValueError, match="blank"):
        TenantClaims(clientcode=blank, proc_center="PC01", jti="J")


def test_non_string_claim_refused() -> None:
    """`clickhouse_client.tenant_settings` does `str(value)` into a ClickHouse
    setting — an unset settings field arriving as `None` would become the literal
    string "None" and match no tenant's rows."""
    with pytest.raises(ValueError, match="must be a string"):
        TenantClaims(clientcode=None, proc_center="PC01", jti="J")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad",
    [
        "CLIENT\nA",  # C0
        "CLIENT\rA",  # C0
        "CLIENT\x00A",  # C0
        "CLIENT\x7fA",  # DEL
        "CLIENT\x85A",  # C1 NEL — constructed FINE while the docstring said it did not
        "CLIENT\x9bA",  # C1 CSI
    ],
)
def test_control_character_claim_refused(bad: str) -> None:
    """The value is placed on the wire as an HTTP query parameter to ClickHouse
    (clickhouse-connect settings); a control character has no legitimate reading.

    The C1 cases are regressions: the predicate was `ch < " " or ch == "\\x7f"` while
    the docstring advertised C0 AND C1, so the range and its own description had
    drifted apart. Fixed by using Unicode category `Cc`, which IS the advertised set."""
    with pytest.raises(ValueError, match="control character"):
        TenantClaims(clientcode=bad, proc_center="PC01", jti="J")


def test_non_control_non_ascii_is_still_allowed() -> None:
    """`Cc` is the whole guard: no charset allowlist. A tenant code is an opaque
    string a row policy compares for equality, so refusing (say) an accented letter
    would be a rule invented from an intent rather than read off the operation."""
    tenant = TenantClaims(clientcode="CLIÉNT_Ä", proc_center="PC01", jti="J")
    assert tenant.as_claims()["clientcode"] == "CLIÉNT_Ä"


def test_surrounding_whitespace_is_stripped_not_rejected() -> None:
    """`"CLIENT_A "` passes the IdP and the MCP presence check and then matches ZERO
    rows under `client_code = getSetting('paycom_client_code')` — a silently-empty
    replay. Stripping cannot turn tenant A into tenant B, so it is safe to normalize."""
    tenant = TenantClaims(clientcode=" CLIENT_A ", proc_center="PC01\n ", jti=" J ")
    assert tenant.as_claims() == {
        "clientcode": "CLIENT_A",
        "proc_center": "PC01",
        "jti": "J",
    }


def test_tenant_is_required_at_construction() -> None:
    """No default: a new mint site cannot omit the claims without a TypeError. The
    old failure was invisible precisely because omission had no local symptom."""
    with pytest.raises(TypeError):
        HttpTokenMinter("http://token.test/token", "issuer-key")  # type: ignore[call-arg]
