"""Presentation repairs reuse references without overriding deliberate new selections."""
import pytest

from data_agent.runtime.loop.proposal import ReviewState, retain_cited_repair_tables, filter_excluded_components


def state(**kwargs):
    return ReviewState(repaired=True, repair_type='presentation', scope_hash='scope',
                       repair_tables=({'result_id':'q','caption':'Count'},), **kwargs)


def test_repair_roundtrip_retains_only_still_cited_tables():
    restored = ReviewState.restore(state().to_doc(), 'scope')
    args = {'tables':[], 'evidence':['q']}
    assert retain_cited_repair_tables(args,restored)['tables'] == [{'result_id':'q','caption':'Count'}]
    assert args['tables'] == []


@pytest.mark.parametrize('args', [
    {'tables':[], 'evidence':[]},
    {'tables':[], 'evidence':['other']},
    {'tables':[{'result_id':'replacement'}], 'evidence':['q','replacement']},
    {'tables':'invalid', 'evidence':['q']},
    {'tables':[], 'evidence':'q'},
])
def test_repair_does_not_override_uncited_or_explicit_selection(args):
    assert retain_cited_repair_tables(args,state()) == args


@pytest.mark.parametrize('repair_type', ['data','omit_component',''])
def test_other_repair_types_can_remove_tables(repair_type):
    s=state();s.repair_type=repair_type
    args={'tables':[], 'evidence':['q']}
    assert retain_cited_repair_tables(args,s) == args


def test_scope_change_clears_saved_references():
    s=ReviewState.restore(state().to_doc(),'different')
    assert s.repair_tables == ()
    args={'tables':[], 'evidence':['q']}
    assert retain_cited_repair_tables(args,s) == args


def test_raw_sql_is_not_restored():
    s=state();s.repair_tables=({'sql':'SELECT 1'},)
    args={'tables':[], 'evidence':['q']}
    assert retain_cited_repair_tables(args,s) == args


def test_exclusion_checks_still_remove_restored_references():
    args=retain_cited_repair_tables({'tables':[], 'evidence':['q']},state())
    filtered=filter_excluded_components(args,({'kind':'table','result_id':'q'},))
    assert filtered['tables'] == []
    assert filtered['evidence'] == []
