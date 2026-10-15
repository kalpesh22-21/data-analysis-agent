"""Execute parameterized templates against synthetic rows, independently of an LLM.

SQLite is a relational oracle for filters, joins and grouping, not a claim of
ClickHouse/Trino type equivalence. Live dialect checks supplement these tests.
"""
from pathlib import Path
import sqlite3

import pytest
import sqlglot
import yaml

from data_agent.runtime.blueprint.models import SlotSpec
from data_agent.runtime.blueprint.slots import AskUser, OmitSlot, expand_binding, resolve_slot
from data_agent.runtime.blueprint.template import bind_template

CORPUS = {v['id']: v for v in yaml.safe_load((Path(__file__).parents[2] / 'fixtures/corpus/blueprints.yaml').read_text())}


def bound_sql(bid, **values):
    bp = CORPUS[bid]
    bound, omitted = {}, {}
    for raw in bp['slots']:
        spec = SlotSpec.parse(raw)
        value = values.get(spec.name)
        resolution = resolve_slot(value, spec, domain=[value] if isinstance(value, str) else None)
        assert not isinstance(resolution, AskUser), resolution
        if isinstance(resolution, OmitSlot):
            omitted[spec.name] = resolution.optional_pattern
        else:
            bound.update(expand_binding(spec, resolution.value))
    return bind_template(bp['sql_template'], bound, optional_patterns=omitted)


@pytest.fixture
def db():
    con = sqlite3.connect(':memory:')
    class DistinctCount:
        def __init__(self):
            self.values = set()

        def step(self, value):
            if value is not None:
                self.values.add(value)

        def finalize(self):
            return len(self.values)

    con.create_aggregate('UNIQEXACT', 1, DistinctCount)
    con.create_function('DATETRUNC', 2, lambda unit, value: value[:7] + '-01' if value else None)
    con.create_function('TOSTRING', 1, lambda value: str(value) if value is not None else None)
    con.create_function('TODATE', 1, lambda value: value[:10] if value else None)
    con.execute("ATTACH DATABASE ':memory:' AS dbpcm_warehouse")
    con.executescript('''
    CREATE TABLE dbpcm_warehouse.employee(employee_code TEXT, department_code TEXT, department_name TEXT, employee_status TEXT, annual_salary REAL);
    INSERT INTO dbpcm_warehouse.employee VALUES ('E1','D1','Engineering','Active',100),('E2','D2','HR','Active',200),('E3','D1','Engineering','Terminated',300);
    CREATE TABLE dbpcm_warehouse.department(department_code TEXT, department_name TEXT);
    INSERT INTO dbpcm_warehouse.department VALUES ('D1','Engineering'),('D2','HR'),('D3','Empty');
    CREATE TABLE dbpcm_warehouse.payroll(employee_code TEXT, pay_date TEXT, amount REAL, register_type TEXT, type_code TEXT);
    INSERT INTO dbpcm_warehouse.payroll VALUES ('E1','2026-09-01',100,'EARN','REG'),('E1','2026-09-01',50,'EARN','OT'),('E2','2026-09-01',200,'EARN','REG'),('E3','2026-09-01',300,'EARN','REG'),('E9','2026-09-01',25,'EARN','REG'),('E1','2026-10-01',900,'EARN','REG'),('E1','2026-09-01',40,'EETAX','TAX');
    CREATE TABLE dbpcm_warehouse.accrual_balances(employee_code TEXT, accrual_code TEXT, beginning_balance REAL, accrued_amount REAL, awarded_amount REAL, taken_amount REAL);
    INSERT INTO dbpcm_warehouse.accrual_balances VALUES ('E1','PTO',10,1,0,2),('E1','PTO',20,2,0,3),('E2','PTO',40,4,0,7),('E1','SICK',99,0,0,1);
    CREATE TABLE dbpcm_warehouse.accrual_events(employee_code TEXT, status TEXT, event_type TEXT, earn_code TEXT, request_date TEXT, hours REAL);
    INSERT INTO dbpcm_warehouse.accrual_events VALUES ('E1','Approved','Time Off Request','PTO','2026-09-01',8),('E1','Requested','Time Off Request','PTO','2026-09-02',4),('E2','Denied','Time Off Request','SICK','2026-09-03',6),('E9','Approved','Payout Request','PTO','2026-09-04',5),('E1','Approved','Time Off Request','PTO','2026-10-01',90);
    ''')
    yield con
    con.close()


def execute(db, bid, **args):
    sql = bound_sql(bid, **args)
    translated = sqlglot.transpile(sql, read='clickhouse', write='sqlite')[0]
    return db.execute(translated).fetchall()


PERIOD = {'start': '2026-09-01', 'end': '2026-10-01'}


def test_fixed_active_blueprint_retains_zero_departments(db):
    rows = execute(db, 'bp-active-headcount-by-department')
    assert {r[1]: r[2] for r in rows} == {'Engineering': 1, 'HR': 1, 'Empty': 0}


@pytest.mark.parametrize('status,count,total', [(None,3,600),('Active',2,300),('Terminated',1,300)])
def test_employee_status_filter_is_optional_and_parameterized(db, status, count, total):
    row = execute(db, 'bp-employee-summary', group_by='company', employee_status=status)[0]
    assert row[1:4] == (count,count,total)


def test_department_and_entity_filters(db):
    assert execute(db, 'bp-employee-summary', group_by='department', department='Engineering', employee='E1')[0][1] == 1
    assert execute(db, 'bp-employee-summary', group_by='department', department="x' OR 1=1 --") == []


