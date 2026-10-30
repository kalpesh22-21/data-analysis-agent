import httpx
import pytest

from scripts._probe_transport import capture_turn, tool_telemetry


class Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b': heartbeat\r\n\r\nevent: progress\r\ndata: {"turn_id":"t",'
        yield b'"sequence":1,"phase":"checking_answer","message":"Checking results"}\r\n\r\n'
        yield b'event: result\r\ndata: {"status":"paused_ask_user",\r\ndata: "pending_question":{"question":"Which period?"}}\r\n\r\n'


async def test_https_capture_reads_through_heartbeat_to_pause():
    def handler(request):
        assert str(request.url) == 'https://lane.test/turn'
        assert request.headers['X-Session-Id'] == 's'
        return httpx.Response(200, stream=Chunks())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        status, events, result, error = await capture_turn(
            client, 'https://lane.test/', headers={'X-Session-Id': 's'}, message='Payroll')
    assert status == 200 and error is None
    assert result['status'] == 'paused_ask_user'
    assert events[0]['data']['phase'] == 'checking_answer'
    assert len(events) == 2


@pytest.mark.parametrize('status,body,code', [
    (200, ': heartbeat\n\n', 'MISSING_TERMINAL_EVENT'),
    (200, 'event: result\ndata: invalid\n\n', 'INVALID_SSE'),
    (403, 'private upstream error', 'HTTP_ERROR'),
    (200, 'event: error\ndata: {"code":"DEPENDENCY_FAILURE"}\n\n', 'DEPENDENCY_FAILURE'),
])
async def test_incomplete_or_failed_stream_is_not_success(status, body, code):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(status, text=body))) as client:
        _, _, result, error = await capture_turn(client, 'https://lane.test', headers={}, message='Q')
    assert result is None and error['code'] == code
    assert 'private' not in str(error)


async def test_connection_failure_reports_only_exception_class():
    def fail(_):
        raise httpx.ConnectError('private credentials')
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        status, _, _, error = await capture_turn(client, 'https://lane.test', headers={}, message='Q')
    assert status is None
    assert error == {'code': 'STREAM_READ_FAILED', 'exception_type': 'ConnectError'}


def test_tool_order_comes_from_traces_and_missing_spans_are_unknown():
    spans = [
        {'start_time': '2026-10-30T02:00:00', 'attributes': {'tool.name': 'runQuery'}},
        {'start_time': '2026-10-30T01:00:00', 'attributes': {'tool.name': 'getBlueprint'}},
    ]
    assert tool_telemetry(spans)['observed_tool_order'] == ['getBlueprint', 'runQuery']
    assert tool_telemetry([])['status'] == 'unavailable'
