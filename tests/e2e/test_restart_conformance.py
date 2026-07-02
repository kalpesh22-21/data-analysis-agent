"""Layer-3 restart-durability conformance (D45) — the ONE scenario that needs a
real, out-of-process session store. A paused approval checkpoint MUST survive
the runtime PROCESS itself restarting (not just a page reload); an
`InMemorySessionStore` loses it by design, so this scenario runs the demo
launcher against the live `l2-cb` Couchbase (`DEMO_SESSION_STORE=couchbase`).

Isolated from the rest of the Layer-3 suite on its OWN ports (:8010 runtime,
:3010 BFF) with its OWN fixtures — it does NOT use `conftest.py`'s session-scoped
`running_stack` (:8000/:3000 in-memory), so a Couchbase outage fails exactly
this one test, never the other eleven. The fixture terminates and relaunches the
:8010 runtime subprocess mid-pause (a genuine process bounce), while the BFF and
the browser page are untouched; clicking approve then resumes the fresh runtime,
which CAS-consumes the Couchbase checkpoint and completes the blueprint DAG.

Skipped unless RUN_E2E is set AND the l2-cb Couchbase admin REST answers on
:8091 (bring it up + seed via `docker compose -f docker-compose.integration.yml
up -d couchbase && scripts/couchbase-init.sh`).
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

_playwright = pytest.importorskip("playwright.sync_api")
Page = _playwright.Page
expect = _playwright.expect

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_PORT = 8010
_BFF_PORT = 3010
_RUNTIME_URL = f"http://localhost:{_RUNTIME_PORT}"
_BFF_URL = f"http://localhost:{_BFF_PORT}"
_COUCHBASE_REST = "http://localhost:8091/pools"
_READY_TIMEOUT_SECONDS = 40.0
_ASSERT_TIMEOUT_MS = 20_000


def _couchbase_up() -> bool:
    try:
        return httpx.get(_COUCHBASE_REST, timeout=2.0).status_code < 500
    except httpx.HTTPError:
        return False


pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("RUN_E2E"),
        reason="Layer-3 restart-durability requires RUN_E2E=1.",
    ),
    pytest.mark.skipif(
        not _couchbase_up(),
        reason="Layer-3 restart-durability requires the live l2-cb Couchbase on :8091 "
        "(docker compose -f docker-compose.integration.yml up -d couchbase && "
        "scripts/couchbase-init.sh).",
    ),
]


def _wait_until_ready(url: str, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2.0).status_code < 500:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(0.5)
    raise RuntimeError(f"{url} not ready within {timeout_seconds}s (last error: {last_error})")


class _RestartableStack:
    """Owns the Couchbase-backed runtime subprocess (bounceable) + the BFF."""

    def __init__(self) -> None:
        self._runtime_log = open(_REPO_ROOT / "tests" / "e2e" / ".runtime.restart.log", "w")
        self._bff_log = open(_REPO_ROOT / "tests" / "e2e" / ".bff.restart.log", "w")
        self._runtime_env = dict(os.environ)
        self._runtime_env["DEMO_SESSION_STORE"] = "couchbase"
        self._runtime: subprocess.Popen[bytes] | None = None
        self._bff: subprocess.Popen[bytes] | None = None

    def _spawn_runtime(self) -> subprocess.Popen[bytes]:
        # `uvicorn scripts.run_ui_runtime:app` imports the module → its
        # module-level `app = build_demo_app()` reads DEMO_SESSION_STORE=couchbase
        # and builds the lazy Couchbase-backed app. Factory not needed: the lazy
        # store defers the real (loop-requiring) connect to the first request.
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "scripts.run_ui_runtime:app",
                "--host",
                "0.0.0.0",
                "--port",
                str(_RUNTIME_PORT),
            ],
            cwd=_REPO_ROOT,
            env=self._runtime_env,
            stdout=self._runtime_log,
            stderr=subprocess.STDOUT,
        )

    def start(self) -> None:
        self._runtime = self._spawn_runtime()
        bff_env = dict(os.environ)
        bff_env["RUNTIME_URL"] = _RUNTIME_URL
        bff_env["TOKEN_SERVICE_URL"] = os.environ.get(
            "TOKEN_SERVICE_URL", "http://localhost:19000/token"
        )
        self._bff = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "ui.server:app",
                "--host",
                "0.0.0.0",
                "--port",
                str(_BFF_PORT),
            ],
            cwd=_REPO_ROOT,
            env=bff_env,
            stdout=self._bff_log,
            stderr=subprocess.STDOUT,
        )
        _wait_until_ready(f"{_RUNTIME_URL}/docs", timeout_seconds=_READY_TIMEOUT_SECONDS)
        _wait_until_ready(f"{_BFF_URL}/", timeout_seconds=_READY_TIMEOUT_SECONDS)

    def restart_runtime(self) -> None:
        """Terminate and relaunch ONLY the runtime subprocess (a real process
        bounce) — the BFF and the browser page are left untouched. The fresh
        runtime shares the same live Couchbase, so the paused checkpoint persists
        across the bounce."""
        assert self._runtime is not None
        self._runtime.terminate()
        try:
            self._runtime.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._runtime.kill()
            self._runtime.wait(timeout=10)
        self._runtime = self._spawn_runtime()
        _wait_until_ready(f"{_RUNTIME_URL}/docs", timeout_seconds=_READY_TIMEOUT_SECONDS)

    def stop(self) -> None:
        for proc in (self._bff, self._runtime):
            if proc is not None:
                proc.terminate()
        for proc in (self._bff, self._runtime):
            if proc is not None:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
        self._runtime_log.close()
        self._bff_log.close()


@pytest.fixture
def restartable_stack() -> Iterator[_RestartableStack]:
    stack = _RestartableStack()
    stack.start()
    try:
        yield stack
    finally:
        stack.stop()


class TestPauseResumeSurvivesRestart:
    """Scenario 12 — pause/resume durability across a runtime restart (D45):
    an approval pause persists a checkpoint to Couchbase; the runtime PROCESS is
    then killed and relaunched; clicking approve resumes the FRESH runtime, which
    CAS-consumes the Couchbase checkpoint and completes the blueprint to a
    verified answer — proving durability a mock/in-memory store cannot."""

    def test_approval_pause_survives_runtime_restart(
        self, page: Page, restartable_stack: _RestartableStack
    ) -> None:
        page.goto(_BFF_URL)
        expect(page.locator("#session-line")).not_to_have_text(
            "connecting…", timeout=_ASSERT_TIMEOUT_MS
        )

        # Drive the approval blueprint → pause showing approve/deny chips. The
        # checkpoint is persisted to Couchbase before the turn yields (D45).
        page.get_by_test_id("message-input").fill("approve headcount for sales")
        page.get_by_test_id("send-button").click()

        expect(page.get_by_test_id("ask-user")).to_be_visible(timeout=_ASSERT_TIMEOUT_MS)
        approve_chip = page.locator('[data-testid="ask-user-option"][data-option="approve"]')
        expect(approve_chip).to_be_visible(timeout=_ASSERT_TIMEOUT_MS)

        # BOUNCE the runtime process mid-pause (fresh process, same Couchbase).
        # The browser page — still showing the pending approval chips — is untouched.
        restartable_stack.restart_runtime()

        # Approve → /api/turn/resume hits the FRESH runtime, which resumes the
        # blueprint executor at its awaiting node from the surviving checkpoint.
        approve_chip.click()

        expect(page.get_by_test_id("ask-user")).to_be_hidden(timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("answer")).not_to_have_text("", timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("status")).to_contain_text("done", timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("error-banner")).to_be_hidden()
