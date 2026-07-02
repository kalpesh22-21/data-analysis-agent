# Auth Hardening — Runtime-Readiness Design (Item 9)

**Status:** Slices 1+2 BUILT (Session 14, **D92**) — the `X-Session-Id`↔`sid_hash` binding + the
no-session scratch fail-closed + the reserved-claim guard are built cross-repo, reviewed, and
live-proven against the enforcing MCP (a cross-session scratch read is rejected). One deviation from
this doc: the omit-header vector is closed via **fix (b)** (extractor fail-closed on `scratch.*` with
no bound session), not fix (a) (require the header), because (a) forbids the legitimate no-header
no-session mode. Slice 2 (per-user `column_scope` entitlement mint) is also built — a stub identity→scope map behind
two `# ENTRA SEAM` functions in `ui/entitlements.py`; demo `ui-user` stays allow-all, restricted users
are enforced live. Only the literal Entra OIDC wiring stays deferred. Companion to [DECISIONS.md](DECISIONS.md)
(D5 / D57 / D64 / D79 / D80 / D81 / D82). Scope: build the runtime/MCP-side
**readiness** for real per-user entitlements and session-hijack protection now;
defer only the literal Microsoft Entra OIDC wiring. This is a *readiness brick*,
not a full IdP integration.

Two independent hardening legs:

- **(a)** Real **per-user `column_scope`** — make the BFF mint per-user-configurable
  via a stub entitlement map, so the Entra swap is a source-line change at a
  named seam.
- **(b)** **`X-Session-Id` → JWT binding** — the session id travels unsigned
  today (D81), so a JWT-authenticated caller can send *another* user's
  `session_id` and read their scratch/PII (D64). Bind it cryptographically.

---

## 1. What exists today (read-confirmed)

- **BFF mint** (`ui/server.py`): `create_session` mints one JWT **per
  `session_id`** via `_mint_jwt([])` → `POST {TOKEN_SERVICE_URL}/token` with a
  hardcoded `user_name="ui-user"` and `column_scope=[]` (allow-all, D82 interim).
  The browser only ever receives `{"session_id": ...}`; the BFF is the sole JWT
  holder (D5/D82). `_SESSION_SCOPES` already tracks per-session scope for the
  monotonic-narrow test affordance (which explicitly anticipates Item 9).
- **Token service** (`clickhouse-api/app/token_service.py`): `_mint` stamps
  `sub`, `iss`, `aud`, `iat/nbf/exp`, `user_name`, `column_scope` (JSON string),
  signs RS256 with a mounted private key, publishes JWKS. `session_id` is
  **not** a claim (D81). `POST /token` is guarded by a static issuer API key.
- **Runtime** (`src/data_agent/runtime/`): `app.py::_extract_credentials` reads
  `Authorization` + `X-Session-Id`, builds `RuntimeCredentials`. `real_client.py`
  forwards **both** headers verbatim to the MCP and nowhere else (D5). The
  runtime is a pure **pass-through** for these two headers — it derives
  `column_scope` locally only for the D44 replay-filter (defense-in-depth, not a
  live gate).
- **MCP enforcement** (`clickhouse-api/app/mcp_server.py::JWTAuthMiddleware`):
  `validate_token` (`app/auth_jwt.py`) verifies signature/iss/aud/exp against
  JWKS and **requires a non-empty `column_scope` claim** (fail-closed, 403).
  The `X-Session-Id` header is read by `_session_id_from_scope` into
  `current_session_id` **without any binding check** — this is the gap. Scratch
  isolation (D64) fires in `provenance.py::_validate_scratch_name`: any
  `scratch.*` table must match the `s_<session_id>_` prefix. Column scope is
  enforced by the same parse (D57) — proven live this session
  (`tests/integration/test_mcp_scope_live.py`, a real `COLUMN_SCOPE_VIOLATION`).

**The hijack (b):** because `X-Session-Id` is unsigned and unbound (D81 itself
flags this), a caller holding userA's valid JWT can set
`X-Session-Id: <userB-session>`; the prefix check *succeeds* against userB's
session and userA reads userB's scratch tables (the most sensitive uploaded
PII). Nothing ties the header to the JWT `sub`.

---

## 2. One-line answers (Q1–Q6)

- **Q1 (HMAC contract):** Put a **`sid_hash` claim** (`b64url(sha256(session_id))`)
  **inside the JWT**; the MCP checks `sha256(X-Session-Id) == claims.sid_hash`
  after it has verified the JWT signature. **No new shared secret** — the JWT
  signature already authenticates the claim, and the claim rides in a token that
  also carries `sub`, so the session↔sub binding is transitive. Rollout: make it
  **required when `X-Session-Id` is present**, behind a short-lived
  `require_sid_binding` flag so the two repos can land mint-then-enforce.
