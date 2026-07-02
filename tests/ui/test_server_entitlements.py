"""Hermetic unit tests for per-user `column_scope` entitlement resolution at the
BFF (auth-hardening Slice 2, data-agent side).

`create_session` no longer mints a hardcoded allow-all token (D82 interim); it
resolves the caller's identity → their entitled `column_scope` through the
`ui/entitlements.py` seams (`resolve_caller_identity` / `resolve_column_scope`),
then mints with THAT scope. These tests capture the mint calls (token service
mocked — no network) and prove:

  * the demo `ui-user` identity keeps its allow-all default (`[]`, D80b) — so the
    Layer-3 conformance scenarios that narrow FROM allow-all stay green;
  * a RESTRICTED non-admin identity mints its restricted allowlist (not `[]`);
  * an unknown identity falls back to the configured default scope;
  * the resolved identity is stamped as `user_name` and preserved (with the
    session binding) across the monotonic-narrow re-mint;
  * narrowing works WITHIN a restricted user's entitled base, and widening beyond
    that base is rejected (subset check reconciled with the entitled base).

The dev-only `X-Debug-User` identity override is honored only under
`UI_TEST_AFFORDANCES=1` (same gate as `POST /api/session/scope`), so a test can
drive a restricted identity without a real login. The Entra swap replaces the two
resolver bodies; these tests pin the plumbing that must survive that swap.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import ui.server as server
from fastapi.testclient import TestClient
from ui import entitlements


@pytest.fixture
def mint_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict]]:
    """Capture every `_mint_jwt(user_name, column_scope, session_id)` call."""
    monkeypatch.setenv("UI_TEST_AFFORDANCES", "1")
    calls: list[dict] = []

    async def _fake_mint(user_name: str, column_scope: list[str], session_id: str) -> str:
        calls.append(
            {"user_name": user_name, "column_scope": list(column_scope), "session_id": session_id}
        )
        return f"fake.jwt.{user_name}.{session_id}.{','.join(column_scope)}"

    monkeypatch.setattr(server, "_mint_jwt", _fake_mint)
    server._SESSIONS.clear()
    server._SESSION_SCOPES.clear()
    server._SESSION_USERS.clear()
    yield calls
    server._SESSIONS.clear()
    server._SESSION_SCOPES.clear()
    server._SESSION_USERS.clear()


@pytest.fixture
def client(mint_calls: list[dict]) -> TestClient:
    return TestClient(server.app)


# ---------------------------------------------------------------------------
# entitlements.py — the resolver seams in isolation
# ---------------------------------------------------------------------------


def test_resolve_column_scope_demo_user_is_allow_all() -> None:
    # The demo identity keeps allow-all (D80b) — conformance-suite invariant.
    assert entitlements.resolve_column_scope("ui-user") == []


def test_resolve_column_scope_restricted_user_is_restricted() -> None:
    scope = entitlements.resolve_column_scope("restricted-analyst")
    assert scope == ["dbpcm_warehouse.employee.EmployeeCode"]
    assert scope != []  # explicitly NOT allow-all


def test_resolve_column_scope_two_column_restricted_user() -> None:
    # A two-column entitlement, so a STRICT narrow (drop one column) is exercisable.
    assert entitlements.resolve_column_scope("restricted-hr") == [
        "dbpcm_warehouse.employee.EmployeeCode",
        "dbpcm_warehouse.employee.Department",
    ]


def test_resolve_column_scope_unknown_user_is_default() -> None:
    assert entitlements.resolve_column_scope("nobody-knows-me") == entitlements._DEFAULT_SCOPE


def test_unmapped_identity_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    # S2: an unmapped identity silently getting the default scope must be VISIBLE
    # (a typo'd default/dev user would otherwise get allow-all invisibly).
    with caplog.at_level("WARNING", logger="ui.entitlements"):
        entitlements.resolve_column_scope("typo-user-xyz")
    assert any(
        "no entitlement mapping" in r.message and "typo-user-xyz" in r.message
        for r in caplog.records
    )


def test_mapped_identity_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="ui.entitlements"):
        entitlements.resolve_column_scope("restricted-analyst")
    assert not caplog.records


def test_resolve_column_scope_returns_a_copy() -> None:
    # A caller mutating the result must not corrupt the entitlement table.
    scope = entitlements.resolve_column_scope("restricted-analyst")
    scope.append("dbpcm_warehouse.employee.AnnualSalary")
    assert entitlements.resolve_column_scope("restricted-analyst") == [
        "dbpcm_warehouse.employee.EmployeeCode"
    ]


# ---------------------------------------------------------------------------
# create_session — mints the caller's ENTITLED scope, not a hardcoded []
# ---------------------------------------------------------------------------


def test_default_identity_mints_allow_all(client: TestClient, mint_calls: list[dict]) -> None:
    """No identity header → the configured default (`ui-user`) → allow-all mint."""
    resp = client.post("/api/session")
    assert resp.status_code == 200

    assert len(mint_calls) == 1
    call = mint_calls[0]
    assert call["user_name"] == "ui-user"
    assert call["column_scope"] == []  # allow-all default preserved (D82/D80b)


def test_restricted_identity_mints_restricted_scope(
    client: TestClient, mint_calls: list[dict]
) -> None:
    """A restricted non-admin identity mints its RESTRICTED allowlist end-to-end."""
    resp = client.post("/api/session", headers={"X-Debug-User": "restricted-analyst"})
    assert resp.status_code == 200
    session_id = resp.json()["session_id"]

    assert len(mint_calls) == 1
    call = mint_calls[0]
    assert call["user_name"] == "restricted-analyst"
    assert call["column_scope"] == ["dbpcm_warehouse.employee.EmployeeCode"]
    assert call["column_scope"] != []  # a real restriction, not allow-all
    assert call["session_id"] == session_id  # still session-bound (Slice 1)
    # Server tracks the resolved identity for the re-mint path.
    assert server._SESSION_USERS[session_id] == "restricted-analyst"
    assert server._SESSION_SCOPES[session_id] == ["dbpcm_warehouse.employee.EmployeeCode"]


def test_identity_override_ignored_without_test_affordances(
    client: TestClient, mint_calls: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dev identity header is inert unless UI_TEST_AFFORDANCES=1 (prod safety)."""
    monkeypatch.delenv("UI_TEST_AFFORDANCES", raising=False)
    resp = client.post("/api/session", headers={"X-Debug-User": "restricted-analyst"})
    assert resp.status_code == 200
    # Header ignored → falls back to the configured default identity, allow-all.
    assert mint_calls[0]["user_name"] == "ui-user"
    assert mint_calls[0]["column_scope"] == []


