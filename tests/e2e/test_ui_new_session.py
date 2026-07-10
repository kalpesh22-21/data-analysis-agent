"""Flow-1 E2E — the "New session" control on the REAL minimal UI
(`ui/static/index.html`), driven by Playwright through the REAL BFF
(`ui/server.py`) and the deterministic scripted runtime
(`scripts/run_ui_runtime.py`) — the SAME session-scoped `running_stack` the
chat suite (`test_ui_chat.py`) and the conformance suite use.

Covers the frontend-only "New session" button added alongside the iMessage-style
transcript restyle:

  * it is present, visible, and enabled on load;
  * clicking it MINTS A DISTINCT session_id (proves the old id is NOT reused) —
    asserted on BOTH the visible `#session-line` and the persisted
    sessionStorage `data-agent-session` value;
  * clicking it CLEARS the transcript (no `turn-block` children → the ":empty"
    placeholder), clears progress, and hides the ask-user panel;
  * the freshly minted session ROUND-TRIPS a question end-to-end.

Same conventions as `test_ui_chat.py`: module skip-guard on `RUN_E2E`,
`importorskip` for playwright, the session-scoped `running_stack` fixture, and
`get_by_test_id(...)` + `expect(...)` throughout — never exact LLM prose.
"""

from __future__ import annotations

import os

import pytest

_playwright = pytest.importorskip("playwright.sync_api")
Page = _playwright.Page
expect = _playwright.expect

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_E2E"),
    reason="Layer-3 E2E suite requires RUN_E2E=1 (spins up subprocesses + a browser; "
    "needs the l2-token container up for JWT minting). Skipped by default.",
)

_BFF_URL = "http://localhost:3000"
_ASSERT_TIMEOUT_MS = 15_000
_SESSION_STORAGE_KEY = "data-agent-session"


@pytest.fixture(autouse=True)
def _stack(running_stack: None) -> None:
    """Every test depends on the running two-process chat stack (session-scoped —
    the SAME fixture the chat/conformance suites use, started once for the run)."""
    return None


def _goto_and_wait_for_session(page: Page) -> None:
    page.goto(_BFF_URL)
    # Bootstrap (`index.html`'s `fetch("/api/session", ...)`) must complete before
    # interacting — else the first action races the mint.
    expect(page.locator("#session-line")).not_to_have_text(
        "connecting…", timeout=_ASSERT_TIMEOUT_MS
    )


def _send_message(page: Page, message: str) -> None:
    page.get_by_test_id("message-input").fill(message)
    page.get_by_test_id("send-button").click()


def _saved_session(page: Page) -> str | None:
    """The persisted opaque session id (`sessionStorage['data-agent-session']`).
    The JWT never leaves the server (D82/D5) — only this id is stored."""
    return page.evaluate(
        "(k) => window.sessionStorage.getItem(k)", _SESSION_STORAGE_KEY
    )


class TestNewSessionButtonPresent:
    """The control itself: present, visible, and interactive on first load."""

    def test_button_visible_and_enabled_on_load(self, page: Page) -> None:
        _goto_and_wait_for_session(page)
        button = page.get_by_test_id("new-session-button")
        expect(button).to_be_visible(timeout=_ASSERT_TIMEOUT_MS)
        expect(button).to_be_enabled(timeout=_ASSERT_TIMEOUT_MS)


class TestNewSessionMintsDistinctSession:
    """The key guarantee: "New session" issues a BRAND-NEW session_id and never
    reuses the old one — asserted on both the visible line and the persisted id."""

    def test_click_mints_a_new_non_empty_id(self, page: Page) -> None:
        _goto_and_wait_for_session(page)

        # Capture the current session id from BOTH surfaces before the reset.
        old_line = page.locator("#session-line").inner_text().strip()
        old_saved = _saved_session(page)
        assert old_line, "session line was empty before reset"
        assert old_saved, "no session persisted before reset"
        # The visible line and the persisted id agree at rest.
        assert old_line == old_saved

        page.get_by_test_id("new-session-button").click()

        # The line settles on a NEW, non-empty id (never the transient
        # "connecting…" and never the old id).
        line = page.locator("#session-line")
        expect(line).not_to_have_text("connecting…", timeout=_ASSERT_TIMEOUT_MS)
        expect(line).not_to_have_text("", timeout=_ASSERT_TIMEOUT_MS)
        expect(line).not_to_have_text(old_line, timeout=_ASSERT_TIMEOUT_MS)

        # The persisted id was replaced too (old id is not reused anywhere).
        def _new_saved_id() -> None:
            new_saved = _saved_session(page)
            assert new_saved, "no session persisted after reset"
            assert new_saved != old_saved, "reset reused the OLD session id"
            assert new_saved == line.inner_text().strip()

        # sessionStorage is set synchronously inside mintSession's .then, right
        # after the line is updated — poll briefly to avoid ordering flake.
        _retry(_new_saved_id)

        # A reset that mints cleanly never raises the error banner.
        expect(page.get_by_test_id("error-banner")).to_be_hidden()


