"""promotion/token_minter.py — the offline JWT minter for the S9 golden replay.

Mints the per-blueprint token the real `WarehouseProbe` runs under, scoped to EXACTLY that
blueprint's declared `uses` footprint (§1.3) in the `["db.table.column", …]` grammar the MCP
enforces (D57). It MUST carry the three warehouse TENANT claims: omitting them 403s every
offline replay before any tool runs, and the resulting `probe_unavailable` HOLD is
byte-identical to the one a healthy fail-closed design produces, so the gate was dark and
silent for the whole life of the feature.

Session-BOUND, a deliberate deviation from §1.3: `RealMCPClient` always sends
`X-Session-Id`, and an MCP running `require_sid_binding=true` rejects an unbound token that
carries one — so the probe mints a token bound to its OWN synthetic session id and sends
that same id, which is correct whichever way the flag is set. A SHORT TTL bounds exposure,
and ANY mint failure RAISES `TokenMintError`, which the probe lets propagate into a clean
`probe_unavailable` HOLD (D98), never a leak.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, fields
from typing import Protocol

import httpx


class TokenMintError(Exception):
    """The offline token mint was rejected or returned no token.

    RAISED so the probe fails closed to `probe_unavailable` — never a value, never a silent pass.
    """


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

    They map onto the ClickHouse custom settings the row policies read via `getSetting(...)`, and
    `validate_token` fails closed on a missing or blank one BEFORE any tool runs. The IdP does not
    supply them, so the CALLER must.

    WHICH TENANT DOES AN OFFLINE REPLAY RUN AS? Process-level config is right HERE and only here:
    the replay carries no user's authority — it is a deployment-level actor asking one structural
    question — so it needs A tenant with rows, not a particular user's. THE LIMIT: the blueprint is
    verified against THAT TENANT'S DATA only; `jti` is the ACTOR, so the replay sees one
    principal's employee set; and a misconfigured tenant does NOT turn the gate red, because row
    policies FILTER rather than error, so a wrong tenant yields zero rows and the grain teeth read
    `0 == 0`.

    VALIDATION is derived from the downstream operation: blank-after-strip is refused (blank config
    must not reproduce the silent hold this class exists to end), and a non-`str` or any Unicode
    `Cc` character is refused because the value goes on the wire as an HTTP query parameter. No
    charset allowlist beyond that — a tenant code is opaque. Whitespace is STRIPPED rather than
    rejected: stripping cannot turn tenant A into tenant B, while a trailing space would pass every
    presence check and then match zero rows. Raises `ValueError`, NOT `TokenMintError`, because
    every `TokenMintError` here is swallowed into a HOLD and this must fail loudly at WIRING time.
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
        """The `claims` object for the IdP mint body.

        Every value is a non-blank, control-character-free `str` BY CONSTRUCTION (frozen +
        validated), so no caller needs to re-check it.
        """
        return {
            "clientcode": self.clientcode,
            "proc_center": self.proc_center,
            "jti": self.jti,
        }


class TokenMinter(Protocol):
    """Mints a JWT scoped to `column_scope` for one offline golden replay, bound to
    `session_id` (the synthetic session the probe also sends as `X-Session-Id`)."""

    async def mint(self, column_scope: list[str], *, session_id: str) -> str: ...


class SuppliedTokenMinter:
    """A `TokenMinter` that returns a token SOMEONE ELSE ALREADY HOLDS, unchanged.

    For the reviewer-driven TRIAL only, and it exists because the trial surface must never mint
    authority. A reviewer pastes a token they already have; this hands it to the probe. Nothing
    here contacts the IdP, so no scope is requested, no `sid_hash` is bound, and no credential
    is created by the act of reviewing.

    ⚠ **`column_scope` IS IGNORED, AND THAT IS THE WHOLE TRADE.** A minted replay token carries
    EXACTLY the blueprint's declared `uses`, so the MCP's D57 teeth reject a query that reads
    outside its own footprint. A pasted token carries whatever its holder was given — usually
    broader — so during a trial those teeth do not bite, and a template reading an undeclared
    column can still return rows.

    WHAT STILL CATCHES THAT, and why the trade is acceptable rather than merely convenient: the
    column-scope contract is enforced at LANDING by
    `runtime/blueprint/compiler.py::_assert_template_reads_within_uses`, which resolves every
    table and column the template reads against a schema built only from `uses` and refuses the
    blueprint outright. That check is static, runs on the promotion path, and cannot be
    influenced by which token a reviewer pasted. So the footprint is still enforced before
    anything is served — the trial simply stops being a second place it is enforced.

    WHAT A TRIAL THEREFORE PROVES, stated precisely: this SQL executes and returns this shape,
    for a principal with this token's entitlements. It does NOT prove the declared footprint is
    honest, and the UI must not imply that it does.

    NEVER PERSISTED, NEVER LOGGED, NEVER ECHOED. The token lives for one request. `__repr__` is
    overridden because a dataclass-style repr in a traceback or a debug log is exactly how a
    bearer credential escapes.
    """

    __slots__ = ("_token",)

    def __init__(self, token: str) -> None:
        token = (token or "").strip()
        if not token:
            raise ValueError("a trial token cannot be blank")
        self._token = token

    async def mint(self, column_scope: list[str], *, session_id: str) -> str:
        return self._token

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return "SuppliedTokenMinter(<redacted>)"


class HttpTokenMinter:
    """Real `TokenMinter` over the token IdP's `POST /token` (Layer 2+).

    `token_endpoint` is the FULL mint URL, guarded by the static issuer API key, and the mint is
    session-BOUND: it stamps a `sid_hash` for the caller-supplied synthetic `session_id`.

    `tenant` is REQUIRED and keyword-only, not defaulted — a default would let a new wiring site
    re-open the original hole by simply not thinking about it, and the resulting breakage is
    invisible (a 403 the probe converts into the same HOLD a healthy run produces). `transport`
    is a test seam so the REQUEST BODY can be asserted at Layer 1, which matters here because
    the omitted-claims defect was a body-shape bug whose only downstream symptom looked healthy.

    Two parameters exist for the REQUEST path, both defaulted to the offline plane's posture:
    `ttl_seconds=None` OMITS the field and defers to the IdP's own configured default, and
    `allow_unscoped=True` disables the allow-all backstop, which a request-path caller whose
    entitlement legitimately resolves to allow-all has to state explicitly at its wiring site.
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


__all__ = [
    "HttpTokenMinter",
    "SuppliedTokenMinter",
    "TenantClaims",
    "TokenMintError",
    "TokenMinter",
]