# ---------------------------------------------------------------------------
# re-mint preserves identity + narrows WITHIN the entitled base
# ---------------------------------------------------------------------------


def test_remint_preserves_restricted_identity(client: TestClient, mint_calls: list[dict]) -> None:
    """The monotonic-narrow re-mint keeps the restricted identity and session id."""
    session_id = client.post(
        "/api/session", headers={"X-Debug-User": "restricted-analyst"}
    ).json()["session_id"]

    # Narrow WITHIN the entitled base (a subset of the entitled allowlist).
    resp = client.post(
        "/api/session/scope",
        json={"session_id": session_id, "column_scope": ["dbpcm_warehouse.employee.EmployeeCode"]},
    )
    assert resp.status_code == 200

    assert len(mint_calls) == 2
    create_call, remint_call = mint_calls
    # Identity + session binding preserved across the narrow.
    assert remint_call["user_name"] == "restricted-analyst" == create_call["user_name"]
    assert remint_call["session_id"] == session_id == create_call["session_id"]


def test_widen_beyond_entitled_base_is_rejected(
    client: TestClient, mint_calls: list[dict]
) -> None:
    """A restricted user cannot widen beyond their entitled base via the affordance."""
    session_id = client.post(
        "/api/session", headers={"X-Debug-User": "restricted-analyst"}
    ).json()["session_id"]

    # Attempt to add a column the user is NOT entitled to → a WIDEN → rejected.
    resp = client.post(
        "/api/session/scope",
        json={
            "session_id": session_id,
            "column_scope": [
                "dbpcm_warehouse.employee.EmployeeCode",
                "dbpcm_warehouse.employee.AnnualSalary",
            ],
        },
    )
    assert resp.status_code == 400
    assert "subset" in resp.json()["detail"]
    # No re-mint happened; the entitled base is unchanged.
    assert len(mint_calls) == 1
    assert server._SESSION_SCOPES[session_id] == ["dbpcm_warehouse.employee.EmployeeCode"]


def test_strict_narrow_within_two_column_base_then_no_rewiden(
    client: TestClient, mint_calls: list[dict]
) -> None:
    """A STRICT narrow WITHIN a restricted user's entitled base (drop one of two
    entitled columns) succeeds and re-mints; re-widening back toward the entitled
    base is then REJECTED.

    Pins the monotonic-narrow semantics HONESTLY: the subset check is against the
    CURRENT scope (set_session_scope), NOT the entitled base. So once narrowed
    from {EmployeeCode, Department} to {EmployeeCode}, restoring Department is a
    widen relative to the CURRENT {EmployeeCode} and is rejected — even though
    {EmployeeCode, Department} is ⊆ the entitled base. Narrowing is one-way."""
    session_id = client.post(
        "/api/session", headers={"X-Debug-User": "restricted-hr"}
    ).json()["session_id"]
    # Entitled base is the two-column allowlist.
    assert server._SESSION_SCOPES[session_id] == [
        "dbpcm_warehouse.employee.EmployeeCode",
        "dbpcm_warehouse.employee.Department",
    ]

    # STRICT narrow: drop Department → proper subset of the entitled base → 200 + re-mint.
    narrow = client.post(
        "/api/session/scope",
        json={"session_id": session_id, "column_scope": ["dbpcm_warehouse.employee.EmployeeCode"]},
    )
    assert narrow.status_code == 200
    assert len(mint_calls) == 2
    assert mint_calls[1]["column_scope"] == ["dbpcm_warehouse.employee.EmployeeCode"]
    assert mint_calls[1]["user_name"] == "restricted-hr"  # identity preserved
    assert server._SESSION_SCOPES[session_id] == ["dbpcm_warehouse.employee.EmployeeCode"]

    # Re-widen back toward the entitled base ({EmployeeCode, Department}) → rejected:
    # it is ⊆ the entitled base but ⊄ the CURRENT {EmployeeCode}. Narrow-against-current.
    rewiden = client.post(
        "/api/session/scope",
        json={
            "session_id": session_id,
            "column_scope": [
                "dbpcm_warehouse.employee.EmployeeCode",
                "dbpcm_warehouse.employee.Department",
            ],
        },
    )
    assert rewiden.status_code == 400
    assert "subset" in rewiden.json()["detail"]
    # No further re-mint; still at the narrowed single-column scope.
    assert len(mint_calls) == 2
    assert server._SESSION_SCOPES[session_id] == ["dbpcm_warehouse.employee.EmployeeCode"]
