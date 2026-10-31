from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('failure', ['replacement', 'replacement_repeat', 'expired', 'denied'])
def test_snapshot_recovery_resets_rows_and_is_bounded(failure):
    calls = []
    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        page = browser.new_page()
        def route(route):
            path = route.request.url.split('http://test', 1)[-1]
            if path == '/':
                route.fulfill(path=str(ROOT/'ui/static/index.html'),content_type='text/html')
            elif path == '/answer-document.js':
                route.fulfill(path=str(ROOT/'ui/static/answer-document.js'),content_type='application/javascript')
            elif path == '/api/session':
                route.fulfill(json={'session_id':'session'})
            elif path.startswith('/api/history'):
                route.fulfill(json={'turns':[{'question':'Pay?', 'answer_document':{
                    'schema_version':2,'answer_id':'a','revision':1,'takeaway':'Pay',
                    'sections':[{'title':'Pay','presentation':{'kind':'table','result_id':'q'}}],
                    'sources':{'q':{'sql':'SELECT pay FROM employee','column_formats':{'Pay':{'kind':'currency','currency':'USD'}}}}
                }}]})
            elif path == '/api/query/page':
                calls.append(route.request.post_data_json)
                if len(calls)==2 or failure=='replacement_repeat' and len(calls)==3:
                    if failure.startswith('replacement'):
                        route.fulfill(status=409,headers={'Retry-After':'0'},json={'detail':{'code':'SNAPSHOT_REPLACED'},'reset_table':True,'snapshot':{'snapshot_id':'new'}})
                    else:
                        route.fulfill(status=410 if failure=='expired' else 403,json={'detail':{'code':'SNAPSHOT_EXPIRED' if failure=='expired' else 'ACCESS_DENIED'}})
                else:
                    route.fulfill(json={'snapshot_id':'old' if len(calls)==1 else 'new','columns':['Pay'],'rows':[[10 if len(calls)==1 else 20]],'has_more':len(calls)==1})
            else:
                route.fulfill(json={})
        page.route('http://test/**', route)
        page.goto('http://test/')
        card=page.locator('.turn-answer .answer-section')
        expect(card.get_by_text('Pay', exact=True)).to_have_count(1)
        panel=page.locator('.turn-answer .result-table')
        panel.locator('summary').click()
        expect(panel.locator('tbody')).to_contain_text('$10.00')
        panel.locator('.pager-next').click()
        if failure in {'denied','replacement_repeat'}:
            expect(panel.locator('.result-table-caption')).to_contain_text('Could not load')
            assert panel.locator('tbody').inner_text()==''
            assert len(calls)==(3 if failure=='replacement_repeat' else 2)
        else:
            if failure=='expired':
                expect(panel.get_by_role('button',name='Refresh data')).to_be_visible()
                assert len(calls)==2
                panel.get_by_role('button',name='Refresh data').click()
            expect(panel.locator('tbody')).to_contain_text('$20.00')
            assert '$10.00' not in panel.locator('tbody').inner_text()
            assert calls[2]['offset']==0
            if failure.startswith('replacement'):
                assert calls[2]['snapshot_id']=='new' and 'sql' not in calls[2]
            else:
                assert calls[2]['sql']=='SELECT pay FROM employee' and 'snapshot_id' not in calls[2]
            assert all('column_formats' not in c for c in calls)
        browser.close()
