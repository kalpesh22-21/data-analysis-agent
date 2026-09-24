# Counting rows across a grouped lookup

The aggregation guard rejected a valid employee-to-department-average join because `COUNT(*)` and a cross-table `countIf` predicate caused it to require unique department codes on both sides. Repeated department codes on employee rows are expected; joining each employee to one grouped department row does not multiply employee records.

The guard now recognizes a narrow structural proof: a two-relation inner/left join whose lookup groups by plain columns, exposes those grouping columns unchanged, and matches every grouping key through mandatory equality predicates or `USING`. Row counts and `countIf` predicates can retain the FROM-side grain. CTE resolution uses SQL scopes, including shadowed names. Renamed projected keys and complete composite grouping keys are supported.

Other aggregate measures retain their existing checks. Summing lookup values across employees still checks employee-side multiplicity. Partial grouping keys, grouping extensions such as rollup/totals, row-expanding projections, nonmandatory equalities, and unproven multiway joins do not receive this exemption. Existing outer-join unmatched-row and nested aggregate checks remain active. This is not a general removal of join-cardinality validation.

Validation:

- Runtime suite: 3,945 passed, 5 skipped, including 24 new grouped-join cases.
- Ruff and whitespace checks passed.
- Live scoped MCP/ClickHouse probe: the original inline query and its CTE equivalent both passed the runtime guard and returned seven employee rows and four above their department average. This is the intermediate salary diagnostic, before the leave exclusion that reduces the final employee list to three.
- Live negative cases: summing a repeated department-average value and joining payroll amounts directly to leave events remained blocked.
- The existing seven-employee fixture entitlement had expired and was refreshed before live validation; business data was not changed.
- Backend restarted with Kimi and capabilities disabled. Its authenticated `/query/page` endpoint returned HTTP 200 and `[[7, 4]]` for the same SQL. The guard probes run separately through the actual runtime guard and scoped dispatcher; this validation does not depend on the rate-limited Kimi provider generating the same query again.

Reproduce the live positive and negative guard checks against the local integration stack:

```sh
.venv/bin/python scripts/probe_grouped_join_guard.py --output /tmp/grouped-join-live-probe.json
```

## Extended edge-case corrections

The follow-up audit expanded validation beyond projection aggregates, preserved nested CTE scopes in probes, and admitted safe lookup counts, conditional measures, filtered joins, transformed keys and ANY joins under the checks documented in [the audit](sql-guard-edge-case-audit-2026-09-24.md). Ordinary/DISTINCT/positional-group lookup cases use live uniqueness probes rather than the original no-query structural shortcut. Unknown shapes remain conservative.

Final validation: 3,969 runtime tests passed, 5 skipped; 25 live edge cases and the original four grouped-join cases passed. No answer-judge or authorization policy was weakened.
