import json

from data_agent.runtime.dispatch.sql_diagnostics import sql_diagnostic
from data_agent.runtime.loop.agent_loop import _tool_trail_entry_to_canonical


def test_failure_projection_keeps_safe_cause_and_drops_repeated_context():
    packet = dict(version=1, code='SQL_EXECUTION_ERROR', input_dialect='clickhouse',
                  cause='UNSUPPORTED_METHOD', code_num=1, message='Rejected.',
                  guidance='Write ClickHouse SQL using logical catalog tables. Inspect getTableSchema and correct the submitted query before retrying; explainQuery applies the same validation. ClickHouse does not support that operation on this table engine.',
                  retry='after_query_change', candidate_expression='query context; exact failure location unavailable: SELECT ...')
    diagnostic = sql_diagnostic(json.dumps(packet), 'SELECT 1')
    entry = dict(tool_call_id='q1', tool_name='runQuery', args={'sql': 'SELECT 1'}, status='denied',
                 error_code='SQL_EXECUTION_ERROR', sql_diagnostic=diagnostic)
    rendered = _tool_trail_entry_to_canonical(entry)[-1]
    content = json.loads(rendered['content'])
    assert rendered['tool_call_id'] == 'q1'
    assert content == {
        'tool_name': 'runQuery', 'status': 'denied', 'error_code': 'SQL_EXECUTION_ERROR',
        'retryable': True, 'sql_diagnostic': {
            'input_dialect': 'clickhouse', 'cause': 'UNSUPPORTED_METHOD', 'code_num': 1,
            'hint': 'ClickHouse does not support that operation on this table engine.'}}
    assert len(rendered['content']) < 400
    # Projection does not erase the internal recovery condition or audit context.
    assert diagnostic['retry'] == 'after_query_change'
    assert 'candidate_expression' in diagnostic


def test_untrusted_cause_and_numeric_pair_are_not_copied():
    packet = dict(version=1, code='SQL_EXECUTION_ERROR', input_dialect='clickhouse',
                  cause='private_view_secret', code_num=1, retry='after_query_change')
    assert 'cause' not in sql_diagnostic(json.dumps(packet), 'SELECT 1')
    packet.update(cause='UNSUPPORTED_METHOD', code_num=999)
    assert 'code_num' not in sql_diagnostic(json.dumps(packet), 'SELECT 1')