- **Q2 (per-user mint):** A stub `dict[str, list[str]]` entitlement map + a
  configurable default, plus a one-function **identity resolver**, both in a
  small BFF module (`ui/entitlements.py`). `create_session` resolves
  `identity → column_scope` and mints with them. The two functions are the Entra
  seams.
- **Q3 (cross-repo):** `clickhouse-api` owns the *binding* (token_service stamps
  `sid_hash`; MCP verifies) and already owns *scope enforcement*. `data-agent`
  owns the *per-user resolution* (BFF) and passes `session_id` into the mint.
  The runtime needs **no change**. clickhouse-api tests prove the security legs;
  data-agent tests prove the per-user resolution + end-to-end denial.
- **Q4 (invariants):** After (b), userA cannot use userB's `session_id`
  (sid_hash mismatch → reject *before* the extractor); a session header without a
  matching `sid_hash` is rejected (fail-closed); the D64 scratch gate is now
  cryptographically bound, not header-trusted. See §6.
- **Q5 (slicing):** **Slice 1** = the `sid_hash` binding (security-critical,
  cross-repo) — must land before Item 8 Slice 1 (scratch-upload). **Slice 2** =
  per-user mint readiness (BFF-mostly). Slice 1 first (the mint gains
  `session_id` anyway).
- **Q6 (deferred):** Only the literal Entra OIDC wiring — the browser login/
  auth-code flow and the two stub seams (`resolve_caller_identity`,
  `resolve_column_scope`). Plus one flagged decision: sid_hash-in-JWT implies a
  session-token minter survives the Entra swap (§7).

---

## 3. Recommendation — `sid_hash` JWT claim over a separate HMAC header

**Chosen: (b) the `sid_hash` JWT claim.**

