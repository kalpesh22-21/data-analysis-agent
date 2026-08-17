"""promotion/token_minter.py — the offline JWT minter for the S9 golden replay.

The real `WarehouseProbe` (`warehouse_probe.py`) runs the golden-replay grain probe
through the adopted MCP `runQuery` choke point, under a JWT minted PER BLUEPRINT and
scoped to EXACTLY that blueprint's declared `uses` footprint (S9-design §1.3). This
module is the thin transport that mints that token against the token IdP's
`POST /token` (`clickhouse-api/app/token_service.py`), mirroring `runtime/mcp/
scratch_client.py`'s httpx posture.

Load-bearing choices (S9-design §1.3, with the Slice-1 binding deviation noted below):
  * `column_scope = <blueprint.uses>` — the exact `["db.table.column", …]` grammar the
    MCP enforces (D57). The replay then reads ONLY within the declared footprint
    (D89 scope-honesty); the MCP's D57 teeth are the backstop if the mint is wrong.
  * **The three warehouse TENANT claims** (`TenantClaims`) — required by the MCP on
    EVERY call. Omitting them made every offline replay 403 before any tool ran, and
    the fail-closed `probe_unavailable` HOLD that resulted is byte-identical to the
    hold a healthy fail-closed design produces, so the gate was dark and silent for
    the whole life of the feature. See `TenantClaims` for who the replay runs AS.
  * **Session-BOUND (deviation from §1.3).** The design assumed a session-LESS mint
    (no `sid_hash`) so the probe could send an arbitrary `X-Session-Id`. But
    `RealMCPClient` ALWAYS sends the header, and an MCP running
    `require_sid_binding=true` rejects an unbound token the moment it sees one (403
    `SESSION_BINDING_MISMATCH`). So the probe mints a token BOUND to its OWN synthetic
    session id and sends that SAME id — the probe owns both (no hijack surface), scope
    stays `uses`, and the mint is correct whichever way the flag is set.
    NOTE, because an earlier version of this docstring asserted it as fact: the flag is
    CONFIG and defaults to FALSE (`clickhouse-api/app/config.py::require_sid_binding`),
    and the l2 dev stack does not set it — binding is currently NOT enforced there.
    Verified 2026-08-11: `test_mcp_scope_live.py::
    test_unbound_token_with_session_header_rejected` fails 200-not-403 against l2-mcp.
  * A SHORT TTL bounds token exposure (the probe fires once and discards the token).
  * ANY mint failure RAISES (`TokenMintError`) — the probe lets it propagate so
    `golden_replay` degrades to a clean `probe_unavailable` HOLD (D98), never a leak.

Two implementations, one interface (the `FakeMCPClient` pattern): the real
`HttpTokenMinter` here (Layer 2), and a fake in the S9 test helpers (Layer 1).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, fields
from typing import Protocol

import httpx


class TokenMintError(Exception):
    """The offline token mint was rejected or returned no token. RAISED so the probe
    fails closed to `probe_unavailable` (never a value, never a silent pass)."""


# Claim field -> the ONE env var that sets it. Both readers of these claims
# (`RuntimeSettings.tenant_*` and `ui/server.py`'s TENANT_* block) read the SAME names
# on purpose, so a blank value has exactly one knob to name. Naming all three in every
# message made the reader diff the message against their own config to find out which
# one they missed — the error knows which field is blank, so it says.
#
# The mapping is hand-written because `clientcode -> TENANT_CLIENT_CODE` is not a
# mechanical transform, and it is read with `.get(..., <generic>)`: a future claim field
# whose env var nobody added here must degrade to the old vaguer sentence, never to a
# `KeyError` raised from inside a validator whose whole job is to fail legibly.
_CLAIM_ENV_VARS = {
    "clientcode": "TENANT_CLIENT_CODE",
    "proc_center": "TENANT_PROC_CENTER",
    "jti": "TENANT_JTI",
}
_CLAIM_ENV_FALLBACK = "TENANT_CLIENT_CODE / TENANT_PROC_CENTER / TENANT_JTI"


@dataclass(frozen=True)
class TenantClaims:
    """The three warehouse tenant claims the MCP requires on EVERY call.

    The MCP's `app/config.py::CLICKHOUSE_TENANT_SETTINGS` maps these claim names onto
    the `paycom_client_code` / `paycom_proc_center` / `paycom_authenticated_user`
    ClickHouse custom settings that the row policies read via `getSetting(...)`
    (`docker/clickhouse-init/hr-4tables-snake-migration.sql`). `app/auth_jwt.py::
    validate_token` fails closed on a missing or blank one — `403
    MISSING_TENANT_CLAIM`, BEFORE any tool runs. The IdP does not supply them:
    `app/token_service.py::_mint` stamps only sub/iss/aud/exp/user_name/column_scope/
    sid_hash, so the CALLER must. `ui/server.py`'s `TENANT_*` block is the request-path
    twin of this type; this is the offline plane's, and the two read the SAME env vars
    so an operator configures one thing.

    WHICH TENANT DOES AN OFFLINE REPLAY RUN AS?
    -------------------------------------------
    The UI resolves this per caller identity, and its comment rightly warns that
    minting tenant claims from process-level config "would let one deployment issue
    tokens for another tenant". That warning is about a token carrying a USER'S
    authority. The golden replay carries none: there is no caller, no request, and no
    user to impersonate — it is a deployment-level actor asking one structural
    question ("does this frozen template still parse, still execute, and still
    preserve its declared grain?"). It needs A tenant with rows, not a PARTICULAR
    user's tenant. So process-level config is the right answer HERE, and only here.

    THE LIMIT OF THAT REASONING, which a reader of a green replay must know:
      * A blueprint verified against one tenant's data is verified against THAT
        TENANT'S DATA ONLY. If tenants have materially different shapes (a column
        populated for one and NULL for another, a grain unique for one and fanned out
        for another), a green replay is weaker evidence than it looks.
      * `jti` is not a bystander. It is the ACTOR: the employee/payroll row policies
        gate on `user_employee_access.jti`, so the replay sees exactly the employee
        set of the ONE configured principal — not the union every future caller sees.
      * A misconfigured tenant does not turn the gate red. Row policies FILTER, they
        do not error, so a wrong tenant yields zero rows; the grain teeth then read
        `0 == 0` and pass. Replay is a STRUCTURE oracle (D98) and always was — the
        sampled synthetic slot values usually match nothing anyway — so "it passed"
        never meant "rows exist". Read it as: the template still parses, the schema
        still has these columns, and the output signature is unchanged.

    VALIDATION is derived from the downstream operation, not from an intent:
      * `auth_jwt.validate_token` rejects a value that is `None` or blank-after-strip
        → we refuse to build one. Blank config must not be allowed to reproduce, from
        the inside, the exact silent hold this class exists to end.
      * `clickhouse_client.tenant_settings` does `str(value)` and hands the result to
        clickhouse-connect, which puts it on the wire as an HTTP query parameter → a
        non-`str` (an int, a `None` from an unset settings field) and any character in
        Unicode category `Cc` (C0, C1 and DEL) are refused. The category is the
        predicate, not a hand-written range — see `__post_init__`. No charset allowlist
        beyond that: a tenant code is an opaque string compared for equality by a row
        policy, and inventing a shape for it would be a guard written from an intent
        rather than from the operation.
      * Surrounding whitespace is STRIPPED, not rejected. Stripping cannot turn tenant
        A into tenant B, and `"CLIENT_A "` would otherwise pass the IdP and the MCP
        presence check and then match zero rows under `client_code = getSetting(...)`
        — a silently-empty replay, which is the failure mode of this whole area.

    The claim NAMES are fixed literals, not config: they are the MCP's mapping keys,
    and the IdP rejects (422) any `claims` entry colliding with a service-controlled
    claim. A configurable name buys nothing and can only mis-map.

    Raises `ValueError` (NOT `TokenMintError`) on bad config, deliberately: every
    `TokenMintError` on this path is swallowed into a `probe_unavailable` HOLD. This
    must be constructed at WIRING time, where a bad value is a loud startup failure
    rather than a promotion gate that quietly never runs.
    """

    clientcode: str
    proc_center: str
    jti: str

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if not isinstance(value, str):
                raise ValueError(
                    f"tenant claim {field.name!r} must be a string, got "
                    f"{type(value).__name__}"
                )
            stripped = value.strip()
            if not stripped:
                raise ValueError(
                    f"tenant claim {field.name!r} is blank — the MCP would reject "
                    "every replay 403 MISSING_TENANT_CLAIM and the promotion gate "
                    "would hold silently (set "
                    f"{_CLAIM_ENV_VARS.get(field.name, _CLAIM_ENV_FALLBACK)})"
                )
            # `unicodedata.category(ch) == "Cc"` IS the C0+C1+DEL set, exactly — the
            # set this class claims to refuse. The predicate here used to be
            # `ch < " " or ch == "\x7f"`, which is C0+DEL only, so a C1 character
            # constructed fine while the docstring said otherwise. Named categories
            # over hand-rolled ranges: the range drifted from its own description
            # silently, and a validation whose selling point is "derived from the
            # operation" cannot afford a predicate its prose disagrees with.
            if any(unicodedata.category(ch) == "Cc" for ch in stripped):
                raise ValueError(
                    f"tenant claim {field.name!r} contains a control character"
                )
            object.__setattr__(self, field.name, stripped)

    def as_claims(self) -> dict[str, str]:
        """The `claims` object for the IdP mint body. Every value is a non-blank,
        control-character-free `str` BY CONSTRUCTION (frozen + validated), so no
        caller needs to re-check it."""
        return {
            "clientcode": self.clientcode,
            "proc_center": self.proc_center,
            "jti": self.jti,
        }


class TokenMinter(Protocol):
    """Mints a JWT scoped to `column_scope` for one offline golden replay, bound to
    `session_id` (the synthetic session the probe also sends as `X-Session-Id`)."""

    async def mint(self, column_scope: list[str], *, session_id: str) -> str: ...


class HttpTokenMinter:
    """Real `TokenMinter` over the token IdP's `POST /token` (Layer 2+).

    `token_endpoint` is the FULL mint URL (e.g. `http://token:8000/token`), guarded
    by the static issuer API key. The mint is session-BOUND (§1.3 deviation): it
    stamps a `sid_hash` for the caller-supplied synthetic `session_id`, because
    `RealMCPClient` always sends `X-Session-Id` and an MCP with `require_sid_binding`
    on rejects an unbound token that carries one (the flag defaults OFF — see the
    module docstring). Short-TTL by design (§1.3).

    `tenant` is REQUIRED and keyword-only, not defaulted. A default would let a new
    wiring site re-open the original hole by simply not thinking about it, and the
    resulting breakage is invisible (a 403 the probe converts into the same
    `probe_unavailable` HOLD a healthy fail-closed run produces). Making it a
    `TypeError` at construction means every future mint site has to answer the
    "which tenant?" question out loud.

    `transport` is a test seam (same shape as `runtime/model/embedding_client.py::
    HttpEmbeddingClient`) so the REQUEST BODY this minter puts on the wire can be
    asserted at Layer 1. That matters more here than usual: the omitted-claims defect
    was a body-shape bug, and its only symptom downstream was a hold that looks
    identical to a healthy one.

    TWO PARAMETERS EXIST FOR THE REQUEST PATH, both defaulted to the offline plane's
    posture so no learning-plane wiring site changes by adopting them:
      * `ttl_seconds=None` OMITS the field, deferring to the IdP's own configured
        default (`token_service.settings.token_ttl_seconds`). The short-TTL default
        (300) is right for a probe that fires once and discards the token; a UI
        session's JWT has to outlive a conversation, and hardcoding the IdP's default
        here would silently pin a value the deployment can change.
      * `allow_unscoped=True` disables the allow-all backstop below. The backstop is
        about the LEARNING plane (see `mint`); a request-path caller whose entitlement
        legitimately resolves to allow-all (D80b: `[]` == no column restriction) has
        to say so explicitly, at its own wiring site, in one word a reviewer can grep.
    """

    def __init__(
        self,
        token_endpoint: str,
        api_key: str,
        *,
        tenant: TenantClaims,
        user_name: str = "learning-scheduler",
        ttl_seconds: int | None = 300,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        allow_unscoped: bool = False,
    ) -> None:
        self._endpoint = token_endpoint
        self._api_key = api_key
        self._tenant = tenant
        self._user_name = user_name
        self._ttl_seconds = ttl_seconds
        self._timeout = timeout
        self._transport = transport
        self._allow_unscoped = allow_unscoped

    async def mint(self, column_scope: list[str], *, session_id: str) -> str:
        # BACKSTOP (D57/D80b): an EMPTY column_scope mints an ALLOW-ALL (unrestricted)
        # token at the IdP + MCP — the learning plane must NEVER do that. Refuse to
        # mint rather than run a model/extraction-derived replay SQL against live
        # ClickHouse with no scope. golden_replay short-circuits on empty `uses` before
        # reaching here; this is the defense-in-depth second gate.
        # `allow_unscoped=True` is the request path's explicit opt-out: there, allow-all
        # is a RESOLVED ENTITLEMENT (`ui/entitlements.py`), not an absent scope.
        if not column_scope and not self._allow_unscoped:
            raise TokenMintError(
                "refusing to mint an allow-all token (empty column_scope)"
            )
        # `session_id` binds the token (sid_hash) to the synthetic session the probe
        # also sends as X-Session-Id — required by the live MCP's require_sid_binding
        # (§1.3 deviation). `column_scope` is EXACTLY the blueprint's `uses` footprint.
        body: dict[str, object] = {
            "user_name": self._user_name,
            "column_scope": list(column_scope),
            "session_id": session_id,
            # The three warehouse tenant claims the MCP requires on EVERY call —
            # without them the replay is rejected 403 MISSING_TENANT_CLAIM before any
            # tool runs and the promotion gate holds silently. See `TenantClaims`.
            "claims": self._tenant.as_claims(),
        }
        if self._ttl_seconds is not None:
            # Omitted (not sent as null) when unset: the IdP treats a missing
            # `ttl_seconds` as "use my configured default", and an absent key is the
            # body every hand-rolled mint site posted before they moved here.
            body["ttl_seconds"] = self._ttl_seconds
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(
                    self._endpoint,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=body,
                )
        except httpx.HTTPError as exc:  # transport failure — token IdP down/unreachable
            raise TokenMintError(f"token mint transport error: {exc}") from exc
        if resp.status_code >= 400:
            # A 401 (bad issuer key), 422 (bad scope), or 5xx — never leak the body.
            raise TokenMintError(f"token mint returned HTTP {resp.status_code}")
        try:
            body_json = resp.json()
        except ValueError as exc:
            raise TokenMintError("token mint returned a non-JSON body") from exc
        token = body_json.get("access_token") if isinstance(body_json, dict) else None
        if not isinstance(token, str) or not token:
            raise TokenMintError("token mint returned no access_token")
        return token


__all__ = ["HttpTokenMinter", "TenantClaims", "TokenMintError", "TokenMinter"]
