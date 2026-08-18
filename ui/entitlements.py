"""ui/entitlements.py — per-user `column_scope` entitlement resolution.

The **Microsoft Entra swap seam** (auth-hardening Slice 2, Item 9). Today it is a STUB:
a configurable default identity plus a small in-code entitlement map. The two public
functions are the ONLY places `ui/server.py::create_session` resolves scope, so an Entra
swap is a one-line body change in each:

  * `resolve_caller_identity(request) -> str` — today `UI_DEFAULT_USER`, or a dev header
    override behind `UI_TEST_AFFORDANCES`. ENTRA SEAM: read the validated OIDC `sub`.
  * `resolve_column_scope(identity) -> list[str]` — today a stub map + a configured
    default. ENTRA SEAM: read the Entra entitlement claim/service.

Scope semantics (D80(b)): an empty `column_scope` (`[]`) means **ALLOW-ALL**; a non-empty
list is an allowlist of fully-qualified `database.table.column` strings. The MCP is the
live enforcement boundary (D57/D80) — this module only *supplies* the scope the BFF mints
into the token. The demo identity (`ui-user`) keeps its allow-all default; a RESTRICTED
per-user scope is an ADDED capability, not a changed default.
"""

from __future__ import annotations

import logging
import os

from fastapi import Request

logger = logging.getLogger(__name__)

# The identity the BFF attributes a session to when no real IdP is wired. Today
# this is the single demo user; keep it allow-all in `_ENTITLEMENTS` below.
_DEFAULT_IDENTITY = os.environ.get("UI_DEFAULT_USER", "ui-user")

# Optional dev-only header letting a test/harness drive a specific identity (and
# thus a specific entitled scope) WITHOUT a real login. Active only when
# `UI_TEST_AFFORDANCES=1`, mirroring the `POST /api/session/scope` gate — the
# production BFF never honors it, so it is not an escalation surface.
_DEV_IDENTITY_HEADER = "X-Debug-User"

# Stub entitlement map: identity -> `column_scope` ([] == allow-all, D80b). This
# is the readiness stand-in for a real entitlement source of truth.
#
# ENTRA SEAM: this whole map is replaced by an Entra entitlement claim/service
# read inside `resolve_column_scope`.
_ENTITLEMENTS: dict[str, list[str]] = {
    # Demo/UI identity — ALLOW-ALL so the Layer-3 conformance suite (which narrows
    # FROM allow-all) is unaffected. Do NOT restrict without updating those tests.
    "ui-user": [],
    # A NON-ADMIN example: entitled to the employee grain column only, so any query
    # touching another column (e.g. AnnualSalary) is denied by the MCP at runtime
    # (COLUMN_SCOPE_VIOLATION, D57). Proves per-user restriction end-to-end.
    "restricted-analyst": ["dbpcm_warehouse.employee.EmployeeCode"],
    # A TWO-column non-admin example, so a STRICT narrow WITHIN the entitled base
    # ({EmployeeCode, Department} → {EmployeeCode}) is exercisable — the base is a
    # proper superset of the narrowed scope. AnnualSalary stays out-of-entitlement.
    "restricted-hr": [
        "dbpcm_warehouse.employee.EmployeeCode",
        "dbpcm_warehouse.employee.Department",
    ],
}

# Scope handed to an identity ABSENT from `_ENTITLEMENTS`. Defaults to allow-all
# (`[]`) to preserve D82's interim posture for any un-mapped identity — this is
# NOT a regression (today every session is allow-all); the map adds restriction.
#
# ENTRA SEAM / TODO: under a real IdP this fallback should become deny-by-default
# (a non-allow-all sentinel or an outright reject) once every caller has an Entra
# entitlement. It stays allow-all here only because the sole live caller (ui-user)
# is explicitly mapped and the demo must keep working.
_DEFAULT_SCOPE: list[str] = []


def resolve_caller_identity(request: Request) -> str:
    """Resolve the caller's identity string for the current request.

    Today: the configured default identity (`UI_DEFAULT_USER`, default ``"ui-user"``),
    unless the dev-only `X-Debug-User` header is present AND `UI_TEST_AFFORDANCES=1`, in
    which case that header wins.

    ENTRA SEAM: replace this body with a read of the validated OIDC `sub` claim.
    """
    if os.environ.get("UI_TEST_AFFORDANCES") == "1":
        override = request.headers.get(_DEV_IDENTITY_HEADER)
        if override:
            return override
    return _DEFAULT_IDENTITY


def resolve_column_scope(identity: str) -> list[str]:
    """Resolve the `column_scope` entitled to *identity*.

    Today: a lookup in the stub `_ENTITLEMENTS` map, falling back to `_DEFAULT_SCOPE`
    for an unknown identity. Returns a fresh list (never the stored object) so callers
    cannot mutate the entitlement table. `[]` == allow-all (D80b).

    ENTRA SEAM: replace this body with a read of the Entra entitlement claim/service.
    """
    if identity not in _ENTITLEMENTS:
        # An unmapped identity silently gets `_DEFAULT_SCOPE` (allow-all today) —
        # a typo'd UI_DEFAULT_USER / X-Debug-User would otherwise get full access
        # INVISIBLY. Log it so the misconfig is visible while preserving the D82
        # posture. DELETE this line at the Entra swap (the map goes away).
        logger.warning("no entitlement mapping for identity %r; using default scope", identity)
    return list(_ENTITLEMENTS.get(identity, _DEFAULT_SCOPE))
