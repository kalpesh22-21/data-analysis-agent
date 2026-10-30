from decimal import Decimal

import pytest

from data_agent.runtime.provenance.formats import display_value, validate_formats
from data_agent.runtime.dispatch.tool_dispatcher import _build_preview
from data_agent.runtime.session.models import ResultPreview
from data_agent.runtime.composer import _visual
from data_agent.runtime.blueprint.models import Blueprint, BlueprintParseError


@pytest.mark.parametrize('value,fmt,expected', [
    ('02108', {'kind':'identifier'}, '02108'),
    (75039, {'kind':'identifier'}, '75039'),
    (75039, {'kind':'integer'}, '75,039'),
    (Decimal('1766987.3'), {'kind':'currency','currency':'USD'}, '$1,766,987.30'),
    (Decimal('125.250000'), {'kind':'number','unit':'hours','precision':2}, '125.25 hours'),
    (Decimal('0.1532'), {'kind':'percent','scale':'ratio'}, '15.32%'),
    (Decimal('15.32'), {'kind':'percent','scale':'percent'}, '15.32%'),
    (None, {'kind':'currency','currency':'USD'}, 'Not available'),
    ('9007199254740993', {'kind':'identifier'}, '9007199254740993'),
])
def test_display_semantics(value, fmt, expected):
    assert display_value(value, fmt) == expected


def test_metadata_survives_preview_and_session_roundtrip():
    raw={'columns':['ZIP','Pay'],'rows':[['02108','1234.50']],
         'column_formats':{'ZIP':{'kind':'identifier'}, 'Pay':{'kind':'currency','currency':'USD'}}}
    preview=_build_preview(raw,20)
    assert ResultPreview.from_doc(preview.to_doc()) == preview
    assert preview.preview_rows == raw['rows']
    p=_visual({'kind':'figure','result_id':'q','column':'Pay','label':'Pay'},
              preview.columns,preview.preview_rows,column_formats=preview.column_formats)
    assert p['value']=='$1,234.50'
    assert p['format']==raw['column_formats']['Pay']


def test_chart_numbers_remain_numeric_and_labels_are_formatted():
    p=_visual({'kind':'bars','result_id':'q','category_column':'ZIP','measure_column':'Pay','label':'Pay'},
              ['ZIP','Pay'],[['02108',1234.5]],column_formats={'Pay':{'kind':'currency','currency':'USD'}})
    assert p['items']==[{'label':'02108','raw':1234.5,'value':'$1,234.50'}]


def test_blueprint_formats_parse_and_reject_ambiguous_percent():
    bp=Blueprint.parse(id='b',intent='Pay',sql_template='SELECT 1 AS Pay',
                       column_formats={'Pay':{'kind':'currency','currency':'USD'}})
    assert bp.column_formats['Pay']['currency']=='USD'
    with pytest.raises(BlueprintParseError,match='scale'):
        Blueprint.parse(id='b',intent='Rate',sql_template='SELECT 1',column_formats={'Rate':{'kind':'percent'}})


@pytest.mark.parametrize('fmt', [{'kind':[]}, {'kind':'percent', 'scale':[]}, {'kind':'currency'}, {'kind':'number','precision':True}, {'kind':'number','scale':'ratio'}])
def test_invalid_format_is_not_silently_accepted(fmt):
    with pytest.raises(ValueError): validate_formats({'x':fmt})


def test_identifiers_cannot_be_plotted_as_numeric_measures():
    from data_agent.runtime.composer import PresentationError
    with pytest.raises(PresentationError,match='not a quantitative'):
        _visual({'kind':'bars','result_id':'q','category_column':'City','measure_column':'ZIP','label':'ZIP'},
                ['City','ZIP'],[['Plano',75039]],column_formats={'ZIP':{'kind':'identifier'}})
