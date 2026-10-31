import pytest

from data_agent.runtime.blueprint.models import Blueprint, BlueprintParseError
from data_agent.runtime.composer import _visual
from data_agent.runtime.dispatch.tool_dispatcher import _build_preview
from data_agent.runtime.provenance.formats import validate_formats
from data_agent.runtime.session.models import ResultPreview


def test_metadata_survives_preview_and_session_roundtrip():
    raw={'columns':['ZIP','Pay'],'rows':[['02108','1234.50']],
         'column_formats':{'ZIP':{'kind':'identifier'}, 'Pay':{'kind':'currency','currency':'USD'}}}
    preview=_build_preview(raw,20)
    assert ResultPreview.from_doc(preview.to_doc()) == preview
    assert preview.preview_rows == raw['rows']
    p=_visual({'kind':'figure','result_id':'q','column':'Pay','label':'Pay'},
              preview.columns,preview.preview_rows,column_formats=preview.column_formats)
    assert p['column']=='Pay' and 'value' not in p
    assert preview.column_formats == raw['column_formats']


def test_chart_numbers_remain_numeric_and_labels_are_formatted():
    p=_visual({'kind':'bars','result_id':'q','category_column':'ZIP','measure_column':'Pay','label':'Pay'},
              ['ZIP','Pay'],[['02108',1234.5]],column_formats={'Pay':{'kind':'currency','currency':'USD'}})
    assert p['x_column']=='ZIP' and p['y_column']=='Pay'
    assert 'items' not in p


def test_blueprint_formats_parse_and_reject_ambiguous_percent():
    bp=Blueprint.parse(id='b',intent='Pay',sql_template='SELECT 1 AS Pay',
                       column_formats={'Pay':{'kind':'currency','currency':'USD'}})
    assert bp.column_formats['Pay']['currency']=='USD'
    with pytest.raises(BlueprintParseError,match='scale'):
        Blueprint.parse(id='b',intent='Rate',sql_template='SELECT 1',column_formats={'Rate':{'kind':'percent'}})


@pytest.mark.parametrize('fmt', [{'kind':[]}, {'kind':'percent', 'scale':[]}, {'kind':'currency'}, {'kind':'number','precision':True}, {'kind':'number','scale':'ratio'}])
def test_invalid_format_is_not_silently_accepted(fmt):
    with pytest.raises(ValueError):
        validate_formats({'x':fmt})


def test_identifiers_cannot_be_plotted_as_numeric_measures():
    from data_agent.runtime.composer import PresentationError
    with pytest.raises(PresentationError,match='quantitative'):
        _visual({'kind':'bars','result_id':'q','category_column':'City','measure_column':'ZIP','label':'ZIP'},
                ['City','ZIP'],[['Plano',75039]],column_formats={'ZIP':{'kind':'identifier'}})
