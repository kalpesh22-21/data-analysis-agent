# SQL aggregation guard edge-case audit — 2026-09-24

## Resolution

Implemented in the amended grouped-join guard commit. The findings below preserve the **before-fix** evidence.

- Aggregates in HAVING/ORDER BY are now checked within their owning SELECT scope.
- Positive mandatory equality keys are shared by the probe path; NOT is never interpreted as an equality guarantee. Additional OR filters may accompany a mandatory key.
- Counts use FROM-side row preservation for two-relation inner/left joins. Unique ordinary, DISTINCT and positional-group lookup outputs can pass scoped live probes.
- Conditional SUM/AVG and CASE/IF value branches distinguish measured values from predicate columns.
- Probes preserve nested WITH clauses and apply deterministic relation-local filters. A live shadowed-CTE negative case remains blocked.
- Safe transformed keys are probed as transformed values. Unknown functions retain conservative rejection. Unambiguous physical columns can be resolved using the current credential-bound catalog; unknown/ambiguous columns still require qualification.
- ANY joins exempt right-side multiplicity only when preserving left-side measures. Outer-side counts require a narrowly proven default-excluding filter and a known String/numeric catalog type; unknown types retain rejection.

Validation: **3,969 runtime tests passed, 5 skipped**; **25 live edge cases passed**, plus the original four grouped-join live cases. Includes 24 additional unit regressions covering the audit and adversarial cases. These are deterministic guard/executor tests; no new Kimi/answer-judge conversation evaluation was performed.

Reproduce: `uv run python scripts/probe_sql_guard_edge_cases.py --output /tmp/sql-guard-live-regression.json`.

Scope: deterministic SQL aggregation checks in `src/data_agent/runtime/loop/measurement.py` and their invocation from the agent loop. This is a targeted audit, not an exhaustive review of every SQL validation or authorization path.

## Method and limits

Ran 24 read-only cases against the configured local integration warehouse, using the real scoped MCP dispatcher and bound runtime credentials. For each case, invoked `validate_join_cardinality`, then independently executed the SQL through the scoped dispatcher to distinguish a guard rejection from SQL execution failure. Authorization remained enforced for both operations. Compared relevant results against employee-only baselines or equivalent prefiltered queries. No runtime logic or service settings were changed.

These are code/warehouse probes, not Kimi conversation or answer-judge evaluations. An accepted guard result does not establish that the judge would approve delivery. Results reflect the seven-employee fixture. Numerical results below are fixture aggregates.

## Priority 1: aggregates outside the projection escape validation

`measurement.py:148–155` collects aggregates only from SELECT expressions. Aggregates used exclusively in HAVING or ORDER BY therefore bypass the multiplicity checks at line 178.

Confirmed both paths live:

- Employee salary joined to payroll, grouped by department, with `HAVING sum(e.annual_salary)>500000`: guard allows it and execution returns D01 and D02. The employee-only baseline returns no departments. Payroll rows inflate the salary sums.
- The same join ordered by `sum(e.annual_salary) DESC`: guard allows it and ranks D01 before D02. The employee-only baseline ranks D02 before D01.

Fix: enumerate relevant aggregate expressions throughout each SELECT scope, including HAVING, ORDER BY and applicable window/QUALIFY expressions. Preserve the nearest-SELECT ownership check so nested queries are evaluated separately. Add regressions asserting guard outcomes and baseline-correct results, not merely the presence of generated probes.

## Priority 2: safe query patterns are rejected

