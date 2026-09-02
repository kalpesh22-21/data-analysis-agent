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

import httpx
import pytest

_playwright = pytest.importorskip("playwright.sync_api")
Page = _playwright.Page
Locator = _playwright.Locator
expect = _playwright.expect

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_E2E"),
    reason="Layer-3 E2E suite requires RUN_E2E=1 (spins up subprocesses + a browser). "
    "Skipped by default.",
)

_INBOX_BFF_URL = "http://localhost:3001"
# The seeded inbox service (same port the `running_inbox_stack` fixture boots it on).
# The test-only `/_test/reseed` hook lives here — reachable directly (no reviewer token
# needed) so each test starts from the pristine two-candidate seed.
_INBOX_SERVICE_URL = "http://localhost:8101"
_ASSERT_TIMEOUT_MS = 15_000


@pytest.fixture(autouse=True)
def _stack(running_inbox_stack: None) -> None:
    """Every test depends on the seeded three-process inbox stack (session-scoped).

    The store is IN-MEMORY and SHARED across the whole session, so approve/reject in one
    test would otherwise leak into the next. Reset to the pristine two `in_review` seeds
    (a blueprint + a global_knowledge, archive empty) BEFORE each test so the suite is
    order-independent."""
    httpx.post(f"{_INBOX_SERVICE_URL}/_test/reseed", timeout=5.0).raise_for_status()
    return None


# --- shared locators ---------------------------------------------------------

_TYPES = ("blueprint", "global_knowledge", "user_knowledge", "schema_edit")


def _tab(page: Page, type_: str) -> Locator:
    """The type tab for *type_* (by its `data-type`)."""
    return page.locator(f'[data-testid="inbox-tab"][data-type="{type_}"]')


def _tab_count(page: Page, type_: str) -> Locator:
    """The count badge inside the *type_* tab."""
    return _tab(page, type_).get_by_test_id("inbox-tab-count")


def _visible_items(page: Page) -> Locator:
    """Only the inbox items in the ACTIVE tab — other-type items stay in the DOM but
    are `hidden`, so `:visible` scopes to what the reviewer actually sees."""
    return page.locator('[data-testid="inbox-item"]:visible')


def _status_button(page: Page, label: str) -> Locator:
    """A Review-queue/Archived toggle button by its visible label."""
    return page.get_by_test_id("inbox-status-toggle").get_by_text(label)


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

        # The seeded set splits one blueprint + one global_knowledge across the type
        # tabs. Only the ACTIVE tab's items are visible now, so assert each type on its
        # OWN tab — a hidden item's innerText is "" (the old both-at-once read is why
        # this block had to become tab-aware).
        expect(_tab_count(page, "blueprint")).to_have_text("1", timeout=_ASSERT_TIMEOUT_MS)
        expect(_tab_count(page, "global_knowledge")).to_have_text(
            "1", timeout=_ASSERT_TIMEOUT_MS
        )

        # Blueprint tab is active by default: its single item is the only visible one.
        visible = _visible_items(page)
        expect(visible).to_have_count(1, timeout=_ASSERT_TIMEOUT_MS)
        expect(visible.get_by_test_id("inbox-type")).to_have_text("blueprint")

        # Switch to the global_knowledge tab: now its single item is the visible one.
        _tab(page, "global_knowledge").click()
        expect(visible).to_have_count(1, timeout=_ASSERT_TIMEOUT_MS)
        expect(visible.get_by_test_id("inbox-type")).to_have_text("global_knowledge")

        # Back to the blueprint tab to drive the approve flow from a known tab (the
        # first rendered row is then the blueprint).
        _tab(page, "blueprint").click()
        expect(_visible_items(page).get_by_test_id("inbox-type")).to_have_text("blueprint")

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


