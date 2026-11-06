"""Runtime ORDER BY slots must not be advertised as extractable predicate literals."""
from data_agent.learning.extractor.models import Decline
from data_agent.learning.extractor.schema import SLOT_TYPE_ENUM
from data_agent.learning.extractor.validation import to_candidate
from data_agent.learning.generalize.rewrite import rewrite_sql_to_template
from data_agent.runtime.blueprint.models import SLOT_TYPES

from .helpers import blueprint_raw, make_summary, make_tool_call, param_slot


def test_sort_order_is_runtime_supported_but_not_offered_for_predicate_mining():
    assert 'sort_order' in SLOT_TYPES
    assert 'sort_order' not in SLOT_TYPE_ENUM


def test_unsupported_sort_extraction_explains_structural_boundary():
    raw = blueprint_raw(parameterization=[param_slot(
        'order_by', slot_type='sort_order', value='6', table='dbpcm_warehouse.employee',
    )])
    sql = 'SELECT employee_code FROM dbpcm_warehouse.employee WHERE order_by = 6'
    result = to_candidate(raw, make_summary(tool_calls=(make_tool_call(ref='tc1', sql=sql),)),
                          known_rules=frozenset())
    assert isinstance(result, Decline)
    assert 'structural ORDER BY locator' in result.detail
    assert 'Preserve the accepted ORDER BY' in result.detail


def test_learning_other_slots_keeps_accepted_multicolumn_sort_intact():
    sql = "SELECT employee_code, Department FROM dbpcm_warehouse.employee WHERE Department = 'Sales' ORDER BY Department ASC, employee_code DESC"
    plan = [{'locator': {'table': 'dbpcm_warehouse.employee', 'column': 'Department', 'value': 'Sales'},
             'role': 'slot', 'slot': {'name': 'department', 'type': 'entity'}}]
    rewritten = rewrite_sql_to_template(sql, plan)
    assert '{department}' in rewritten
    assert 'ORDER BY Department ASC, employee_code DESC' in rewritten
