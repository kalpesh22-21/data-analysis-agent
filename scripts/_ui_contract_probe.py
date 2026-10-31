"""Live schema-v2 and saved-result checks for the opt-in conversational harness."""
import csv
import io
from urllib.parse import urlsplit


def validate_document(document, executed_sql):
    from data_agent.runtime.provenance.formats import validate_formats
    assert document['schema_version'] == 2
    sources = document['sources']
    required = {
        'figure': {'result_id', 'column', 'label'},
        'facts': {'result_id', 'columns'},
        'table': {'result_id'},
        'bars': {'result_id', 'y_column', 'label'},
        'line': {'result_id', 'x_column', 'y_column', 'label'},
        'text': {'markdown'},
    }
    for source in sources.values():
        assert source['sql'] in executed_sql
        assert not {'table_ref', 'rows', 'points', 'items'} & source.keys()
        assert isinstance(source['column_formats'], dict)
        validate_formats(source['column_formats'])
    for section in document['sections']:
        assert not {'supporting_data', 'supporting_table', 'fallback', 'fallback_table'} & section.keys()
        presentation = section['presentation']
        kind = presentation['kind']
        assert required[kind] <= presentation.keys()
        assert not {'rows', 'value', 'formatted_value', 'format', 'points', 'items'} & presentation.keys()
        if kind != 'text':
            assert presentation['result_id'] in sources
        if kind == 'bars':
            assert ('x_column' in presentation) != ('x_columns' in presentation)


async def verify_saved_results(client, headers, document):
    """Exercise real Lane5 HTTP and existing API storage, with no mocked responses."""
    checks, results = {}, {}
    for rid, source in document.get('sources', {}).items():
        try:
            response = await client.post('/query/page', headers=headers,
                                         json={'sql':source['sql'], 'limit':2, 'offset':0})
            response.raise_for_status()
            first = response.json()
            # Empty exports may have no header metadata; formats describe the
            # executed source, not rows that exist in this empty snapshot.
            if first['columns'] or first['row_count']:
                assert set(source.get('column_formats',{})) <= set(first['columns'])
            else:
                assert first['rows'] == [] and first['has_more'] is False
            sid = first['snapshot_id']
            rows = list(first['rows'])
            page = first
            while page['has_more']:
                response = await client.post('/query/page', headers=headers,
                    json={'snapshot_id':sid, 'limit':2, 'offset':len(rows)})
                response.raise_for_status()
                page = response.json()
                assert page['snapshot_id'] == sid and page['rows']
                rows.extend(page['rows'])
                assert len(rows) <= 10000, 'Probe row budget exceeded'
            assert len(rows) == first['row_count']
            # Fetching source SQL again deduplicates; it does not mint another snapshot.
            repeat = await client.post('/query/page', headers=headers,
                                       json={'sql':source['sql'], 'limit':2})
            repeat.raise_for_status()
            assert repeat.json()['snapshot_id'] == sid
            urls = await client.get(f'/query/snapshots/{sid}/download-urls', headers=headers)
            urls.raise_for_status()
            parts = urls.json()['files']
            csv_rows = 0
            csv_values = []
            for part in parts:
                url = part['url']
                if urlsplit(url).scheme:
                    # Presigned links must not receive the session bearer token.
                    downloaded = await client.get(url)
                else:
                    downloaded = await client.get('/'+url.lstrip('/'), headers=headers)
                downloaded.raise_for_status()
                parsed = list(csv.reader(io.StringIO(downloaded.text)))
                assert parsed and len(parsed[0]) == len(first['columns'])
                csv_rows += len(parsed)-1
                csv_values.extend(parsed[1:])
            assert csv_rows == first['row_count']
            for index, column in enumerate(first['columns']):
                if source.get('column_formats',{}).get(column,{}).get('kind') == 'identifier':
                    assert [row[index] for row in csv_values] == [str(row[index]) if row[index] is not None else '' for row in rows]
            refreshed = await client.post('/query/page', headers=headers,
                json={'sql':source['sql'], 'refresh_of':sid, 'limit':2, 'offset':0})
            refreshed.raise_for_status()
            replacement = refreshed.json()
            assert replacement['reset_table'] is True and replacement['snapshot_id'] != sid
            replay = await client.post('/query/page', headers=headers,
                json={'sql':source['sql'], 'refresh_of':sid, 'limit':2, 'offset':0})
            replay.raise_for_status()
            assert replay.json()['snapshot_id'] == replacement['snapshot_id']
            results[rid] = {'first_page': first, 'all_rows': rows, 'csv_parts':len(parts),
                            'csv_rows':csv_rows, 'csv_values':csv_values, 'refresh_snapshot_id':replacement['snapshot_id']}
            checks['saved_result_'+rid] = True
        except Exception as exc:
            checks['saved_result_'+rid] = False
            results[rid] = {'error_type':type(exc).__name__, 'error':str(exc)}
    try:
        results["reference_ui"] = await verify_reference_ui(client, headers, document)
        checks["reference_ui_live_pages_and_controls"] = True
    except Exception as exc:
        results["reference_ui"] = {"error_type":type(exc).__name__, "error":str(exc)}
        checks["reference_ui_live_pages_and_controls"] = False
    return checks, results


