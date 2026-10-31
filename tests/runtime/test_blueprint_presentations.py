"""Corpus-wide presentation contracts; fixtures are synthetic, not query truth."""
import json

import pytest
from scripts.audit_blueprint_presentations import load_cases, materialize

CASES = list(load_cases())


def case_id(case):
    return ':'.join([case['blueprint'], str(case['group']), *case['measures']])


@pytest.mark.parametrize('case', CASES, ids=case_id)
@pytest.mark.parametrize('variant', ['native', 'numeric_strings', 'null_measure', 'truncated'])
def test_every_blueprint_projection_has_a_valid_reviewable_presentation(case, variant):
    options = {} if variant == 'native' else {variant: True}
    result = materialize(case, **options)
    expected = 'table' if variant == 'truncated' and case['kind'] in {'figure', 'facts'} else case['kind']
    assert result['rendered'] == expected, result['reasons']
    if expected == 'table' and case['kind'] != 'table':
        assert result['reasons'] and 'shown as a table' in result['reasons'][0]
    assert result['document']['sources']['audit']['sql'] == 'SELECT synthetic_audit'
    assert not {'points', 'items', 'value', 'rows'} & result['document']['sections'][0]['presentation'].keys()
    json.dumps(result['document'], allow_nan=False)


@pytest.mark.parametrize('case', [c for c in CASES if c['kind'] == 'line'], ids=case_id)
@pytest.mark.parametrize('options', [{'reverse': True}, {'count': 2}])
def test_temporal_blueprints_accept_reverse_order_and_two_period_comparisons(case, options):
    result = materialize(case, **options)
    assert result['rendered'] == 'line', result['reasons']
    presentation = result['document']['sections'][0]['presentation']
    assert presentation['x_column'] and presentation['y_column']
    assert 'points' not in presentation
