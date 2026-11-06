"""Actual blueprint execution survives persistence, canonical rendering and judge fallback."""
import json

import pytest

from data_agent.runtime.context.blueprint_execution import (
    blueprint_execution_receipt,
    model_execution,
)
from data_agent.runtime.context.budget import _render_entry
from data_agent.runtime.loop.agent_loop import _tool_trail_entry_to_canonical
from data_agent.runtime.loop.answer_judge import _POST_EXECUTION_PROMPT, JudgeBrief
from data_agent.runtime.loop.judge_evidence import enrich_brief
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import TrailEntry
from tests.runtime.loop.test_judge_evidence import CREDS, catalog


def receipt():
    return blueprint_execution_receipt('runBlueprint', 'ok', {
        'blueprint_id': 'bp-actual',
        'terminal_sql': 'SELECT count(id) FROM hr.employee WHERE TRUE',
        'sql': ['SELECT count(id) FROM hr.employee WHERE TRUE'],
        'bound_slots': {'order_by': [{'column': 'Count', 'direction': 'DESC'}]},
        'omitted_slots': ['department'],
        'preview_rows': [['private extra row']], 'unrelated': 'not included',
    })


def entry():
    return TrailEntry(turn_index=0, tool_call_id='q', tool_name='runBlueprint',
                      args={'id': 'requested-id', 'slot_bindings': {}}, status='ok', error_code=None,
                      provenance=frozenset({('hr.employee', 'id')}), result_preview=None,
                      result_full_ref='unavailable', ts='now', blueprint_execution=receipt())


def test_execution_receipt_survives_history_and_model_projection():
    stored = TrailEntry.from_doc(entry().to_doc())
    rendered = _render_entry(stored, 5)
    content = json.loads(_tool_trail_entry_to_canonical(rendered)[-1]['content'])
    assert content['blueprint_id'] == 'bp-actual'
    assert content['execution'] == model_execution(receipt())
    assert content['execution']['terminal_sql'].endswith('TRUE')
    assert 'private extra row' not in json.dumps(content)
    assert 'unrelated' not in json.dumps(content)
    assert set(content['execution']) == {'terminal_sql'}
    assert '\n' in content['execution']['terminal_sql']


@pytest.mark.parametrize('tool,status', [('runQuery', 'ok'), ('runBlueprint', 'denied'), ('runBlueprint', 'error')])
def test_no_execution_claim_for_non_blueprint_or_failed_call(tool, status):
    assert blueprint_execution_receipt(tool, status, receipt()) is None


def test_legacy_or_malformed_history_does_not_invent_execution():
    doc = entry().to_doc()
    for value in (None, [], {'blueprint_id': 'fake'}):
        doc['blueprint_execution'] = value
        rendered = _render_entry(TrailEntry.from_doc(doc), 5)
        assert 'execution' not in rendered


async def test_judge_uses_persisted_execution_when_full_result_is_unavailable():
    brief = await enrich_brief(
        JudgeBrief('exit_table', 'Headcount', results=({'tool_call_id': 'q'},)),
        trail=[entry()], turn_index=0, session_id=CREDS.session_id,
        store=InMemorySessionStore(), catalog_provider=catalog(), credentials=CREDS, analysis_state=None,
    )
    execution = brief.results[0]['execution']
    assert brief.results[0]['blueprint_id'] == 'bp-actual'
    assert execution['terminal_sql'].endswith('TRUE')
    assert set(execution) == {'terminal_sql'}


def test_judge_prompt_prioritizes_execution_over_blueprint_label():
    assert 'execution.terminal_sql' in _POST_EXECUTION_PROMPT
    assert 'Do not approve or reject by blueprint name' in _POST_EXECUTION_PROMPT
    assert 'a fetched blueprint is not proof that it ran' in _POST_EXECUTION_PROMPT


def test_composed_receipt_has_formatted_ordered_steps_without_metadata():
    full = {**receipt(), 'sql': ['SELECT 1 AS x', 'SELECT 2 AS y']}
    projected = model_execution(full)
    assert list(projected) == ['steps']
    assert [s['step'] for s in projected['steps']] == [1, 2]
    assert '1 AS x' in projected['steps'][0]['sql']
    assert '2 AS y' in projected['steps'][1]['sql']
    assert '\n' in projected['steps'][0]['sql']


async def test_scalar_lineage_reaches_judge_from_persisted_receipt_not_agent_sql_view():
    original = entry()
    original.blueprint_execution.update({
        'sql': ['SELECT avg(id) AS average FROM hr.employee',
                'SELECT id FROM hr.employee WHERE id > 107000'],
        'terminal_sql': 'SELECT id FROM hr.employee WHERE id > 107000',
        'scalar_bindings': [{'step': 2, 'parameter': 'threshold', 'source_step': 1,
                             'source_output': 'average', 'value': 107000}],
    })
    stored = TrailEntry.from_doc(original.to_doc())
    brief = await enrich_brief(
        JudgeBrief('exit_table', 'Above average', results=({'tool_call_id': 'q'},)),
        trail=[stored], turn_index=0, session_id=CREDS.session_id,
        store=InMemorySessionStore(), catalog_provider=catalog(), credentials=CREDS, analysis_state=None,
    )
    assert brief.results[0]['execution']['scalar_bindings'] == original.blueprint_execution['scalar_bindings']
    assert 'scalar_bindings' not in model_execution(stored.blueprint_execution)
    assert 'execution.scalar_bindings' in _POST_EXECUTION_PROMPT


def test_receipt_keeps_only_used_metadata_and_formats_sql_once(monkeypatch):
    import sqlglot

    from data_agent.runtime.context.blueprint_execution import _formatted_sql
    _formatted_sql.cache_clear()
    calls = []
    original = sqlglot.parse_one
    def parse(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(sqlglot, 'parse_one', parse)
    value = receipt()
    assert not {'bound_slots', 'omitted_slots', 'resolved_rule_bindings', 'window_end', 'verify'} & value.keys()
    model_execution(value)
    model_execution(TrailEntry.from_doc(entry().to_doc()).blueprint_execution)
    assert len(calls) == 1
