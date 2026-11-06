"""Actual blueprint execution survives persistence, canonical rendering and judge fallback."""
import json

import pytest

from data_agent.runtime.context.blueprint_execution import blueprint_execution_receipt
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
    assert content['execution'] == receipt()
    assert content['execution']['terminal_sql'].endswith('WHERE TRUE')
    assert 'private extra row' not in json.dumps(content)
    assert 'unrelated' not in json.dumps(content)
    assert content['execution']['semantic_review'] == 'pending'


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
    assert execution['blueprint_id'] == 'bp-actual'
    assert execution['terminal_sql'].endswith('WHERE TRUE')
    assert execution['bound_slots'] == receipt()['bound_slots']
    assert execution['semantic_review'] == 'pending'


def test_judge_prompt_prioritizes_execution_over_blueprint_label():
    assert 'execution.terminal_sql' in _POST_EXECUTION_PROMPT
    assert 'Do not approve or reject by blueprint name' in _POST_EXECUTION_PROMPT
    assert 'a fetched blueprint is not proof that it ran' in _POST_EXECUTION_PROMPT
