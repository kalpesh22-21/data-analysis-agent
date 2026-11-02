"""A single encoding correction must not relax the answer contract."""
import json
import pytest
from data_agent.runtime.composer import PresentationError, proposal_to_internal


def test_decodes_once_without_mutating_input():
    raw = {'takeaway': 'Hello', 'sections': '[]', 'evidence': []}
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
    response = {**PART, 'outcome': 'partial', 'sections': json.dumps(PART['sections'])}
    client = RecoveryClient([batch(call('salvageAnswer', 'encoded', **response))])
    result = await AnswerJudge(client, 10000).salvage(JudgeBrief('exit_prose', 'Headcount?'))
    assert result['sections'] == PART['sections']
    assert client.calls_made == 1
