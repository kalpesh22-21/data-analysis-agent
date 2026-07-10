"""Flow 1 (chat) E2E — Playwright driving the REAL minimal UI
(`ui/static/index.html`) through the REAL BFF (`ui/server.py`) through the REAL
agent runtime, deterministic via the scripted doubles in
`scripts/run_ui_runtime.py` (NO OpenAI key, NO live ClickHouse; JWT verification
against the real `l2-token` container is NOT bypassed).

Complements `test_conformance.py`'s 11 spec scenarios with a focused
"ask-a-question → get-an-answer" pair that asserts the ENRICHED result surface a
verified runBlueprint fast-path renders (the blueprint chip, verified badge, SQL
panel, result table, and data-lineage panel) — the observable payoff of the D89
fast path, beyond the conformance suite's progress/answer/status structural
checks.

Same conventions as `test_conformance.py`: module skip-guard on `RUN_E2E`,
`importorskip` for playwright, the session-scoped `running_stack` fixture, and
`get_by_test_id(...)` + `expect(...)` throughout — never exact LLM prose (the
scripted doubles' wording is demo copy, not a contract).
"""

from __future__ import annotations

import os

# Skip the whole module cleanly at COLLECTION if playwright isn't installed
# (Layer-3 runs nightly/release, not per-PR).
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


@pytest.fixture(autouse=True)
def _stack(running_stack: None) -> None:
    """Every test depends on the running two-process chat stack (session-scoped —
    the SAME fixture the conformance suite uses, started once for the run)."""
    return None


def _goto_and_wait_for_session(page: Page) -> None:
    page.goto(_BFF_URL)
    # Bootstrap (`index.html`'s `fetch("/api/session", ...)`) must complete before
    # sending — else the first message surfaces a NO_SESSION error.
    expect(page.locator("#session-line")).not_to_have_text(
        "connecting…", timeout=_ASSERT_TIMEOUT_MS
    )


def _send_message(page: Page, message: str) -> None:
    page.get_by_test_id("message-input").fill(message)
    page.get_by_test_id("send-button").click()


class TestAskQuestionGetsEnrichedAnswer:
    """A user asks a question and gets a VERIFIED, enriched answer: the
    "headcount by department" runBlueprint fast path (D89) runs the node query,
    the grain probe passes, and the turn renders the full enriched surface."""

    def test_verified_blueprint_answer_renders_full_surface(self, page: Page) -> None:
        _goto_and_wait_for_session(page)
        _send_message(page, "run the headcount by department blueprint")

        # A turn block materializes for the question.
        turn = page.get_by_test_id("turn-block").first
        expect(turn).to_be_visible(timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("turn-question").first).not_to_have_text(
            "", timeout=_ASSERT_TIMEOUT_MS
        )

        # At least one progress item streamed (tool_dispatch_start/ok → items).
        expect(page.get_by_test_id("progress-item").first).to_be_visible(
            timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("progress-item")).not_to_have_count(
            0, timeout=_ASSERT_TIMEOUT_MS
        )

        # The answer becomes non-empty and the turn reaches "done".
        expect(page.get_by_test_id("answer").first).not_to_have_text(
            "", timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("status").first).to_contain_text(
            "done", timeout=_ASSERT_TIMEOUT_MS
        )

        # The ENRICHED surface a verified blueprint run produces: the blueprint
        # chip + verified badge, the SQL panel, the result table, and the data
        # lineage (provenance) panel all render.
        expect(page.get_by_test_id("blueprint-chip").first).to_be_visible(
            timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("verified-badge").first).to_be_visible(
            timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("sql-panel").first).to_be_visible(
            timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("result-table").first).to_be_visible(
            timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("provenance-panel").first).to_be_visible(
            timeout=_ASSERT_TIMEOUT_MS
        )

        # A verified fast path is never a crash.
        expect(page.get_by_test_id("error-banner")).to_be_hidden()


class TestAskPlainQuestionGetsAnswer:
    """The simpler ask→answer path: a plain "columns" question drives a
    getTableSchema tool call, progress streams, and a non-empty answer renders —
    the minimal happy path a user sees, with no blueprint enrichment required."""

    def test_plain_question_streams_progress_and_answers(self, page: Page) -> None:
        _goto_and_wait_for_session(page)
        _send_message(page, "show me the columns")

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
