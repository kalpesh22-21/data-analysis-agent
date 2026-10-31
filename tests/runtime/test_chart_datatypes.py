"""Presentation instructions preserve column identity; rows are fetched by the UI."""
import json
from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from data_agent.runtime.composer import PresentationError, _visual


@pytest.mark.parametrize('kind', ['bars','line'])
@pytest.mark.parametrize('value', [None, Decimal('123.4500'), '1.25e3', 2**63, True, '1,234', {'x': 1}])
def test_cells_are_not_coerced_or_embedded(kind,value):
    rows=[[date(2026,1,1),value],['2026-02',None]]
    before=deepcopy(rows)
    output=_visual({'kind':kind,'result_id':'q','category_column':'Month','measure_column':'Amount','label':'Payroll'},['Month','Amount'],rows)
    assert output=={'kind':kind,'result_id':'q','x_column':'Month','y_column':'Amount','label':'Payroll'}
    assert rows==before
    json.dumps(output,allow_nan=False)


def test_compound_categories_remain_separate_columns_not_joined_labels():
    output=_visual({'kind':'bars','result_id':'q','category_columns':['Department','Pay Type'],'measure_column':'n','label':'Employees'},['Department','Pay Type','n'],[['Sales','Hourly',10]])
    assert output['x_columns']==['Department','Pay Type'] and output['y_column']=='n'
    assert 'items' not in output


@pytest.mark.parametrize('p', [
    {'kind':'figure','result_id':'q','column':'missing','label':'Count'},
    {'kind':'facts','result_id':'q','columns':['missing']},
    {'kind':'bars','result_id':'q','category_column':'n','measure_column':'n','label':'Count'},
])
def test_invalid_selectors_still_fail_before_delivery(p):
    with pytest.raises(PresentationError):
        _visual(p,['n'],[[1]])
