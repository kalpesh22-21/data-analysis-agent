"""Audit every corpus output against the real document materializer.

This is a shape/contract battery using explicitly synthetic cells, NOT SQL execution
or a conversational LLM battery. SQL projection names and grouping variants come from
actual blueprints; the reviewed candidate selections live beside the corpus snapshot.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import sqlglot
import yaml

from data_agent.runtime.composer import PresentationError, build_document, proposal_to_internal
from data_agent.runtime.session.models import ResultPreview

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / 'tests/fixtures/corpus_snapshot/blueprints.yaml'
PROFILES = ROOT / 'tests/fixtures/corpus_snapshot/presentation_cases.yaml'


def load_cases(corpus=SNAPSHOT):
    raw = yaml.safe_load(corpus.read_text())
    docs = raw['blueprints'] if isinstance(raw, dict) else raw
    if isinstance(docs, dict):
        docs = list(docs.values())
    profiles = yaml.safe_load(PROFILES.read_text())
    assert {d['id'] for d in docs} == set(profiles), 'Every blueprint requires a reviewed profile'
    for doc in docs:
        profile = profiles[doc['id']]
        sql = (doc.get('composes') or [doc])[-1]['sql_template']
        tree = sqlglot.parse_one(re.sub(r'\{(\w+)\}', r':\1', sql), read='clickhouse')
        columns = [e.alias_or_name for e in tree.selects]
        assert len(set(columns)) == len(columns), (doc['id'], 'ambiguous projection')
        assert set(profile.get('categories', []) + profile.get('measures', [])) <= set(columns), doc['id']
        groups = next((s['enum_values'] for s in doc['slots'] if s['name'] == 'group_by'), [None])
        for group in groups:
            categories = profile.get('categories', [])
            kind = profile['kind']
            if kind == 'grouped':
                kind = 'line' if group in {'month', 'day'} else 'scalar' if group == 'company' and not {'Pay Type'} & set(categories) else 'bars'
                if kind == 'line':
                    categories = ['Group']
            measures = profile.get('measures', [])
            if kind == 'scalar':
                yield dict(blueprint=doc['id'], group=group, columns=columns, kind='facts' if len(measures) > 1 else 'figure', measures=measures, categories=[], sql=sql)
            elif kind == 'table':
                yield dict(blueprint=doc['id'], group=group, columns=columns, kind='table', measures=[], categories=[], sql=sql)
            else:
                for measure in measures:
                    yield dict(blueprint=doc['id'], group=group, columns=columns, kind=kind, measures=[measure], categories=categories, sql=sql)


def materialize(case, *, count=4, numeric_strings=False, reverse=False, null_measure=False, truncated=False):
    """Feed projected columns and synthetic warehouse-shaped rows through real validation."""
    kind, columns = case['kind'], case['columns']
    if kind in {'figure', 'facts'}:
        count = 1
    rows = [[f'example-{i}' for _ in columns] for i in range(count)]
    categories = case['categories']
    for i, row in enumerate(rows):
        for j, name in enumerate(categories):
            if kind == 'line':
                # Monthly string output is common; lower-case month is a DATE output.
                row[columns.index(name)] = f'2026-{i+1:02d}' + ('-01' if name in {'month', 'Period', 'Punch Date'} or case['group'] == 'day' else '')
            elif len(categories) > 1:
                # Duplicate first dimension, distinct compound grain, as in pay type/dept.
                row[columns.index(name)] = f'group-{i // 2}' if j == 0 else f'value-{i % 2}'
            elif name in {'Pairs Per Day', 'Band Start', 'Lower Bound'}:
                row[columns.index(name)] = i + 1
            else:
                row[columns.index(name)] = f'group-{i}'
        for name in case['measures']:
            row[columns.index(name)] = str(i + 1) + '.25' if numeric_strings else i + 1
        if null_measure and case['measures'] and i == 0:
            row[columns.index(case['measures'][0])] = None
    if reverse:
        rows.reverse()
    presentation = {'kind': kind, 'result_id': 'audit'}
    if kind in {'bars', 'line'}:
        presentation.update(measure_column=case['measures'][0], label=case['measures'][0].replace('_', ' '))
        if len(categories) == 1:
            presentation['category_column'] = categories[0]
        else:
            presentation['category_columns'] = categories
    elif kind == 'figure':
        presentation.update(column=case['measures'][0], label=case['measures'][0].replace('_', ' '))
    elif kind == 'facts':
        presentation['columns'] = case['measures']
    proposal = {'takeaway': 'Synthetic presentation compatibility check.', 'evidence': ['audit'], 'sections': [
        {'id': 'audit', 'evidence_refs': ['audit'], 'presentation': presentation, 'caveats': [], 'actions': []},
    ]}
    try:
        normalized = proposal_to_internal(proposal)
        receipt = SimpleNamespace(tool_call_id='audit', turn_index=0, tool_name='runQuery', status='ok', error_code=None,
                                  provenance=frozenset(), args={'sql': 'SELECT synthetic_audit'}, ts='2026-10-29T00:00:00Z',
                                  result_preview=ResultPreview(columns=columns, row_count=count+1 if truncated else count,
                                                               truncated=truncated, preview_rows=rows))
        document = build_document(normalized, [receipt], turn_index=0)
        section = document['sections'][0]
        return {'rendered': section['presentation']['kind'], 'reasons': section['caveats'], 'document': document}
    except PresentationError as exc:
        return {'rendered': 'rejected', 'reasons': [str(exc)]}


def audit():
    results = []
    for case in load_cases():
        variants = [('native', {}), ('numeric_strings', {'numeric_strings': True}), ('null_measure', {'null_measure': True})]
        if case['kind'] == 'line':
            variants += [('descending', {'reverse': True}), ('two_periods', {'count': 2})]
        if case['kind'] != 'table':
            variants += [('truncated', {'truncated': True})]
        for variant, options in variants:
            result = materialize(case, **options)
            result.pop('document', None)
            expected = 'table' if options.get('truncated') or options.get('null_measure') and case['kind'] == 'bars' else case['kind']
            results.append({k: v for k, v in case.items() if k != 'sql'} | {
                'variant': variant, 'expected': expected, **result, 'passed': result['rendered'] == expected,
            })
    return results



def write_markdown(path, results):
    profiles = yaml.safe_load(PROFILES.read_text())
    counts = Counter(profile['kind'] for profile in profiles.values())
    lines = [
        '# Blueprint presentation compatibility audit', '',
        f'The local corpus contains **{len(profiles)} blueprints**. These are reviewed candidate presentations, '
        'not fixed runtime routing rules: the user request, selected measure, grouping bindings and '
        'actual result determine the final view. Several measures mean alternatives, not automatic aggregation.', '',
        '**Method:** read every terminal SQL projection (including the three composed blueprints), '
        'check exact selector names, then feed explicitly synthetic rows through the real proposal validator '
        'and document materializer. Exercise every declared group_by value, each listed measure, native and '
        'serialized numeric values, null measures, incomplete previews, descending time order and two-period lines. '
        'This is not SQL execution, a truth audit, or a claim that every possible production result will chart.', '',
        f'**Result:** {sum(r["passed"] for r in results)}/{len(results)} checks match the expected chart or explained table fallback.', '',
        'Candidate families: ' + ', '.join(f'{value} {key}' for key, value in sorted(counts.items())) + '.', '',
        '## Adaptations', '',
        '- Compound bar selectors preserve all chosen dimensions as one label, without merging rows.',
        '- Line points are ordered chronologically as complete value/label pairs; two distinct periods are sufficient.',
        '- Facts accept up to 16 selected fields, including the 12-metric workforce KPI result; underscores in fact labels become spaces.',
        '- The UI object shapes remain unchanged. The agent has a new optional category_columns selector for compound bars.', '',
        '## Deliberate table fallbacks', '',
        'Incomplete or more-than-25-row previews, null bar measures, unsafe numeric precision, duplicate categories '
        'or periods, ambiguous dates, mixed period granularity, and incompatible selectors retain their table fallback '
        'with an explanation for judge review. Multi-series results must not be flattened into one line; compound bars '
        'or a table preserve their grain. Record/detail blueprints remain table candidates. No result rows are dropped '
        'or aggregated to force a chart.', '',
        '## Every blueprint', '',
        '| Blueprint | Candidate views | Category dimensions | Measure choices | Checks |',
        '|---|---|---|---|---:|',
    ]
    for blueprint, profile in sorted(profiles.items()):
        checks = [r for r in results if r['blueprint'] == blueprint]
        kinds = ', '.join(sorted({r['kind'] for r in checks}))
        categories = ', '.join(profile.get('categories', [])) or '—'
        measures = ', '.join(profile.get('measures', [])) or 'Record columns'
        lines.append(f'| {blueprint} | {kinds} | {categories} | {measures} | {sum(r["passed"] for r in checks)}/{len(checks)} |')
    lines += ['', 'Reproduce with:', '', '```sh',
              'PYTHONPATH=src .venv/bin/python scripts/audit_blueprint_presentations.py --output /tmp/blueprint-presentations.json --markdown docs/verification/blueprint-presentations.md',
              '.venv/bin/pytest tests/runtime/test_blueprint_presentations.py tests/runtime/test_chart_datatypes.py', '```', '']
    path.write_text('\n'.join(lines))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--markdown', type=Path)
    args = parser.parse_args()
    results = audit()
    report = {'method': 'Actual corpus projections; synthetic shape/type cells; no SQL or LLM execution.',
              'blueprints': len({r['blueprint'] for r in results}), 'checks': len(results),
              'passed': sum(r['passed'] for r in results), 'results': results}
    args.output.write_text(json.dumps(report, indent=2))
    print({k: v for k, v in report.items() if k != 'results'})
    if args.markdown:
        write_markdown(args.markdown, results)
    print('Unexpected rejection/fallback cases:', Counter(r['kind'] for r in results if not r['passed']))


if __name__ == '__main__':
    main()
