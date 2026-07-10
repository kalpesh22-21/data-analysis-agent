"""Session-scoped fixture that boots the REAL two-process stack (the
scripted `scripts/run_ui_runtime.py` runtime on :8000, and the `ui/server.py`
BFF on :3000) as subprocesses, for the Layer-3 spec-conformance suite
(`test_conformance.py`) to drive with Playwright's own `page` fixture
(registered by the `pytest-playwright` plugin — nothing to define here).

Only active when `RUN_E2E` is set (see `test_conformance.py`'s module-level
`pytestmark` skip guard) — this fixture is session-scoped so the two
processes are started once for the whole `tests/e2e` run, not once per test.

Requires the `l2-token` container (docker-compose.integration.yml) already
running on :19000 — `ui/server.py`'s `/api/session` mints a real JWT from it
server-side, and `scripts/run_ui_runtime.py` verifies that JWT against the
SAME container's real JWKS endpoint (see that script's module docstring) —
so no auth is bypassed/monkeypatched here either.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_URL = "http://localhost:8000"
_BFF_URL = "http://localhost:3000"
_READY_TIMEOUT_SECONDS = 30.0
_READY_POLL_INTERVAL_SECONDS = 0.5

# --- Flow-2 (review inbox) stack: FRESH ports so it never collides with the
# chat stack above (both can be session-scoped in the same `tests/e2e` run).
_INBOX_RUNTIME_URL = "http://localhost:8001"
_INBOX_SERVICE_URL = "http://localhost:8101"
_INBOX_BFF_URL = "http://localhost:3001"
# The shared reviewer secret the BFF attaches server-side and the inbox service
# compares constant-time (`X-Reviewer-Token` ↔ `REVIEWER_TOKEN`); the browser
# never sees it, exactly like production.
_REVIEWER_TOKEN = "e2e-reviewer-secret"


def _wait_until_ready(url: str, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=2.0)
            if response.status_code < 500:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(_READY_POLL_INTERVAL_SECONDS)
    raise RuntimeError(f"{url} did not become ready within {timeout_seconds}s (last error: {last_error})")


@pytest.fixture(scope="session")
def running_stack() -> Iterator[None]:
    """Launch the scripted runtime (:8000) + BFF (:3000) as subprocesses,
    wait for both to answer, yield, then terminate both cleanly."""
    runtime_log = open(_REPO_ROOT / "tests" / "e2e" / ".runtime.stack.log", "w")
    bff_log = open(_REPO_ROOT / "tests" / "e2e" / ".bff.stack.log", "w")

    # Slice-2 test affordances (all env-gated; the store stays IN-MEMORY here —
    # only the isolated restart module opts into Couchbase). DEMO_TEST_SPANS=1
    # installs the in-memory span exporter + `GET /_test/spans` (D25); it adds a
    # span processor + a route but changes NO turn behavior, so the 9 pre-existing
    # scenarios stay green. UI_TEST_AFFORDANCES=1 (BFF, below) exposes the inert
    # `POST /api/session/scope` (D44) — never called by the other scenarios.
    runtime_env = dict(os.environ)
    runtime_env["DEMO_TEST_SPANS"] = "1"
    # Hermetic to the operator's debug flag: `.env` may set OTLP_DISABLE_REDACTION=1
    # (unredacted Phoenix traces + full tool-call logging). That un-redacts span
    # attributes, so the D25 PII-clean conformance scenario would see raw SQL
    # literals. FORCE the DEFAULT redacted posture here so the suite tests what
    # ships, regardless of the operator's local `.env`.
    runtime_env["OTLP_DISABLE_REDACTION"] = "0"
    runtime_proc = subprocess.Popen(
        [sys.executable, "scripts/run_ui_runtime.py"],
        cwd=_REPO_ROOT,
        env=runtime_env,
        stdout=runtime_log,
        stderr=subprocess.STDOUT,
    )
    bff_env = dict(os.environ)
    bff_env["RUNTIME_URL"] = _RUNTIME_URL
    bff_env["UI_TEST_AFFORDANCES"] = "1"
    bff_env["OTLP_DISABLE_REDACTION"] = "0"
    bff_env["TOKEN_SERVICE_URL"] = os.environ.get("TOKEN_SERVICE_URL", "http://localhost:19000/token")
    bff_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "ui.server:app", "--host", "0.0.0.0", "--port", "3000"],
        cwd=_REPO_ROOT,
        env=bff_env,
        stdout=bff_log,
        stderr=subprocess.STDOUT,
    )

    try:
        _wait_until_ready(f"{_RUNTIME_URL}/docs", timeout_seconds=_READY_TIMEOUT_SECONDS)
        _wait_until_ready(_BFF_URL + "/", timeout_seconds=_READY_TIMEOUT_SECONDS)
        yield
    finally:
        for proc in (bff_proc, runtime_proc):
            proc.terminate()
        for proc in (bff_proc, runtime_proc):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        runtime_log.close()
        bff_log.close()


@pytest.fixture(scope="session")
def running_inbox_stack() -> Iterator[None]:
    """Launch the THREE-process review-inbox stack on fresh ports (runtime :8001,
    seeded inbox service :8101, BFF :3001), wait for all three, yield, then
    terminate cleanly.

    The BFF serves the inbox PAGE and proxies the browser's `/api/inbox/*` DATA
    calls to the seeded inbox service, attaching the shared `X-Reviewer-Token`
    server-side (the browser never holds it). The inbox service seeds TWO
    `in_review` candidates (a blueprint + a global_knowledge) over an
    `InMemoryCandidateStore` with a fakes-backed scheduler + `write_plane="full"`,
    so approve is ENABLED and both types land+validate with no live infra
    (see `tests/e2e/_seeded_inbox_app.py`)."""
    runtime_log = open(_REPO_ROOT / "tests" / "e2e" / ".runtime.inbox.log", "w")
    inbox_log = open(_REPO_ROOT / "tests" / "e2e" / ".inbox.service.log", "w")
    bff_log = open(_REPO_ROOT / "tests" / "e2e" / ".bff.inbox.log", "w")

    runtime_env = dict(os.environ)
    runtime_env["DEMO_TEST_SPANS"] = "1"
    # Hermetic to the operator's `.env` debug flag (see `running_stack`): force the
    # DEFAULT redacted span posture regardless of a local OTLP_DISABLE_REDACTION=1.
    runtime_env["OTLP_DISABLE_REDACTION"] = "0"
    # The launcher's `__main__` block hardcodes :8000; the module also exposes the
    # built `app`, so target it via uvicorn on a fresh port (like the BFF below).
    runtime_proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "scripts.run_ui_runtime:app",
            "--host", "0.0.0.0", "--port", "8001",
        ],
        cwd=_REPO_ROOT,
        env=runtime_env,
        stdout=runtime_log,
        stderr=subprocess.STDOUT,
    )

    # Seeded inbox service (:8101). Its `_require_reviewer` gate needs BOTH the
    # flag AND the token set in ITS OWN process env.
    inbox_env = dict(os.environ)
    inbox_env["REVIEW_INBOX_ENABLED"] = "1"
    inbox_env["REVIEWER_TOKEN"] = _REVIEWER_TOKEN
    inbox_proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "tests.e2e._seeded_inbox_app:app",
            "--host", "0.0.0.0", "--port", "8101",
        ],
        cwd=_REPO_ROOT,
        env=inbox_env,
        stdout=inbox_log,
        stderr=subprocess.STDOUT,
    )

    bff_env = dict(os.environ)
    bff_env["RUNTIME_URL"] = _INBOX_RUNTIME_URL
    bff_env["UI_TEST_AFFORDANCES"] = "1"
    bff_env["OTLP_DISABLE_REDACTION"] = "0"
    bff_env["TOKEN_SERVICE_URL"] = os.environ.get("TOKEN_SERVICE_URL", "http://localhost:19000/token")
    # The BFF-side inbox proxy: enable the surface + hold the SAME reviewer secret
    # server-side + point at the seeded inbox service.
    bff_env["REVIEW_INBOX_ENABLED"] = "1"
    bff_env["REVIEWER_TOKEN"] = _REVIEWER_TOKEN
    bff_env["INBOX_SERVICE_URL"] = _INBOX_SERVICE_URL
    bff_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "ui.server:app", "--host", "0.0.0.0", "--port", "3001"],
        cwd=_REPO_ROOT,
        env=bff_env,
        stdout=bff_log,
        stderr=subprocess.STDOUT,
    )

    try:
        _wait_until_ready(f"{_INBOX_RUNTIME_URL}/docs", timeout_seconds=_READY_TIMEOUT_SECONDS)
        # `/inbox/health` without the token 401s (flag on, no header) — still <500,
        # so this confirms the service process is up + serving.
        _wait_until_ready(f"{_INBOX_SERVICE_URL}/inbox/health", timeout_seconds=_READY_TIMEOUT_SECONDS)
        _wait_until_ready(_INBOX_BFF_URL + "/", timeout_seconds=_READY_TIMEOUT_SECONDS)
        yield
    finally:
        for proc in (bff_proc, inbox_proc, runtime_proc):
            proc.terminate()
        for proc in (bff_proc, inbox_proc, runtime_proc):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        runtime_log.close()
        inbox_log.close()
        bff_log.close()