| Pattern | Live evidence | Root cause / proposed approach |
|---|---|---|
| `COUNT(*)` over employee → department lookup | Rejected; direct result 7. `COUNT(e.employee_code)` passes and returns 7. | Generic counts demand uniqueness on both sides; extend source-row preservation proofs to validated lookup keys. Preserve ambiguity handling when the intended counted entity is unknown. |
| DISTINCT lookup or positional GROUP BY | Both rejected; each returns 7. Equivalent explicit GROUP BY passes. | The recent structural proof recognizes only plain grouping columns. Normalize GROUP BY positions safely and recognize DISTINCT keys covering the join. |
| Conditional salary sum using department predicate | Rejected; direct result 250000, matching employee-only baseline. | All referenced columns are treated as measured sources, including condition columns. Distinguish measure arguments from predicate dependencies; require predicates to preserve measure-row multiplicity. |
| Payroll filtered to one earning record per employee and period | Rejected; direct result 700000. Moving the same filters into the payroll subquery passes and returns 700000. | Uniqueness probes ignore outer WHERE restrictions. Push down only proven relation-local predicates, respecting outer-join semantics. |
| Nested CTE used by a join | Rejected with probe `PARSE_FAILED_CLOSED`; original SQL executes and returns 700000. | Generated probe references `dept` without carrying the nested WITH binding. Build probes from the actual lexical scope, including dependency bindings and shadowing. |
| Mandatory equality plus additional OR filter | Rejected; direct result 575000. | Any OR anywhere in ON triggers rejection. Extract mandatory equality conjuncts; an extra disjunctive restriction does not invalidate an existing unique-key proof. |
| Filtered outer-side count | Rejected; direct result 2. | Outer-count validation does not prove that WHERE eliminates unmatched rows. Its INNER equivalent is also rejected by the separate count-grain check. Address both layers, with ClickHouse default-filled rows explicitly covered. |
| Unqualified but unambiguous measure | Rejected; direct salary sum 700000. | Guard requires explicit qualification. Resolve columns against schema/scope, or guide the agent to qualify them; do not infer from spelling alone. |
| Computed equality key (`lower`) | Rejected; direct salary sum 700000. | Key extraction accepts only plain columns. Validate uniqueness of the actual transformed key; uniqueness of the raw key is insufficient. |
| ANY INNER JOIN | Rejected; direct salary sum 700000. | Generic probe ignores the join's at-most-one-match semantics. Add support only with explicit semantics tests; arbitrary right-side selection can still make a requested metric inappropriate. |

These cases are valid and have demonstrably correct results for the fixture and stated employee measures. This does not imply every structurally similar query should be allowed without proving its grain.

## Additional concern: equality inside NOT

Generic ON analysis (`measurement.py:191`) treats descendant equality nodes as join keys, even when negated. The grouped-lookup helper correctly restricts its own proof to mandatory positive equalities, but the generic probe path does not.

For `ON NOT(e.department_code=d.department_code)`, the guard allows the query after testing department-code uniqueness. That test does not establish uniqueness of matches under inequality. However, the scoped live executor rejected this query with `CLICKHOUSE_QUERY_ERROR`. Therefore this is a confirmed unsound guard inference, **not a demonstrated executable wrong-answer path in the current environment**. A second variant also failed execution. Harden predicate extraction, but do not report these as successful warehouse exploits.

## Recommended work sequence

1. Close HAVING/ORDER BY coverage and add live correctness comparisons.
2. Unify mandatory-key extraction across the structural proof and probe paths; test AND, OR, parentheses and NOT.
3. Make probe construction scope-aware and safely filter-aware.
4. Generalize source-row preservation for ordinary unique lookups, DISTINCT/grouping variants and supported join semantics.
5. Separate conditional aggregate predicates from measured values. Add conservative handling for computed keys and null/default behavior.
6. Run the existing runtime suite, this live matrix, and a paced Kimi/answer-judge evaluation using questions that exercise these patterns.

The runtime owns the confirmed guard issues. A remote developer can contribute failing SQL and expected measure grain, but these reproductions do not require them to diagnose our code. Treat executor SQL incompatibilities separately from guard failures. Preserve authorization and explicit judge rejections throughout any changes.

## Complete live matrix

### baseline_salary

```sql
SELECT sum(e.annual_salary) FROM dbpcm_warehouse.employee e
```

Guard: allowed.

Scoped execution: `ok`; rows `[["700000.000000"]]`.

### safe_sum_lookup

```sql
SELECT sum(e.annual_salary) FROM dbpcm_warehouse.employee e JOIN dbpcm_warehouse.department d ON e.department_code=d.department_code
```

Guard: allowed.

Scoped execution: `ok`; rows `[["700000.000000"]]`.

### plain_lookup_count

```sql
SELECT count(*) FROM dbpcm_warehouse.employee e JOIN dbpcm_warehouse.department d ON e.department_code=d.department_code
```