def test_salary_template_can_filter_status(db):
    rows = execute(db, 'bp-average-salary-by-department', employee_status='Active')
    assert {r[0]:r[1:] for r in rows} == {'Engineering':(100,1),'HR':(200,1)}


def test_payroll_period_register_filters_and_unmatched_employee(db):
    row = execute(db, 'bp-payroll-summary', group_by='company', register_type='EARN', period=PERIOD)[0]
    assert row == ('Company',675,5,4)
    row = execute(db, 'bp-payroll-summary', group_by='company', register_type='EETAX', period=PERIOD)[0]
    assert row == ('Company',40,1,1)
    row = execute(db, 'bp-payroll-summary', group_by='company', register_type='EARN', period=PERIOD, employee_status='Active', type_code='REG')[0]
    assert row == ('Company',300,2,2)


def test_taken_amounts_are_balance_values_not_approved_event_hours(db):
    row = execute(db, 'bp-accrual-balance-summary', group_by='company', accrual_code='PTO', employee_status='Active')[0]
    assert row == ('Company',70,7,0,12,3,2)


def test_events_default_includes_payouts_and_all_statuses(db):
    row = execute(db, 'bp-accrual-event-summary', group_by='company', period=PERIOD)[0]
    assert row == ('Company','All selected statuses',4,23,3)
    rows = execute(db, 'bp-accrual-event-summary', group_by='department_status', period=PERIOD, earn_code='PTO', event_type='Time Off Request')
    assert rows == [('Engineering','Approved',1,8,1),('Engineering','Requested',1,4,1)]


@pytest.mark.parametrize('bid,slot,bad', [
    ('bp-employee-summary','employee_status','A'),
    ('bp-payroll-summary','register_type','arbitrary'),
    ('bp-accrual-event-summary','event_status','arbitrary'),
    ('bp-employee-summary','group_by','department_name; DROP TABLE employee'),
])
def test_fixed_enums_reject_unsupported_values(bid, slot, bad):
    spec = SlotSpec.parse(next(s for s in CORPUS[bid]['slots'] if s['name']==slot))
    assert isinstance(resolve_slot(bad,spec),AskUser)


@pytest.mark.parametrize('comparison,threshold,expected', [('above',200,1),('at_least',200,2),('equal',200,1),('below',200,1),('above',1000,0)])
def test_salary_threshold_comparisons_and_empty_zero(db, comparison, threshold, expected):
    assert execute(db, 'bp-salary-threshold-count', comparison=comparison, salary=threshold) == [(expected,)]


def test_salary_directory_limit_and_status_are_explicit(db):
    rows = execute(db, 'bp-salary-threshold-directory', comparison='at_least', salary=0, row_limit=1, employee_status='Active')
    assert rows == [('E2','HR','Active',200)]


def test_date_basis_does_not_imply_current_status(db):
    for column in ['hire_date','most_recent_hire_date','termination_date']:
        db.execute(f'ALTER TABLE dbpcm_warehouse.employee ADD COLUMN {column} TEXT')
    db.execute("UPDATE dbpcm_warehouse.employee SET hire_date='2026-09-01', most_recent_hire_date='2026-10-01', termination_date='2026-09-15' WHERE employee_code='E1'")
    db.execute("UPDATE dbpcm_warehouse.employee SET hire_date='2025-09-01', most_recent_hire_date='2026-09-10', termination_date='2026-10-01' WHERE employee_code='E3'")
    assert execute(db,'bp-employee-date-summary',date_basis='original_hire',group_by='company',period=PERIOD) == [('Company',1)]
    assert execute(db,'bp-employee-date-summary',date_basis='most_recent_hire',group_by='status',period=PERIOD) == [('Terminated',1)]
    # Recorded termination includes E1 even though current status is Active;
    # the end boundary excludes E3's October termination.
    assert execute(db,'bp-employee-date-summary',date_basis='termination',group_by='status',period=PERIOD) == [('Active',1)]


def test_salary_median_never_uses_approximation():
    sql = bound_sql('bp-salary-exact-median',group_by='company',salary_population='positive')
    assert 'quantileExactLow'.lower() in sql.lower()
    assert 'quantileExactHigh'.lower() in sql.lower()
    assert 'approx' not in sql.lower()


@pytest.mark.parametrize('width', [0,-1,10000001])
def test_invalid_salary_band_width_rejected(width):
    with pytest.raises(AssertionError):
        bound_sql('bp-salary-distribution',band_width=width,salary_population='recorded')


@pytest.mark.parametrize('bid', ['bp-punch-pair-summary','bp-employee-day-clock-span','bp-timecard-earnings-summary'])
def test_timecard_templates_require_source_and_final(bid):
    kwargs = {'period':PERIOD,'source':'synthetic'}
    if bid != 'bp-employee-day-clock-span':
        kwargs['group_by'] = 'company'
    sql = bound_sql(bid,**kwargs)
    assert 'FINAL' in sql
    assert "table_source = 'synthetic'" in sql
    assert '2026-09-01' in sql and '2026-10-01' in sql
    kwargs.pop('source')
    with pytest.raises(AssertionError):
        bound_sql(bid,**kwargs)


def test_punch_types_are_enums_but_hours_codes_are_entities():
    sql = bound_sql('bp-punch-pair-summary',group_by='employee',period=PERIOD,source='one',hours_type='XY',in_type='IL')
    assert "hours_type = 'XY'" in sql and "in_punch_type = 'IL'" in sql
    with pytest.raises(AssertionError):
        bound_sql('bp-punch-pair-summary',group_by='employee',period=PERIOD,source='one',in_type='made-up')