| | `sid_hash` JWT claim (chosen) | Separate `X-Session-Id-Sig` HMAC header |
|---|---|---|
| Shared secret | **None** — reuses the existing JWKS trust | New secret BFF/token-service ↔ MCP, provisioned + rotated (env/helm) |
| Verification paths | **One** — the claim is verified with the token | Two — JWT signature *and* the HMAC |
| Binding to `sub` | Transitive (claim is inside sub's signed token) | Explicit `HMAC(secret, session_id‖sub)` |
| Forgery cost | The token_service private key | The shared secret |
| Fit to code today | BFF already mints one JWT **per session** | Adds a new header + secret plumbing |

**Rationale:**
1. **No new secret to provision or rotate.** The token_service already signs and
   the MCP already trusts its JWKS; a claim inside that token inherits the same
   integrity guarantee for free. A separate HMAC introduces a *second* secret,
   its own rotation story, and a second verification path that can drift.
2. **`sha256(session_id)` needs no keying.** `session_id` is a uuid4 (~122 bits
   of entropy), so a plain hash is not brute-forceable and leaks nothing
   exploitable; keying by `sub` is redundant because the JWT signature already
   binds the claim to `sub`. An attacker cannot move the claim onto another JWT
   without the private key.
3. **It matches the code as-built.** `create_session` already mints one JWT per
   `session_id`, so the minter already knows the `session_id` at mint time — the
   claim costs one extra line. The D81/D82 "one identity token serves many
   sessions" property is *aspirational, not implemented*, so binding a token to a
   single session gives up nothing today.

**The cost, stated plainly (Q6 / §7):** this couples the *minter* to the session
lifecycle. Under Entra, Entra cannot stamp `sid_hash` (it does not know
`session_id`), so a **session-token minter must survive** the Entra swap — the
token_service becomes a thin session-token exchange (Entra = identity/entitlement
source; token_service = session-binding minter), rather than D82's "pure config
swap." This is the *honest* seam: some component must bind the session, and
Entra structurally cannot. If keeping token_service disposable is later judged
more valuable than avoiding a shared secret, the separate-HMAC option (BFF-signed,
since the BFF always exists and always knows `session_id`) is the fallback — but
that is an Entra-time re-decision, not a readiness-brick concern.

**What is hashed / where it lives / how carried:** `sid_hash =
base64url(sha256(session_id_utf8))`, an ordinary registered-style private claim
in the JWT body, carried by the existing `Authorization: Bearer` token. No new
header. `X-Session-Id` continues to carry the raw `session_id` (unchanged);
the MCP recomputes the hash and compares.

---

## 4. Cross-repo contract

### 4.1 `clickhouse-api` (the binding + enforcement)

- **`app/token_service.py`**
  - `TokenRequest` gains `session_id: Optional[str]`.
  - `_mint(...)` gains `session_id: Optional[str]`; when present, adds
    `claims["sid_hash"] = base64url(sha256(session_id.encode()))`. When absent,
    no `sid_hash` claim (preserves non-session callers: tests, examples, stdio).
- **`app/mcp_server.py::JWTAuthMiddleware`** (after `validate_token` succeeds,
  where both `principal.claims` and the session header are in scope):
  - If `X-Session-Id` present:
    - require `principal.claims.get("sid_hash")` present, and
    - require `base64url(sha256(header)) == claims["sid_hash"]` (constant-time
      compare),
    - else reject **403** `code="SESSION_BINDING_MISMATCH"` (fail-closed,
      consistent with D63/D64), via the existing `_send_json_response` /
      `_www_authenticate("insufficient_scope")` path.
  - Gated by `settings.require_sid_binding` (default **true** in deploy;
    togglable for the mint-then-enforce transition, then remove).
  - `Principal` already carries `claims`, so no `principal.py` change.
- **No `auth_jwt.py::validate_token` change** — binding is a *transport+session*
  concern, kept in the middleware next to `_session_id_from_scope`, not in the
  pure token validator.

### 4.2 `data-agent` (per-user resolution + passing `session_id` to mint)

- **`ui/server.py`**
  - `_mint_jwt(user_name, column_scope, session_id)` — now passes `user_name`,
    `column_scope`, **and** `session_id` to `POST /token`.
  - `create_session`: `identity = resolve_caller_identity(request)`;
    `scope = resolve_column_scope(identity)`; mint with them + the new
    `session_id`. Track `session_id → identity` (extend `_SESSION_SCOPES` into a
    small per-session record, or add `_SESSION_USERS`) so the monotonic-narrow
    re-mint **preserves identity and re-binds the same `session_id`**.
  - `set_session_scope` (test affordance): re-mint must pass the **same**
    `user_name` and **same** `session_id` (so `sid_hash` stays correct) — only
    `column_scope` narrows.
- **`ui/entitlements.py`** (new, the Entra seam):
  - `resolve_caller_identity(request) -> str` — today returns a configured
    default (`UI_DEFAULT_USER`) or a dev header behind `UI_TEST_AFFORDANCES`;
    tomorrow reads the OIDC `sub`.
  - `resolve_column_scope(user) -> list[str]` — today a stub
    `dict[str, list[str]]` + default; tomorrow an Entra entitlement claim/service.
- **Runtime: no change.** `real_client.py` / `app.py` already forward
  `Authorization` + `X-Session-Id` verbatim; `sid_hash` is never model-visible
  and the runtime never inspects it (D5 preserved).

### 4.3 Contract summary (the wire)

```
BFF  --POST /token { user_name, column_scope, session_id }-->  token_service
     <--------------- JWT { sub, user_name, column_scope, sid_hash } ----------
BFF  --Authorization: Bearer <JWT>  +  X-Session-Id: <session_id>-->  runtime
runtime  --(same two headers, verbatim)-->  MCP
MCP: verify JWT (JWKS) -> require column_scope -> require sha256(X-Session-Id)==sid_hash
     -> D57 column gate + D64 scratch gate
```

---

## 5. Slice plan

### Slice 1 — `X-Session-Id` ↔ `sid_hash` binding (security-critical, cross-repo)

**Why first:** closes the D81 hijack before Item 8 Slice 1 ships scratch-upload
(the feature the binding protects). Small, well-bounded, both repos.

- clickhouse-api: token_service stamps `sid_hash`; middleware verifies; add
  `require_sid_binding` flag. data-agent: BFF passes `session_id` into the mint;
  re-mint preserves `session_id`.
- **Landing order:** ship mint-side (`sid_hash` stamped) with
  `require_sid_binding=false`, confirm tokens carry the claim, then flip to
  `true` and update the remaining minting paths. One team / one session, so this
  is a same-PR-pair coordination, not a long grace window.
- **Tests (clickhouse-api owns the proof):**
  - token_service: a mint *with* `session_id` carries `sid_hash =
    b64url(sha256(session_id))`; *without* carries none.
  - middleware unit/integration: header matching `sid_hash` → allowed; mismatched
    → 403 `SESSION_BINDING_MISMATCH`; session header + no `sid_hash` claim +
    `require_sid_binding=true` → 403; `require_sid_binding=false` → passes (legacy).
  - live (L2, extend `test_mcp_scope_live.py`): userA JWT + userB session_id →
    rejected *before* the scratch extractor; userA JWT + userA session_id →
    scratch access proceeds.
- **Tests (data-agent):** `create_session` mint call includes `session_id`; the
  monotonic-narrow re-mint keeps the same `session_id` (sid_hash stable).

### Slice 2 — Per-user mint readiness (BFF-mostly)

**Depends on Slice 1** (mint already threads `session_id`).

- data-agent: `ui/entitlements.py` (stub map + identity resolver);
  `create_session` resolves per-user scope; track `session_id → identity`.
- **Tests:**
  - unit (data-agent): a non-admin user resolves to a restrictive scope; unknown
    user → configured default; identity preserved across re-mint.
  - end-to-end (L2, reuse the `test_mcp_scope_live.py` scope-denial pattern):
    BFF mints a restricted non-admin token → runtime → MCP → an out-of-scope
    column query returns `COLUMN_SCOPE_VIOLATION`. This is the (a) proof.

*Could Slice 1+2 combine?* They are both small, but Slice 1 is
security-critical and cross-repo while Slice 2 is a BFF config seam. Keeping them
separate lets Slice 1 land, get reviewed, and unblock Item 8 independently.
Recommend separate; Slice 1 first.

---

## 6. Threat model + hard invariants (must-test)

After Slice 1 (b):

1. **No cross-session read.** userA's valid JWT + `X-Session-Id=<userB-session>`
   → `sid_hash` mismatch → 403 *before* the D64 extractor runs. (Even though
   userB's prefix would match, the request never reaches the parse.)
2. **No unbound session access.** A JWT lacking `sid_hash` + any `X-Session-Id`,
   with `require_sid_binding=true`, is rejected (fail-closed, D63/D64 posture).
3. **`sid_hash` is unforgeable across tokens.** It rides inside the JWT
   signature; binding it to a different `sub` requires the token_service private
   key.
4. **Binding is orthogonal to scope.** An allow-all token with a *valid*
   `sid_hash` still works; a restricted token with a valid `sid_hash` still
   enforces column scope (D57) — the two gates stack.
5. **Re-mint preserves binding.** The monotonic-narrow re-mint keeps the same
   `session_id` (sid_hash stable) and same `sub`; it never re-widens scope and
   never rebinds the session to a different id.
6. **Runtime stays a pass-through.** `sid_hash` is never placed in any
   model-visible structure; the runtime never inspects or derives it (D5).

After Slice 2 (a):

7. **Per-user scope is honored end-to-end.** A BFF-minted non-admin restrictive
   scope produces a real `COLUMN_SCOPE_VIOLATION` on an out-of-scope query — the
   MCP is the live boundary (D57/D80), the BFF only supplies the scope.

---

## 7. Deferred + open questions

**Deferred (explicitly out of this brick):**
- The **literal Entra OIDC wiring**: the browser login / auth-code flow and
  pointing at Entra. The seams are `resolve_caller_identity` (stub default →
  OIDC `sub`) and `resolve_column_scope` (stub map → Entra entitlement
  claim/service). Both are single-function swaps by construction.
- **Real entitlement source of truth** and **login UX** — stub only for now.
- **Secret rotation / key rotation** beyond what the token_service JWKS already
  provides (the chosen design adds *no* new secret, so nothing new to rotate).

**Open questions (to record as an ADR when Slice 1 lands):**
- **OQ-1 (flag D82 refinement).** sid_hash-in-JWT means a **session-token minter
  survives** the Entra swap (Entra cannot bind sessions). D82's "Entra swap =
  config change" should be amended: token_service (or a successor) becomes a
  session-token exchange in front of Entra. Decide/record at Entra time; the
  fallback if disposability is preferred is the separate BFF-signed HMAC header.
- **OQ-2.** `require_sid_binding` is a transition flag — schedule its removal
  (default-true → remove) once all minting paths stamp `sid_hash`.
- **OQ-3.** Should `sid_hash` also cover `sub` explicitly
  (`sha256(sub‖session_id)`)? Not needed (§3 rationale 2), but note it if a
  future model ever mints tokens outside the BFF path.

**Bigger-than-a-brick flags (resist now):** do **not** build a real entitlement
service, a real login, or token-exchange plumbing in this brick — the stub map +
default identity is the whole readiness surface. The only genuine architecture
decision surfaced (OQ-1) is *recorded and deferred*, not built.
