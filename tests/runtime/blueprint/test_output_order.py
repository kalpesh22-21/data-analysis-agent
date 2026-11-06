"""Output-column ordering is an AST operation, not a raw SQL substitution."""
import sqlite3
from pathlib import Path

import pytest
import yaml
from sqlglot import exp

from data_agent.runtime.blueprint.models import BlueprintParseError, SlotSpec
from data_agent.runtime.blueprint.template import (
    TemplateBindError,
    bind_template,
    parse_template,
    referenced_slots,
)

EVENTS = [('B', 2), ('A', 3), ('C', 2), ('A', 7), ('D', None)]
TOP_TWO = 'SELECT department AS "Department", SUM(hours) AS "Hours" FROM events GROUP BY department ORDER BY {order_by} LIMIT 2'


def run(sql, rows=EVENTS):
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE events(department TEXT, hours INTEGER)')
        db.executemany('INSERT INTO events VALUES (?, ?)', rows)
        return db.execute(parse_template(sql).sql(dialect="sqlite")).fetchall()


@pytest.mark.parametrize('column,direction,expected', [
    ('Department', 'ASC', [('A', 10), ('B', 2)]),
    ('Department', 'DESC', [('D', None), ('C', 2)]),
    ('Hours', 'ASC', [('B', 2), ('C', 2)]),
    ('Hours', 'DESC', [('A', 10), ('B', 2)]),
])
def test_native_sort_selects_rows_before_limit(column, direction, expected):
    sql = bind_template(TOP_TWO, {'order_by': [{'column': column, 'direction': direction}]})
    actual = run(sql)
    if column == 'Hours':
        assert [row[1] for row in actual] == [row[1] for row in expected]
    else:
        assert actual == expected
    tree = parse_template(sql)
    assert tree.args.get('limit') is not None
    assert tree.find(exp.Subquery) is None
    assert tree.find(exp.Case) is None
    assert tree.args['order'].expressions[0].this.name == column


@pytest.mark.parametrize('column,direction', [
    ('hidden_salary', 'ASC'), ('Hours DESC; DROP TABLE events', 'ASC'),
    ('Hours', 'ASC; SELECT secret'), ('Hours', 'sideways'), ([], 'ASC'),
])
def test_unexposed_columns_and_sql_fragments_rejected(column, direction):
    with pytest.raises(TemplateBindError):
        bind_template(TOP_TWO, {'order_by': [{'column': column, 'direction': direction}]})


def test_union_sort_applies_to_combined_result():
    sql = "SELECT 'B' AS label, 2 AS amount UNION ALL SELECT 'A' AS label, 10 AS amount ORDER BY {order_by}"
    with sqlite3.connect(':memory:') as db:
        assert db.execute(bind_template(sql, {'order_by': [{'column': 'amount', 'direction': 'DESC'}]})).fetchall() == [('A',10), ('B',2)]


_CORPUS = yaml.safe_load((Path(__file__).parents[2] / 'fixtures/corpus_snapshot/blueprints.yaml').read_text())


@pytest.mark.parametrize('blueprint', [b for b in _CORPUS if any(
    s.get('name') == 'order_by' for s in b['slots'])], ids=lambda b: b['id'])
def test_every_declared_output_column_can_be_sorted(blueprint):
    nodes = blueprint.get('composes') or [blueprint]
    terminal = sorted((n for n in nodes if n.get('sql_template')), key=lambda n: n.get('order', 0))[-1]
    template = terminal['sql_template']
    refs = referenced_slots(template)
    slot = next(s for s in blueprint['slots'] if s['name'] == 'order_by')
    for column in slot['allowed_columns']:
        for direction in ('ASC', 'DESC'):
            bindings = {name: 1 for name in refs}
            bindings.update(order_by=[{'column': column, 'direction': direction}])
            tree = parse_template(bind_template(template, bindings))
            assert tree.named_selects == slot['allowed_columns']
            assert tree.args['order'].expressions[0].this.name == column
            assert tree.args['order'].expressions[0].args['desc'] is (direction == 'DESC')


def test_mixed_directions_break_ties_before_limit():
    bindings = {'order_by': [{'column': 'Hours', 'direction': 'DESC'},
                             {'column': 'Department', 'direction': 'DESC'}]}
    sql = bind_template(TOP_TWO, bindings)
    assert run(sql) == [('A', 10), ('C', 2)]
    assert 'ORDER BY "Hours" DESC, "Department" DESC LIMIT 2' in sql
    assert 'NULLS' not in sql and 'blueprint_output' not in sql
    assert bindings['order_by'][0] == {'column': 'Hours', 'direction': 'DESC'}


@pytest.mark.parametrize('value', [
    [], 'Hours', {}, None,
    [{'column': 'Hours', 'direction': 'DESC'}] * 2,
    [{'column': 'Hours', 'direction': 'DESC', 'sql': 'DROP TABLE events'}],
    [{'column': 'Hours'}], [{'column': ['Hours'], 'direction': 'DESC'}],
    [{'column': 'Hours', 'direction': ['DESC']}],
    [{'column': 'unknown', 'direction': 'DESC'}],
    [{'column': 'Hours', 'direction': 'desc'}],
    [{'column': str(i), 'direction': 'ASC'} for i in range(4)],
])
def test_sort_list_rejects_invalid_shapes(value):
    from data_agent.runtime.blueprint.models import validate_sort_order
    with pytest.raises(ValueError):
        validate_sort_order(value, ['Department', 'Hours'])


def test_sort_resolver_validates_default_and_copies_it():
    from data_agent.runtime.blueprint.slots import AskUser, SlotBinding, resolve_slot
    raw = {'name': 'order_by', 'type': 'sort_order', 'required': False,
           'allowed_columns': ['Department', 'Hours'],
           'default': [{'column': 'Hours', 'direction': 'DESC'}]}
    spec = SlotSpec.parse(raw)
    bound = resolve_slot(None, spec)
    assert isinstance(bound, SlotBinding)
    bound.value[0]['direction'] = 'ASC'
    assert spec.default == raw['default'] == [{'column': 'Hours', 'direction': 'DESC'}]
    assert isinstance(resolve_slot([{'column': 'unknown', 'direction': 'ASC'}], spec), AskUser)
    with pytest.raises(BlueprintParseError):
        SlotSpec.parse({**raw, 'default': [{'column': 'unknown', 'direction': 'DESC'}]})


def test_three_column_order_and_quoted_identifier():
    template = 'SELECT 1 AS "x; DROP TABLE t", 2 AS b, 3 AS c ORDER BY {order_by}'
    sql = bind_template(template, {'order_by': [
        {'column': 'x; DROP TABLE t', 'direction': 'DESC'},
        {'column': 'b', 'direction': 'ASC'}, {'column': 'c', 'direction': 'DESC'},
    ]})
    assert isinstance(parse_template(sql), exp.Select)
    assert len(parse_template(sql).args['order'].expressions) == 3


def test_discovery_describes_sort_list_schema():
    from data_agent.runtime.retrieval.tools import _enrich_slot
    slot = _enrich_slot({'name': 'order_by', 'type': 'sort_order', 'required': False,
                        'allowed_columns': ['Hours'],
                        'default': [{'column': 'Hours', 'direction': 'DESC'}]})
    assert slot['value_schema']['maxItems'] == 3
    assert slot['value_schema']['items']['properties']['column']['enum'] == ['Hours']
    assert slot['default'] == [{'column': 'Hours', 'direction': 'DESC'}]
