"""Actual browser rendering with local HTTP fixtures; no runtime or model required."""

from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def test_history_fallback_fresh_page_and_unknown_timestamp():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            _check_freshness(page)
        finally:
            browser.close()


def _check_freshness(page):
    html = (Path(__file__).parents[2] / "ui/static/index.html").read_text()
    sql = "SELECT 1 AS Count"
    old = "2026-10-12T06:00:00.000Z"
    page_calls = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def route(request):
        path = request.request.url
        if "/api/session" in path:
            request.fulfill(json={"session_id": "freshness-test"})
        elif "/api/history" in path:
            request.fulfill(
                json={
                    "turns": [
                        {
                            "turn_index": 0,
                            "question": "Count?",
                            "answer_document": {"schema_version":2, "answer_id":"answer-0", "revision":1,
                                "takeaway":"One.", "sections":[{"title":"Count", "presentation":{"kind":"table", "result_id":"q"}}],
                                "sources":{"q":{"sql":sql, "data_as_of":old}}},
                            "tool_calls": [
                                {"tool_name": "runQuery", "sql": sql, "data_as_of": old}
                            ],
                        }
                    ]
                }
            )
        elif "/api/query/page" in path:
            page_calls.append(request.request.post_data_json)
            request.fulfill(
                json={
                    "columns": ["Count"],
                    "rows": [[1]],
                    "has_more": True,
                    "snapshot_id": "saved",
                }
            )
        elif path.endswith("/answer-document.js"):
            request.fulfill(path=str(Path(__file__).parents[2] / "ui/static/answer-document.js"), content_type="application/javascript")
        else:
            request.fulfill(content_type="text/html", body=html)

    page.route("http://freshness.test/**", route)
    page.goto("http://freshness.test/")
    panel = page.locator(".turn-answer .result-table")
    panel.locator("summary").click()
    expect(panel.locator(".data-as-of")).to_have_text("Data as of 2026-10-12 06:00:00.000 UTC")
    panel.locator(".pager-next").click()
    expect(panel.locator(".data-as-of")).to_have_text("Data as of 2026-10-12 06:00:00.000 UTC")
    assert len(page_calls) == 2
    assert page_calls[0]["sql"] == sql
    assert page_calls[1]["snapshot_id"] == "saved"
    assert "sql" not in page_calls[1]
    page.reload()
    # Rehydrated panels are lazy: the persisted value returns before any new page request.
    expect(page.locator(".turn-answer .data-as-of")).to_have_text(
        "Data as of 2026-10-12 06:00:00.000 UTC"
    )
    assert len(page_calls) == 2
    assert errors == []