Guard: rejected — The aggregate join can duplicate measured records, or its cardinality could not be checked. Aggregate each source to the intended grain or use a semijoin..

Scoped execution: `ok`; rows `[[7]]`.

### distinct_lookup_count

```sql
SELECT count(*) FROM dbpcm_warehouse.employee e JOIN (SELECT DISTINCT department_code FROM dbpcm_warehouse.employee) d ON e.department_code=d.department_code
```

Guard: rejected — The aggregate join can duplicate measured records, or its cardinality could not be checked. Aggregate each source to the intended grain or use a semijoin..

Scoped execution: `ok`; rows `[[7]]`.

### grouped_lookup_count

```sql
SELECT count(*) FROM dbpcm_warehouse.employee e JOIN (SELECT department_code FROM dbpcm_warehouse.employee GROUP BY department_code) d ON e.department_code=d.department_code
```

Guard: allowed.

Scoped execution: `ok`; rows `[[7]]`.

### positional_group_count

```sql
SELECT count(*) FROM dbpcm_warehouse.employee e JOIN (SELECT department_code FROM dbpcm_warehouse.employee GROUP BY 1) d ON e.department_code=d.department_code
```

Guard: rejected — The aggregate join can duplicate measured records, or its cardinality could not be checked. Aggregate each source to the intended grain or use a semijoin..

Scoped execution: `ok`; rows `[[7]]`.

### conditional_lookup_sum

```sql
SELECT sumIf(e.annual_salary,d.department_code='D01') FROM dbpcm_warehouse.employee e JOIN dbpcm_warehouse.department d ON e.department_code=d.department_code
```

Guard: rejected — The aggregate join can duplicate measured records, or its cardinality could not be checked. Aggregate each source to the intended grain or use a semijoin..

Scoped execution: `ok`; rows `[["250000.000000"]]`.

### conditional_baseline

```sql
SELECT sum(e.annual_salary) FROM dbpcm_warehouse.employee e WHERE e.department_code='D01'
```

Guard: allowed.

Scoped execution: `ok`; rows `[["250000.000000"]]`.

### negated_equality_sum

```sql
SELECT sum(e.annual_salary) FROM dbpcm_warehouse.employee e JOIN dbpcm_warehouse.department d ON NOT(e.department_code=d.department_code)
```

Guard: allowed.

Scoped execution: `denied`; error `CLICKHOUSE_QUERY_ERROR`.

### having_only_sum

```sql
SELECT e.department_code FROM dbpcm_warehouse.employee e JOIN dbpcm_warehouse.payroll p ON e.employee_code=p.employee_code GROUP BY e.department_code HAVING sum(e.annual_salary)>500000 ORDER BY e.department_code
```

Guard: allowed.

Scoped execution: `ok`; rows `[["D01"], ["D02"]]`.

### having_baseline

```sql
SELECT e.department_code FROM dbpcm_warehouse.employee e GROUP BY e.department_code HAVING sum(e.annual_salary)>500000 ORDER BY e.department_code
```

Guard: allowed.

Scoped execution: `ok`; rows `[]`.

### filtered_payroll_join

```sql
SELECT sum(e.annual_salary) FROM dbpcm_warehouse.employee e JOIN dbpcm_warehouse.payroll p ON e.employee_code=p.employee_code WHERE p.register_type='EARN' AND p.pay_period_end_date='2024-01-15'
```

Guard: rejected — The aggregate join can duplicate measured records, or its cardinality could not be checked. Aggregate each source to the intended grain or use a semijoin..

Scoped execution: `ok`; rows `[["700000.000000"]]`.

### prefiltered_payroll_join

```sql
SELECT sum(e.annual_salary) FROM dbpcm_warehouse.employee e JOIN (SELECT employee_code FROM dbpcm_warehouse.payroll WHERE register_type='EARN' AND pay_period_end_date='2024-01-15') p ON e.employee_code=p.employee_code
```

Guard: allowed.

Scoped execution: `ok`; rows `[["700000.000000"]]`.

### filtered_outer_count

```sql
SELECT count(d.department_code) FROM dbpcm_warehouse.employee e LEFT JOIN dbpcm_warehouse.department d ON e.department_code=d.department_code WHERE d.department_code='D01'
```

