"""Flow 2 (review inbox) E2E — Playwright driving the REAL inbox page
(`ui/static/inbox.html`) through the REAL BFF inbox proxy (`ui/server.py`) to a
SEEDED inbox service (`tests/e2e/_seeded_inbox_app.py`) holding a fakes-backed
promotion write plane (`write_plane="full"`, no live neo4j/couchbase/MCP).

A reviewer opens the inbox, sees TWO in_review candidates — a `blueprint` and a
`global_knowledge` — and APPROVES both via the two-step confirm. Each approve
LANDS (FakeLandingWriter) then validates, so the item leaves the in_review
projection and the list refreshes; the count drops 2 → 1 → 0.

`write_plane="full"` is the key seam: `GET /inbox/health` reports full, so the
page ENABLES approve (offline mode pre-disables it). The reviewer token is held
server-side by the BFF and attached on the proxy hop — the browser never sees it.

Same conventions as `test_conformance.py`: module skip-guard on `RUN_E2E`,
`importorskip` for playwright, a session-scoped stack fixture
(`running_inbox_stack`, on fresh ports 8001/8101/3001), and
`get_by_test_id(...)` + `expect(...)` throughout.
"""

from __future__ import annotations

import os

import pytest

_playwright = pytest.importorskip("playwright.sync_api")
Page = _playwright.Page
expect = _playwright.expect

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_E2E"),
    reason="Layer-3 E2E suite requires RUN_E2E=1 (spins up subprocesses + a browser). "
    "Skipped by default.",
)

_INBOX_BFF_URL = "http://localhost:3001"
_ASSERT_TIMEOUT_MS = 15_000


@pytest.fixture(autouse=True)
def _stack(running_inbox_stack: None) -> None:
    """Every test depends on the seeded three-process inbox stack (session-scoped)."""
    return None


class TestReviewerApprovesBlueprintAndKnowledge:
    def test_approve_both_candidates_drains_the_inbox(self, page: Page) -> None:
        page.goto(f"{_INBOX_BFF_URL}/inbox")

        # Full mode: the read-only health banner stays hidden (approve is armable),
        # and the inbox is NOT empty.
        expect(page.get_by_test_id("inbox-health-banner")).to_be_hidden(
            timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("inbox-empty")).to_be_hidden(
            timeout=_ASSERT_TIMEOUT_MS
        )

        # Two seeded candidates render.
        items = page.get_by_test_id("inbox-item")
        expect(items).to_have_count(2, timeout=_ASSERT_TIMEOUT_MS)

        # Both type badges are present (order-independent): blueprint + knowledge.
        type_badges = page.get_by_test_id("inbox-type")
        expect(type_badges).to_have_count(2, timeout=_ASSERT_TIMEOUT_MS)
        badge_texts = type_badges.all_inner_texts()
        assert "blueprint" in badge_texts, badge_texts
        assert "global_knowledge" in badge_texts, badge_texts

        # The reviewer-facing projection fields render on each item.
        expect(page.get_by_test_id("inbox-reason").first).to_be_visible(
            timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("inbox-summary").first).to_be_visible(
            timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("inbox-payload-view").first).to_be_visible(
            timeout=_ASSERT_TIMEOUT_MS
        )

        # No error at rest.
        expect(page.get_by_test_id("error-banner")).to_be_hidden()

        # Approve the FIRST item (two-step confirm): first click arms, second fires.
        self._approve_first_item(page)

        # After a successful approve the item leaves the in_review projection and
        # the list refreshes: count drops to 1.
        expect(items).to_have_count(1, timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("error-banner")).to_be_hidden()

        # Approve the remaining item → inbox drains to empty.
        self._approve_first_item(page)
        expect(items).to_have_count(0, timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("inbox-empty")).to_be_visible(
            timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("error-banner")).to_be_hidden()

    @staticmethod
    def _approve_first_item(page: Page) -> None:
        """Approve the first rendered inbox item via its scoped two-step confirm."""
        item = page.get_by_test_id("inbox-item").first
        approve = item.get_by_test_id("inbox-approve")
        expect(approve).to_be_enabled(timeout=_ASSERT_TIMEOUT_MS)

        # First click ARMS the button (destructive/promotion intent gate).
        approve.click()
        expect(approve).to_have_attribute("data-confirming", "1", timeout=_ASSERT_TIMEOUT_MS)
        expect(approve).to_have_text("Confirm Approve", timeout=_ASSERT_TIMEOUT_MS)

        # Second click FIRES the approve POST.
        approve.click()
