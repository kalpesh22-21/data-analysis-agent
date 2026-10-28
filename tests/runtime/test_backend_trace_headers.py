from opentelemetry import baggage, context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from data_agent.runtime.mcp._transport import side_channel_headers, auth_headers


def test_backend_headers_carry_current_span_but_not_baggage():
    provider = TracerProvider()
    tracer = provider.get_tracer('test')
    token = context.attach(baggage.set_baggage('private', 'never-forward'))
    try:
        with tracer.start_as_current_span('runQuery') as span:
            headers = side_channel_headers('test-token', 'session')
            expected = {}
            TraceContextTextMapPropagator().inject(expected)
            assert headers['traceparent'] == expected['traceparent']
            assert headers['Authorization'] == 'Bearer test-token'
            assert headers['X-Session-Id'] == 'session'
            assert 'baggage' not in headers
            assert auth_headers(service_key='service',jwt='',session_id='')['traceparent'] == expected['traceparent']
        assert 'traceparent' not in side_channel_headers('test-token','session')
    finally:
        context.detach(token)
        provider.shutdown()