class TestTypeTabs:
    """Per-type tabbed dashboard (ui-inbox-type-archive contract §Frontend)."""

    def test_all_four_tabs_render_with_seeded_counts(self, page: Page) -> None:
        page.goto(f"{_INBOX_BFF_URL}/inbox")
        expect(page.get_by_test_id("inbox-tabs")).to_be_visible(timeout=_ASSERT_TIMEOUT_MS)

        # The fixed four types always render (shown even at 0), stable order.
        tabs = page.get_by_test_id("inbox-tab")
        expect(tabs).to_have_count(4, timeout=_ASSERT_TIMEOUT_MS)
        for type_ in _TYPES:
            expect(_tab(page, type_)).to_be_visible()

        # Seeded set: one blueprint + one global_knowledge; schema_edit is empty.
        expect(_tab_count(page, "blueprint")).to_have_text("1", timeout=_ASSERT_TIMEOUT_MS)
        expect(_tab_count(page, "global_knowledge")).to_have_text("1")
        expect(_tab_count(page, "schema_edit")).to_have_text("0")
        # ⚠ USER KNOWLEDGE READS "—", NOT "0" (knowledge-edit design §D.3). That tab stopped
        # being a filter over the candidate list — `user_knowledge` candidates auto-commit to
        # the per-user store and are dropped from the candidate store, so a count over
        # candidates could only ever be zero. It is now a VIEW over one named user's facts, and
        # nothing has been counted until a reviewer names a user: "0" would claim a store had
        # been consulted and found empty, which is a different and false statement.
        expect(_tab_count(page, "user_knowledge")).to_have_text("—")

    def test_clicking_a_tab_filters_the_visible_list_to_that_type(
        self, page: Page
    ) -> None:
        page.goto(f"{_INBOX_BFF_URL}/inbox")
        # Both seeded items are in the DOM …
        expect(page.get_by_test_id("inbox-item")).to_have_count(2, timeout=_ASSERT_TIMEOUT_MS)

        # … but only the active (default blueprint) tab's one item is VISIBLE.
        visible = _visible_items(page)
        expect(visible).to_have_count(1, timeout=_ASSERT_TIMEOUT_MS)
        expect(visible.get_by_test_id("inbox-type")).to_have_text("blueprint")

        # Clicking a tab is a client-side filter → the visible item switches type.
        _tab(page, "global_knowledge").click()
        expect(visible).to_have_count(1, timeout=_ASSERT_TIMEOUT_MS)
        expect(visible.get_by_test_id("inbox-type")).to_have_text("global_knowledge")

        _tab(page, "blueprint").click()
        expect(visible).to_have_count(1, timeout=_ASSERT_TIMEOUT_MS)
        expect(visible.get_by_test_id("inbox-type")).to_have_text("blueprint")

    def test_empty_tab_shows_the_per_tab_empty_state(self, page: Page) -> None:
        page.goto(f"{_INBOX_BFF_URL}/inbox")
        expect(page.get_by_test_id("inbox-tabs")).to_be_visible(timeout=_ASSERT_TIMEOUT_MS)

        # schema_edit has no seeded items → its tab shows the per-tab empty state, and none of
        # the (other-type) seeded items are visible under it. This used to be asserted on
        # user_knowledge, which no longer HAS an empty state — see the test below.
        _tab(page, "schema_edit").click()
        empty = page.get_by_test_id("inbox-empty")
        expect(empty).to_be_visible(timeout=_ASSERT_TIMEOUT_MS)
        expect(empty).to_contain_text("schema edit")
        expect(empty).to_contain_text("review queue")
        expect(_visible_items(page)).to_have_count(0)

    def test_the_user_knowledge_tab_is_its_own_panel_not_an_empty_queue(
        self, page: Page
    ) -> None:
        """⚠ THE TAB THIS DESIGN REPLACED (knowledge-edit design §D.3).

        It used to be a filter over the candidate list that could never match anything, so it
        rendered the per-tab empty state forever. It is now a VIEW over the per-user store: a
        user-id box, a Load button, and one card per record with a Promote button.

        So the assertion inverts — the empty state must NOT be shown, because "no candidates of
        this type" is not a true thing to say about a panel that does not list candidates.
        """
        page.goto(f"{_INBOX_BFF_URL}/inbox")
        expect(page.get_by_test_id("inbox-tabs")).to_be_visible(timeout=_ASSERT_TIMEOUT_MS)

        _tab(page, "user_knowledge").click()

        # The panel replaces the candidate list, with the two controls §D.3 names.
        panel = page.get_by_test_id("inbox-uk-panel")
        expect(panel).to_be_visible(timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("inbox-uk-user-id")).to_be_visible()
        expect(page.get_by_test_id("inbox-uk-load")).to_be_visible()
        expect(page.get_by_test_id("inbox-uk-list")).to_be_attached()

        # NO empty state and NO candidate rows — the queue view is not what this tab shows.
        expect(page.get_by_test_id("inbox-empty")).to_be_hidden()
        expect(_visible_items(page)).to_have_count(0)
        # Nothing has been read yet, and the count line says so rather than claiming zero.
        expect(page.get_by_test_id("inbox-count-line")).to_contain_text("name a user")

    def test_loading_user_knowledge_without_a_user_id_is_refused_on_the_page(
        self, page: Page
    ) -> None:
        """§D.1: the reviewer must NAME the user. The blank id is refused in the page as well
        as at the BFF, and the message says why — a blank id is not a request for everyone.

        Asserted in the browser because this is the guard that keeps the D17 exception narrow,
        and a guard that only exists server-side would still let the page ASK."""
        page.goto(f"{_INBOX_BFF_URL}/inbox")
        _tab(page, "user_knowledge").click()
        expect(page.get_by_test_id("inbox-uk-panel")).to_be_visible(
            timeout=_ASSERT_TIMEOUT_MS
        )

        page.get_by_test_id("inbox-uk-load").click()

        status = page.get_by_test_id("inbox-uk-status")
        expect(status).to_be_visible(timeout=_ASSERT_TIMEOUT_MS)
        expect(status).to_contain_text("never all of them")
        # Still nothing loaded, so the badge still reads "—" rather than "0".
        expect(_tab_count(page, "user_knowledge")).to_have_text("—")


