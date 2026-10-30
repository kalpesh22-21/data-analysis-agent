"""HTTP turn capture for probes; progress is UI text, never tool telemetry."""

import asyncio
import json


def parse_events(text):
    events = []
    event, data = 'message', []
    for line in [*text.splitlines(), '']:
        if not line:
            if data:
                events.append({'event': event, 'data': json.loads('\n'.join(data))})
            event, data = 'message', []
        elif line.startswith('event:'):
            event = line[6:].strip()
        elif line.startswith('data:'):
            data.append(line[5:].lstrip(' '))
    return events


async def capture_turn(client, base_url, *, headers, message):
    chunks = []
    status = None
    failure = None
    try:
        async with asyncio.timeout(300):
            async with client.stream('POST', base_url.rstrip('/') + '/turn',
                                     headers=headers, json={'message': message}) as response:
                status = response.status_code
                async for chunk in response.aiter_text():
                    chunks.append(chunk)
    except Exception as exc:
        # Never persist exception messages: they can contain request credentials.
        failure = {'code': 'STREAM_READ_FAILED', 'exception_type': type(exc).__name__}
    try:
        events = parse_events(''.join(chunks)) if status == 200 else []
    except (ValueError, TypeError):
        events = []
        failure = failure or {'code': 'INVALID_SSE'}
    result = next((e['data'] for e in reversed(events) if e['event'] == 'result'), None)
    error = next((e['data'] for e in reversed(events) if e['event'] == 'error'), None)
    if failure:
        error = failure
    elif status != 200:
        error = {'code': 'HTTP_ERROR', 'http_status': status}
    elif result is None and error is None:
        error = {'code': 'MISSING_TERMINAL_EVENT'}
    return status, events, result, error


def tool_telemetry(spans):
    tools = [s['attributes']['tool.name'] for s in sorted(spans, key=lambda s: s.get('start_time', ''))
             if s.get('attributes', {}).get('tool.name')]
    # A bounded trace fetch may omit tools even when the root span is present.
    return {'status': 'observed' if tools else 'unavailable', 'source': 'phoenix',
            'observed_tool_order': tools}