async def verify_reference_ui(client, headers, document):
    """Load actual reference UI assets; bridge its BFF calls to the live runtime."""
    from pathlib import Path

    from playwright.async_api import async_playwright, expect

    root = Path(__file__).resolve().parents[1]
    requests, errors, controls = [], [], []
    async with async_playwright() as driver:
        browser = await driver.chromium.launch()
        page = await browser.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        async def bridge(route):
            parsed = urlsplit(route.request.url)
            path = parsed.path
            if path == '/':
                await route.fulfill(path=str(root/'ui/static/index.html'), content_type='text/html')
            elif path == '/answer-document.js':
                await route.fulfill(path=str(root/'ui/static/answer-document.js'), content_type='application/javascript')
            elif path == '/api/session':
                await route.fulfill(json={'session_id':headers['X-Session-Id']})
            elif path == '/api/history':
                response = await client.get('/session/history', headers=headers)
                await route.fulfill(status=response.status_code, json=response.json())
            elif path == '/api/query/page':
                body = route.request.post_data_json
                body.pop('session_id', None)
                assert 'column_formats' not in body
                requests.append({'path':path, 'body':body})
                response = await client.post('/query/page', headers=headers, json=body)
                await route.fulfill(status=response.status_code, json=response.json())
            elif path.startswith('/api/query/snapshots/'):
                from urllib.parse import parse_qs
                query = {k:v[0] for k,v in parse_qs(parsed.query).items() if k != 'session_id'}
                requests.append({'path':path, 'params':query})
                response = await client.get(path[4:], params=query, headers=headers)
                await route.fulfill(status=response.status_code, body=response.content,
                    headers={k:v for k,v in response.headers.items() if k in {'content-type','content-disposition'}})
            else:
                await route.fulfill(json={})
        await page.route('http://contract.test/**', bridge)
        try:
            await page.goto('http://contract.test/')
            cards = page.locator('.turn-answer .answer-section')
            await expect(cards).to_have_count(len(document['sections']), timeout=30000)
            for index, section in enumerate(document['sections']):
                card = cards.nth(index)
                kind = section['presentation']['kind']
                if kind == 'figure':
                    await expect(card.locator('strong')).not_to_have_count(0, timeout=30000)
                    assert await card.locator('details').count() == 0
                    controls.append({'kind':kind, 'table_csv':False})
                elif kind != 'text':
                    grid = card.locator('.result-table')
                    await expect(grid).to_have_count(1, timeout=30000)
                    if kind in {'facts','bars','line'}:
                        outer = card.locator('details').first
                        if not await outer.evaluate('(el)=>el.open'):
                            await outer.locator('summary').first.click()
                    if kind == 'table' and section.get('title'):
                        assert await card.get_by_text(section['title'], exact=True).count() == 1
                    if not await grid.evaluate('(el)=>el.open'):
                        await grid.locator('summary').first.click()
                    download = grid.get_by_role('button', name='Download CSV parts')
                    await expect(download).to_be_visible(timeout=30000)
                    await download.click()
                    rid = section['presentation']['result_id']
                    if document['sources'][rid].get('row_count',0):
                        link = grid.locator('a').first
                        await expect(link).to_be_visible(timeout=30000)
                        async with page.expect_download() as downloading:
                            await link.click()
                        received = await downloading.value
                        assert await received.failure() is None
                    controls.append({'kind':kind, 'table_csv':True})
            assert not errors, errors
            return {'controls':controls, 'requests':requests, 'page_errors':errors,
                    'rendered_text':await page.locator('.turn-answer').inner_text()}
        finally:
            await browser.close()