class TestStatusToggleDefault:
    """The Review-queue ↔ Archived toggle (ui-inbox-type-archive contract §Status)."""

    def test_review_queue_active_on_load_and_archive_empty(self, page: Page) -> None:
        page.goto(f"{_INBOX_BFF_URL}/inbox")
        toggle = page.get_by_test_id("inbox-status-toggle")
        expect(toggle).to_be_visible(timeout=_ASSERT_TIMEOUT_MS)

        # Review queue is the active status on load; Archived is not.
        expect(_status_button(page, "Review queue")).to_have_attribute(
            "aria-pressed", "true", timeout=_ASSERT_TIMEOUT_MS
        )
        expect(_status_button(page, "Archived")).to_have_attribute("aria-pressed", "false")

        # It shows the two seeded in_review candidates.
        expect(page.get_by_test_id("inbox-item")).to_have_count(2, timeout=_ASSERT_TIMEOUT_MS)

        # Switching to Archived refetches; with nothing rejected yet it is empty.
        _status_button(page, "Archived").click()
        expect(_status_button(page, "Archived")).to_have_attribute(
            "aria-pressed", "true", timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("inbox-item")).to_have_count(0, timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("inbox-empty")).to_be_visible(timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("error-banner")).to_be_hidden()


class TestArchiveOnReject:
    """Reject archives, not deletes: a rejected candidate leaves the review queue and
    appears in the Archived view as a read-only REJECTED card (contract §Actions)."""

    def test_reject_moves_candidate_from_review_queue_to_archive(
        self, page: Page
    ) -> None:
        page.goto(f"{_INBOX_BFF_URL}/inbox")
        expect(page.get_by_test_id("inbox-item")).to_have_count(2, timeout=_ASSERT_TIMEOUT_MS)

        # Reject the seeded global_knowledge candidate from the review queue.
        _tab(page, "global_knowledge").click()
        target = _visible_items(page)
        expect(target).to_have_count(1, timeout=_ASSERT_TIMEOUT_MS)
        rejected_id = target.get_attribute("data-candidate-id")
        assert rejected_id, "the seeded global_knowledge item must carry a candidate id"
        self._reject_first_visible(page)

        # It leaves the review queue: the global_knowledge tab count drops to 0 and the
        # row is gone from the DOM (no error surfaced).
        expect(_tab_count(page, "global_knowledge")).to_have_text(
            "0", timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.locator(f'[data-candidate-id="{rejected_id}"]')).to_have_count(0)
        expect(page.get_by_test_id("error-banner")).to_be_hidden()

        # Switch to the Archived view → the rejected candidate now appears there.
        _status_button(page, "Archived").click()
        archived = _visible_items(page)
        expect(archived).to_have_count(1, timeout=_ASSERT_TIMEOUT_MS)
        expect(archived).to_have_attribute("data-candidate-id", rejected_id)
        expect(archived.get_by_test_id("inbox-type")).to_have_text("global_knowledge")

        # It carries a REJECTED status badge …
        expect(archived.get_by_test_id("inbox-status")).to_have_text("REJECTED")
        # … and NO action buttons (terminal, read-only — contract §Actions).
        expect(page.get_by_test_id("inbox-approve")).to_have_count(0)
        expect(page.get_by_test_id("inbox-reject")).to_have_count(0)
        expect(page.get_by_test_id("inbox-retract")).to_have_count(0)
        expect(page.get_by_test_id("error-banner")).to_be_hidden()

        # Back to the Review queue → the rejected candidate is no longer there.
        _status_button(page, "Review queue").click()
        expect(page.get_by_test_id("inbox-item")).to_have_count(1, timeout=_ASSERT_TIMEOUT_MS)
        expect(page.locator(f'[data-candidate-id="{rejected_id}"]')).to_have_count(0)
        expect(page.get_by_test_id("error-banner")).to_be_hidden()

    @staticmethod
    def _reject_first_visible(page: Page) -> None:
        """Reject the first visible inbox item via its scoped two-step confirm."""
        item = _visible_items(page).first
        reject = item.get_by_test_id("inbox-reject")
        expect(reject).to_be_enabled(timeout=_ASSERT_TIMEOUT_MS)

        # First click ARMS the reject; second FIRES the POST.
        reject.click()
        expect(reject).to_have_attribute("data-confirming", "1", timeout=_ASSERT_TIMEOUT_MS)
        expect(reject).to_have_text("Confirm Reject", timeout=_ASSERT_TIMEOUT_MS)
        reject.click()