class TestNewSessionClearsTranscript:
    """"New session" wipes the client: no lingering turn blocks, no stale
    progress, and the ask-user panel hidden — the new session starts empty."""

    def test_reset_clears_transcript_and_progress(self, page: Page) -> None:
        _goto_and_wait_for_session(page)

        # Ask a question so a turn block + progress exist to be cleared.
        _send_message(page, "show me the columns")
        expect(page.get_by_test_id("turn-block").first).to_be_visible(
            timeout=_ASSERT_TIMEOUT_MS
        )
        # Let the turn finish so the reset is not racing an in-flight stream (the
        # in-flight case is covered separately in TestNewSessionInFlightReset).
        expect(page.get_by_test_id("status").first).to_contain_text(
            "done", timeout=_ASSERT_TIMEOUT_MS
        )

        page.get_by_test_id("new-session-button").click()

        # A distinct session was minted (the line leaves "connecting…").
        expect(page.locator("#session-line")).not_to_have_text(
            "connecting…", timeout=_ASSERT_TIMEOUT_MS
        )

        # Transcript is empty: no turn-block children remain (":empty"
        # placeholder shows). Progress list is empty. Ask-user is hidden.
        expect(page.get_by_test_id("turn-block")).to_have_count(
            0, timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("progress-item")).to_have_count(
            0, timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("ask-user")).to_be_hidden()
        expect(page.get_by_test_id("error-banner")).to_be_hidden()


class TestMintedSessionWorks:
    """The freshly minted session is fully functional: after a reset, a question
    round-trips through the NEW session and a fresh answer renders."""

    def test_question_round_trips_after_reset(self, page: Page) -> None:
        _goto_and_wait_for_session(page)

        # Reset first, then ask in the fresh session.
        page.get_by_test_id("new-session-button").click()
        expect(page.locator("#session-line")).not_to_have_text(
            "connecting…", timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("turn-block")).to_have_count(
            0, timeout=_ASSERT_TIMEOUT_MS
        )

        _send_message(page, "show me the columns")

        # A new turn materializes and answers in the minted session.
        expect(page.get_by_test_id("turn-block").first).to_be_visible(
            timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("progress-item").first).to_be_visible(
            timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("answer").first).not_to_have_text(
            "", timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("status").first).to_contain_text(
            "done", timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("error-banner")).to_be_hidden()


class TestNewSessionInFlightReset:
    """Reset while a turn's SSE stream is still streaming (the click-during-
    in-flight race).

    The invariants under test — the two the reset MUST hold no matter the timing:
      1. No dangling render: after reset the transcript has NO turn-block (the
         abandoned stream must not paint into the new/empty transcript). This is
         guarded in `handleSSEEvent`, whose `result` branch only renders when
         `inProgressBlock` is set — and `resetSession()` nulls it.
      2. The "New session" button does not stay disabled: `resetSession` disables
         it only for the duration of `mintSession()`, re-enabling in `.finally`.

    NOTE (for the reviewer): landing the click *inside* the stream window is
    timing-dependent even with the scripted runtime. This test issues the reset
    immediately after send (best-effort mid-flight) and asserts only the two
    timing-robust invariants above; it does NOT assert on transient progress
    items, because a still-draining abandoned stream can append progress into the
    freshly-cleared list AFTER reset (see the reported secondary artifact —
    progress/ask-user/error events in `handleSSEEvent` are NOT guarded by
    `inProgressBlock`). Those secondary artifacts are a latent defect, not a
    regression of this restyle, and are reported separately rather than pinned
    down flakily here.
    """

    def test_reset_during_stream_leaves_no_dangling_block(self, page: Page) -> None:
        _goto_and_wait_for_session(page)

        _send_message(page, "run the headcount by department blueprint")
        # The turn block appears synchronously on submit; reset right away to try
        # to catch the stream still in flight.
        expect(page.get_by_test_id("turn-block").first).to_be_visible(
            timeout=_ASSERT_TIMEOUT_MS
        )
        page.get_by_test_id("new-session-button").click()

        # Invariant 1: the transcript ends up empty — no abandoned block renders.
        expect(page.get_by_test_id("turn-block")).to_have_count(
            0, timeout=_ASSERT_TIMEOUT_MS
        )
        # Invariant 2: a fresh session was minted and the button is usable again.
        expect(page.locator("#session-line")).not_to_have_text(
            "connecting…", timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("new-session-button")).to_be_enabled(
            timeout=_ASSERT_TIMEOUT_MS
        )


def _retry(assertion, *, attempts: int = 20, delay_s: float = 0.1) -> None:
    """Poll a plain-assert `assertion()` until it stops raising (mirrors
    Playwright's own auto-retry for the non-locator sessionStorage read)."""
    import time

    last: AssertionError | None = None
    for _ in range(attempts):
        try:
            assertion()
            return
        except AssertionError as exc:  # noqa: PERF203
            last = exc
            time.sleep(delay_s)
    if last is not None:
        raise last
