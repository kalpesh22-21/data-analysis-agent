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
    page.evaluate("""async () => {
      const source={sql:'SELECT Month, Amount FROM t', column_formats:{Amount:{kind:'currency',currency:'USD'}}};
      const sections = [
        {title:'Figure',presentation:{kind:'figure',result_id:'one',column:'Amount',label:'Pay'}},
        {title:'Facts',presentation:{kind:'facts',result_id:'one',columns:['Amount']}},
        {title:'Bars',presentation:{kind:'bars',result_id:'bars',x_column:'Month',y_column:'Amount',label:'Pay'}},
        {title:'Line',presentation:{kind:'line',result_id:'line',x_column:'Month',y_column:'Amount',label:'Pay'}},
        {title:'Broken',presentation:{kind:'bars',result_id:'broken',x_column:'Month',y_column:'missing',label:'Pay'}},
        {title:'Unavailable',presentation:{kind:'unknown'}},
      ];
      const sources={one:{...source,sql:'one'},bars:{...source,sql:'bars'},line:{...source,sql:'line'},broken:{...source,sql:'broken'}};
      window.loads=[];
      await window.renderAnswerDocument(document.querySelector('main'),{
        schema_version:2,takeaway:'<img src=x onerror="window.pwned=true">',sections,sources
      }, (parent,sql) => {const el=document.createElement('p');el.textContent='Table '+sql;parent.appendChild(el);}, async source => {
        window.loads.push(source.sql);
        return {snapshot_id:'saved',columns:['Month','Amount'],column_formats:{},rows:source.sql==='one'?[['2026-01',7]]:source.sql==='line'?[['2026-01',1],['2026-02',null],['2026-03',3]]:[['2026-01',3],['2026-02',4]],has_more:false};
      });
    }""")
    assert page.locator(".answer-section").nth(0).locator("details").count() == 0
    assert page.locator(".answer-section").nth(1).get_by_text("View data / CSV").count() == 1
    assert page.locator("svg").count() == 2
    assert page.locator("svg").nth(1).locator("circle").count() == 2
    assert page.locator("svg").nth(1).locator("line").count() == 1
    assert page.get_by_text("2026-02: Missing").count() == 1
    assert page.get_by_text("This view is unavailable.", exact=True).count() == 1
    assert page.locator("img").count() == 0
    assert page.evaluate("window.pwned === undefined")
    assert page.evaluate("window.loads.filter(x=>x==='one').length") == 1
    assert page.get_by_text("$7.00", exact=True).count() == 2


def test_history_and_refresh_replace_whole_document_without_legacy_fields(page):
    import json

    from playwright.sync_api import expect

    old = {
        "schema_version": 2,
        "answer_id": "answer-0",
        "revision": 1,
        "takeaway": "Original answer",
        "sections": [],
        "sources": {},
    }
    fresh = {
        **old,
        "revision": 2,
        "sources": {"q": {"sql": "SELECT 7 AS n", "column_formats": {"n": {"kind": "integer"}}}},
        "takeaway": "Refreshed answer",
        "sections": [
            {
                "title": "Headcount",
                "presentation": {"kind": "figure", "result_id": "q", "label": "Employees", "column": "n"},
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
        elif path == "/api/query/page":
            request.fulfill(json={"snapshot_id":"saved","columns":["n"],"rows":[[7]],"has_more":False})
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


@pytest.mark.parametrize("value,fmt,expected", [
    (1.5,{"kind":"integer"},"1.5"),
    ("",{"kind":"number"},""),
    (True,{"kind":"number"},"true"),
    ("1.123456789",{"kind":"number"},"1.123456789"),
    (75039,{"kind":"identifier"},"75039"),
    ("02108",{"kind":"identifier"},"02108"),
    ("1766987.30",{"kind":"currency","currency":"USD"},"$1,766,987.30"),
    (0.25,{"kind":"percent","scale":"ratio"},"25%"),
    ("9007199254740993",{"kind":"identifier"},"9007199254740993"),
    (None,{"kind":"integer"},"Not available"),
])
def test_source_formatting_preserves_values(page,value,fmt,expected):
    page.add_script_tag(path=str(ROOT / "ui/static/answer-document.js"))
    assert page.evaluate("([value,fmt]) => formatAnswerCell(value,fmt)",[value,fmt]) == expected
