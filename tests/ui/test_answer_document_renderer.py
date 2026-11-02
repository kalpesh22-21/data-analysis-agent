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


@pytest.mark.parametrize('kind', ['multi_series_line','grouped_bars','stacked_bars','heatmap','donut'])
def test_opt_in_chart_rendering_with_shared_table_data(page, kind):
    page.set_content('<main></main>')
    page.add_script_tag(path=str(ROOT / 'ui/static/answer-document.js'))
    page.evaluate('''async kind => {
      const p={kind,result_id:'r',x_column:'Month',label:'Earnings',series:[{y_column:'Regular',label:'Regular'},{y_column:'Overtime',label:'Overtime'}]};
      if(kind==='stacked_bars')p.additive=true;
      if(kind==='donut'){delete p.series;p.y_column='Regular';p.composition=true;}
      if(kind==='heatmap'){delete p.series;p.y_column='Department';p.value_column='Regular';}
      window.grids=[];
      await window.renderAnswerDocument(document.querySelector('main'),{schema_version:2,takeaway:'Results',sections:[{id:'s',title:'Earnings',presentation:p}],sources:{r:{sql:'approved',column_formats:{Regular:{kind:'currency',currency:'USD'},Overtime:{kind:'currency',currency:'USD'}}}}},
        (el,sql)=>{window.grids.push(sql);const t=document.createElement('div');t.textContent='Approved table';el.appendChild(t);},
        async()=>({columns:['Month','Department','Regular','Overtime'],rows:[['2026-01','Sales',10,2],['2026-02','Engineering',20,3]],has_more:false}));
    }''', kind)
    assert page.locator('svg').count() == 1
    assert page.locator('svg').get_attribute('data-chart-kind') == kind
    assert page.get_by_text('View data / CSV').count() == 1
    assert page.evaluate('window.grids') == ['approved']
    assert page.get_by_text('This visualization is unavailable; showing its approved table.').count() == 0


@pytest.mark.parametrize('failure', ['partial','negative','too_many','duplicate','null'])
def test_donut_fails_to_same_table_without_dropping_rows(page, failure):
    page.set_content('<main></main>')
    page.add_script_tag(path=str(ROOT / 'ui/static/answer-document.js'))
    page.evaluate('''async failure => {
      let rows=[['Sales',10],['Engineering',20]];
      if(failure==='negative')rows[1][1]=-1;
      if(failure==='null')rows[1][1]=null;
      if(failure==='duplicate')rows[1][0]='Sales';
      if(failure==='too_many')rows=Array.from({length:6},(_,i)=>['D'+i,i+1]);
      await window.renderAnswerDocument(document.querySelector('main'),{schema_version:2,takeaway:'Results',sections:[{id:'s',presentation:{kind:'donut',result_id:'r',x_column:'Department',y_column:'Count',label:'Headcount',composition:true}}],sources:{r:{sql:'approved'}}},
        (el,sql)=>{const t=document.createElement('div');t.textContent=sql;el.appendChild(t);},async()=>({columns:['Department','Count'],rows,has_more:failure==='partial'}));
    }''', failure)
    assert page.locator('svg').count() == 0
    assert page.get_by_text('This visualization is unavailable; showing its approved table.').count() == 1
    assert page.get_by_text('approved', exact=True).count() == 1


@pytest.mark.parametrize('failure', ['null_gap', 'duplicate_period', 'format_conflict', 'negative_stack', 'too_many_series'])
def test_multi_series_guards_and_null_gaps(page, failure):
    page.set_content('<main></main>')
    page.add_script_tag(path=str(ROOT / 'ui/static/answer-document.js'))
    page.evaluate('''async failure => {
      const rows=[['2026-01',10,2],['2026-02',null,3],['2026-03',20,4]];
      const formats={A:{kind:'currency',currency:'USD'},B:{kind:'currency',currency:'USD'}};
      const p={kind:'multi_series_line',result_id:'r',x_column:'Month',label:'Earnings',series:[{y_column:'A',label:'Regular'},{y_column:'B',label:'Overtime'}]};
      if(failure==='duplicate_period')rows[1][0]='2026-01';
      if(failure==='format_conflict')formats.B.currency='EUR';
      if(failure==='negative_stack'){p.kind='stacked_bars';p.additive=true;rows[1][1]=-1;}
      if(failure==='too_many_series')p.series=Array.from({length:5},()=>p.series[0]);
      await window.renderAnswerDocument(document.querySelector('main'),{schema_version:2,takeaway:'Results',sections:[{id:'s',presentation:p}],sources:{r:{sql:'approved',column_formats:formats}}},
        (el,sql)=>{const t=document.createElement('div');t.textContent=sql;el.appendChild(t);},
        async()=>({columns:['Month','A','B'],rows,has_more:false}));
    }''', failure)
    if failure == 'null_gap':
        assert page.locator('svg circle').count() == 5
        # Axis plus two segments for B; no segment bridges A's null.
        assert page.locator('svg line').count() == 3
        assert page.get_by_text('Regular / 2026-02: Not available', exact=True).count() == 1
    else:
        assert page.locator('svg').count() == 0
        assert page.get_by_text('This visualization is unavailable; showing its approved table.').count() == 1
