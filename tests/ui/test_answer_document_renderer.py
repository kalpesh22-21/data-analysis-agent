from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def page():
    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        yield browser.new_page()
        browser.close()


def test_document_renderer_charts_missing_points_and_safe_fallback(page):
    page.set_content('<main id="answer"></main>')
    page.add_script_tag(path=str(ROOT / "ui/static/answer-document.js"))
    page.evaluate("""() => {
      const sections = [
        {title:'Figure',presentation:{kind:'figure',label:'Employees',value:'7'}},
        {title:'Facts',presentation:{kind:'facts',items:[{label:'Count',value:'7'}]}},
        {title:'Bars',presentation:{kind:'bars',measure_label:'Employees',items:[{label:'Sales',raw:3,value:'3'},{label:'Engineering',raw:4,value:'4'}]}},
        {title:'Line',presentation:{kind:'line',measure_label:'Salary',points:[
          {time:'2026-01-01',label:'Jan',value:1,formatted_value:'1'},
          {time:'2026-02-01',label:'Feb',value:null,formatted_value:null},
          {time:'2026-03-01',label:'Mar',value:3,formatted_value:'3'}]}},
        {title:'Broken',presentation:{kind:'bars',items:[{raw:'3'}]},fallback_table:{result_id:'q'}},
        {title:'Unavailable',presentation:{kind:'unknown'}},
      ];
      window.renderAnswerDocument(document.querySelector('main'),{
        schema_version:1,takeaway:'<img src=x onerror="window.pwned=true">',sections,
        sources:{q:{table_ref:'opaque'}}
      }, (parent,ref) => {const el=document.createElement('p');el.textContent='Table '+ref;parent.appendChild(el);});
    }""")
    assert page.locator("svg").count() == 2
    assert page.locator("svg").nth(1).locator("circle").count() == 2
    assert (
        page.locator("svg").nth(1).locator("line").count() == 1
    )  # axis only; no bridge across null
    assert page.get_by_text("Feb: Missing").count() == 1
    assert page.get_by_text("Table opaque").count() == 1
    assert page.get_by_text("This view is unavailable.", exact=True).count() == 1
    assert page.locator("img").count() == 0
    assert page.evaluate("window.pwned === undefined")


def test_history_and_refresh_replace_whole_document_without_legacy_fields(page):
    import json

    from playwright.sync_api import expect

    old = {
        "schema_version": 1,
        "answer_id": "answer-0",
        "revision": 1,
        "takeaway": "Original answer",
        "sections": [],
        "sources": {},
    }
    fresh = {
        **old,
        "revision": 2,
        "takeaway": "Refreshed answer",
        "sections": [
            {
                "title": "Headcount",
                "presentation": {"kind": "figure", "label": "Employees", "value": "7"},
            }
        ],
    }
    refreshes = []

    def route(request):
        path = request.request.url.split("http://test", 1)[-1]
        if path == "/":
            request.fulfill(path=str(ROOT / "ui/static/index.html"), content_type="text/html")
        elif path == "/answer-document.js":
            request.fulfill(
                path=str(ROOT / "ui/static/answer-document.js"),
                content_type="application/javascript",
            )
        elif path == "/api/session":
            request.fulfill(json={"session_id": "test-session"})
        elif path.startswith("/api/history"):
            request.fulfill(json={"turns": [{"question": "Headcount?", "answer_document": old}]})
        elif path == "/api/answer/refresh":
            refreshes.append(request.request.post_data_json)
            if len(refreshes) == 1:
                request.fulfill(
                    content_type="text/event-stream",
                    body="event: result\ndata: "
                    + json.dumps({"status": "done", "answer_document": fresh})
                    + "\n\n",
                )
            else:
                request.fulfill(
                    content_type="text/event-stream",
                    body="event: error\ndata: "
                    + json.dumps(
                        {"code": "RefreshRejectedError", "message": "Refresh was not approved."}
                    )
                    + "\n\n",
                )
        else:
            request.fulfill(status=404, json={})

    page.route("http://test/**", route)
    page.goto("http://test/")
    expect(page.get_by_text("Original answer", exact=True)).to_be_visible()
    page.get_by_role("button", name="Refresh answer").click()
    expect(page.get_by_text("Refreshed answer", exact=True)).to_be_visible()
    assert page.get_by_text("Original answer", exact=True).count() == 0
    page.get_by_role("button", name="Refresh answer").click()
    expect(page.get_by_text("Refresh was not approved.", exact=True)).to_be_visible()
    expect(page.get_by_text("Refreshed answer", exact=True)).to_be_visible()
    assert [r["expected_revision"] for r in refreshes] == [1, 2]
    page.screenshot(path="/tmp/lane5-answer-refresh-ui.png", full_page=True)


def test_mixed_freshness_does_not_assign_payroll_date_to_live_employee(page):
    page.set_content('<main></main>')
    page.add_script_tag(path=str(ROOT / 'ui/static/answer-document.js'))
    label = page.evaluate('''() => window.answerFreshnessLabel({retrieved_at:'2026-10-30T12:00:00Z',data_as_of:'2026-10-12T05:30:00Z',freshness:{mode:'mixed',sources:[{table:'dbpcm_warehouse.employee',kind:'live'},{table:'dbpcm_warehouse.payroll',kind:'ingested',data_as_of:'2026-10-12T05:30:00Z'}]}})''')
    assert label == 'employee: live; payroll: data as of 2026-10-12T05:30:00Z'
