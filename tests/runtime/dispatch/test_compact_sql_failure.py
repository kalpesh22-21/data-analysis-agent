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


def test_saved_receipt_and_canonical_projection_agree_without_losing_retry_state():
    from dataclasses import replace
    from data_agent.runtime.context.budget import render_entry, estimate_message_tokens, fit_request_to_budget
    from data_agent.runtime.dispatch.sql_diagnostics import encode_diagnostic, decode_diagnostic, compact_model_diagnostic
    from tests.runtime.loop.test_repository_regressions import entry

    packet = dict(version=1, code='SQL_EXECUTION_ERROR', input_dialect='clickhouse',
                  cause='UNSUPPORTED_METHOD', code_num=1, message='Rejected.',
                  guidance='Check correlated scalar subqueries.', retry='after_query_change',
                  candidate_expression='private-audit-placeholder' * 100)
    receipt = replace(entry('q1', 'runQuery', status='denied', provenance=None),
                      error_code=packet['code'], denial_detail=encode_diagnostic(json.dumps(packet), 'SELECT 1'))
    receipt = type(receipt).from_doc(receipt.to_doc())
    full = decode_diagnostic(receipt.denial_detail)
    projected = render_entry(receipt, 5)
    canonical = _tool_trail_entry_to_canonical(projected)
    content = json.loads(canonical[-1]['content'])
    assert content['sql_diagnostic'] == projected['sql_diagnostic'] == compact_model_diagnostic(full)
    assert compact_model_diagnostic(projected['sql_diagnostic']) == projected['sql_diagnostic']
    assert content['retryable'] is True
    assert 'private-audit-placeholder' not in json.dumps(projected)
    assert full['candidate_expression'] and full['retry'] == 'after_query_change'
    # The actual request budget sees the compact canonical packet, not the audit receipt.
    messages = [{'role': 'system', 'content': 'Policy'}, *canonical,
                {'role': 'user', 'content': 'Continue'}]
    budget = sum(estimate_message_tokens(m) for m in messages)
    fit = fit_request_to_budget(messages, token_budget=budget)
    assert fit.dropped_messages == 0
    assert fit.kept_tokens == budget


def test_failure_policy_is_hoisted_once_for_structured_and_other_denials():
    from data_agent.runtime.prompts import AGENT_SYSTEM_PROMPT, FAILURE_EVIDENCE_GUIDANCE
    assert AGENT_SYSTEM_PROMPT.count(FAILURE_EVIDENCE_GUIDANCE) == 1
    assert 'SQL_REPAIR_EXHAUSTED can support EXECUTION_FAILED' in FAILURE_EVIDENCE_GUIDANCE
    assert 'Permission denials can support NO_ACCESS' in FAILURE_EVIDENCE_GUIDANCE
    assert 'No failed tool proves data is absent' in FAILURE_EVIDENCE_GUIDANCE
    for code in ('SQL_EXECUTION_ERROR', 'SQL_REPAIR_EXHAUSTED', 'COLUMN_SCOPE_VIOLATION'):
        entry = dict(tool_call_id='failure', tool_name='runQuery', args={}, status='denied', error_code=code)
        content = json.loads(_tool_trail_entry_to_canonical(entry)[-1]['content'])
        assert 'evidence_usage' not in content
        assert content['error_code'] == code