Guard: rejected — Outer-join count can include unmatched default-filled rows as employees or other entities, even with DISTINCT. Aggregate the counted source before the outer join, then fill missing counts with zero. Qualify counted columns when counting the preserved side..

Scoped execution: `ok`; rows `[[2]]`.

### filtered_inner_count

```sql
SELECT count(d.department_code) FROM dbpcm_warehouse.employee e JOIN dbpcm_warehouse.department d ON e.department_code=d.department_code WHERE d.department_code='D01'
```

Guard: rejected — The aggregate join can duplicate measured records, or its cardinality could not be checked. Aggregate each source to the intended grain or use a semijoin..

Scoped execution: `ok`; rows `[[2]]`.

### qualified_lookup_count

```sql
SELECT count(e.employee_code) FROM dbpcm_warehouse.employee e JOIN dbpcm_warehouse.department d ON e.department_code=d.department_code
```

Guard: allowed.

Scoped execution: `ok`; rows `[[7]]`.

### nested_cte_sum

```sql
SELECT * FROM (WITH dept AS (SELECT department_code FROM dbpcm_warehouse.department) SELECT sum(e.annual_salary) FROM dbpcm_warehouse.employee e JOIN dept d ON e.department_code=d.department_code)
```

Guard: rejected — The join cardinality probe failed; this is not proof of duplicate rows. PARSE_FAILED_CLOSED Correct the underlying query or use the tested SQL procedure..

Scoped execution: `ok`; rows `[["700000.000000"]]`.

### order_only_sum

```sql
SELECT e.department_code FROM dbpcm_warehouse.employee e JOIN dbpcm_warehouse.payroll p ON e.employee_code=p.employee_code GROUP BY e.department_code ORDER BY sum(e.annual_salary) DESC
```

Guard: allowed.

Scoped execution: `ok`; rows `[["D01"], ["D02"], ["D03"]]`.

### order_baseline

```sql
SELECT e.department_code FROM dbpcm_warehouse.employee e GROUP BY e.department_code ORDER BY sum(e.annual_salary) DESC
```

Guard: allowed.

Scoped execution: `ok`; rows `[["D02"], ["D01"], ["D03"]]`.

### mandatory_key_with_or

```sql
SELECT sum(e.annual_salary) FROM dbpcm_warehouse.employee e JOIN dbpcm_warehouse.department d ON e.department_code=d.department_code AND (d.department_code='D01' OR d.department_code='D02')
```

Guard: rejected — Disjunctive aggregate joins require an explicit grain rewrite..

Scoped execution: `ok`; rows `[["575000.000000"]]`.

### unqualified_measure

```sql
SELECT sum(annual_salary) FROM dbpcm_warehouse.employee e JOIN dbpcm_warehouse.department d ON e.department_code=d.department_code
```

Guard: rejected — Qualify measured columns so their source grain can be checked..

Scoped execution: `ok`; rows `[["700000.000000"]]`.

### computed_join_key

```sql
SELECT sum(e.annual_salary) FROM dbpcm_warehouse.employee e JOIN dbpcm_warehouse.department d ON lower(e.department_code)=lower(d.department_code)
```

Guard: rejected — Aggregate join cardinality cannot be established. Use a semijoin or aggregate each source at its intended grain..

Scoped execution: `ok`; rows `[["700000.000000"]]`.

### any_payroll_join

```sql
SELECT sum(e.annual_salary) FROM dbpcm_warehouse.employee e ANY INNER JOIN dbpcm_warehouse.payroll p ON e.employee_code=p.employee_code
```

Guard: rejected — The aggregate join can duplicate measured records, or its cardinality could not be checked. Aggregate each source to the intended grain or use a semijoin..

Scoped execution: `ok`; rows `[["700000.000000"]]`.

### negated_eq_with_valid_key

```sql
SELECT sum(e.annual_salary) FROM dbpcm_warehouse.employee e JOIN dbpcm_warehouse.payroll p ON e.department_code=p.department_code AND NOT(e.employee_code=p.employee_code)
```

Guard: rejected — The aggregate join can duplicate measured records, or its cardinality could not be checked. Aggregate each source to the intended grain or use a semijoin..

Scoped execution: `denied`; error `CLICKHOUSE_QUERY_ERROR`.

