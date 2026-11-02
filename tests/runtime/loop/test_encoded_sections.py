"""A single encoding correction must not relax the answer contract."""
import json
import pytest
from data_agent.runtime.composer import PresentationError, proposal_to_internal


def test_decodes_once_without_mutating_input():
    raw = {'takeaway': 'Hello', 'sections': '[]', 'evidence': '[]'}
    assert proposal_to_internal(raw)['evidence'] == []
    assert proposal_to_internal(raw)['sections'] == []
    assert raw['sections'] == '[]'


@pytest.mark.parametrize('sections', ['not JSON', '{', '{}', 'null', '42', '"[]"', '[NaN]', '[{}]'])
def test_rejects_invalid_json_or_invalid_decoded_contract(sections):
    with pytest.raises(PresentationError):
        proposal_to_internal({'takeaway': 'Hello', 'sections': sections})


def test_encoded_sections_still_require_unique_ids_and_table_reasons():
    from tests.runtime.loop.test_salvage_contract import recovery_sections
    sections = recovery_sections(['q', 'q2'])
    for s in sections:
        s.pop('table_reason', None)
    with pytest.raises(PresentationError, match='table_reason'):
        proposal_to_internal({'takeaway': 'Totals', 'sections': json.dumps(sections)})
    sections[1]['id'] = sections[0]['id']
    with pytest.raises(PresentationError, match='unique'):
        proposal_to_internal({'takeaway': 'Totals', 'sections': json.dumps(sections)})


async def test_salvage_accepts_encoded_valid_sections_without_retry():
    from tests.runtime.loop.test_salvage_contract import RecoveryClient, PART, batch, call
    from data_agent.runtime.loop.answer_judge import AnswerJudge, JudgeBrief
    response = {**PART, 'outcome': 'partial', 'sections': json.dumps(PART['sections']), 'evidence': json.dumps(PART['evidence'])}
    client = RecoveryClient([batch(call('salvageAnswer', 'encoded', **response))])
    result = await AnswerJudge(client, 10000).salvage(JudgeBrief('exit_prose', 'Headcount?'))
    assert result['sections'] == PART['sections']
    assert client.calls_made == 1


@pytest.mark.parametrize('value', ['not JSON', '{}', 'null', '42', '"[]"', '[NaN]'])
def test_invalid_encoding_stays_unchanged_for_schema_feedback(value):
    from data_agent.runtime.composer import normalize_answer_arguments
    raw = {'takeaway': '["prose"]', 'sections': value, 'evidence': value}
    assert normalize_answer_arguments(raw) is raw
    with pytest.raises(PresentationError, match='sections: expected type: array, got: string'):
        proposal_to_internal(raw)


def test_normalization_does_not_parse_prose_nested_or_derived_fields():
    from data_agent.runtime.composer import normalize_answer_arguments
    raw = {'takeaway': '[]', 'sections': '[{"title":"[]","caveats":"[]"}]',
           'tables': '[]', 'deliverables': '[]'}
    result = normalize_answer_arguments(raw)
    assert result['sections'] == [{'title': '[]', 'caveats': '[]'}]
    assert result['takeaway'] == result['tables'] == result['deliverables'] == '[]'


def test_encoded_judge_correction_survives_parsing_and_telemetry_check():
    from data_agent.runtime.loop.answer_judge import parse_verdict, _looks_malformed
    from tests.runtime.loop.test_answer_judge_unit import _raw_turn
    document = {'takeaway': 'Results.', 'sections': '[]', 'evidence': '[]'}
    result = _raw_turn({'approved': True, 'violation': '', 'feedback': '',
                       'repair_type': 'presentation', 'corrected_document': document})
    verdict = parse_verdict(result, 'exit_table')
    assert verdict.corrected_document == {'takeaway': 'Results.', 'sections': [], 'evidence': []}
    assert not _looks_malformed(result, 'exit_table')
    assert document['sections'] == '[]'
