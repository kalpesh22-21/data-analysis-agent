# Test Plan: Column-Provenance Extraction Unit

**Component:** `sqlglot` ClickHouse-dialect wrapper that, given a SQL string and the catalog schema,
returns the qualified `(table, column)` USES set the query references.

**Decision coverage:** D44, D52, D57, D62, D63, D64

**Layer:** 1 — Unit (pure logic, no I/O; inputs are a SQL string + an in-memory schema dict;
no ClickHouse connection required)

**Framework note for implementer:** use `pytest` + `pytest-parametrize`; each named case below maps
directly to one parametrized test or one `def test_<slug>` function. Target: < 200 ms total suite
runtime (all inputs are strings; no I/O).

---

## Implementation contract (from D62)

```python
# Pseudocode — not implementation code
def extract_column_provenance(sql: str, catalog_schema: dict) -> frozenset[tuple[str, str]]:
    """
    Parse `sql` with sqlglot ClickHouse dialect, run qualify_columns with catalog_schema,
    return the USES set as frozenset of (database.table, column) pairs (D69/OQ-3: canonical
    pair is fully-qualified three-part; scope vectors and catalog keys use the same granularity).

    Fail behavior:
      - ParseError / qualification failure / lambda-body not proved walked (D69/OQ-1) ->
        raise ProvenanceExtractionError
        (callers map this to their own fail behavior: D57/D63 -> reject query;
         D44 -> drop trail entry; D48 -> skip hard key; D35 -> fail-to-review)
    Precondition (D69/OQ-5):
      - Caller must NEVER pass an EXPLAIN statement; that is a caller-enforced precondition,
        not handled inside the extractor.
    """
```

The catalog schema dict fed to `qualify_columns` must mirror the shape sqlglot's optimizer expects.
**Per D69/OQ-3**, keys must be at `database.table` granularity (e.g. `"dbpcm_warehouse.payroll"`)
so that extracted three-part references resolve correctly and scope comparison works at the same
granularity. Scratch tables (D69/OQ-4) are NOT included in this dict; their columns are accepted
without catalog qualification, gated only by the session-ID name match (D64). The test fixtures
below use the REAL table/column names from `databaseSchemaDocs/payroll.yaml` and
`databaseSchemaDocs/employee.yaml`. **Note:** the `CATALOG_SCHEMA` fixture defined below uses bare
table-name keys for readability; the implementation must use the `database.table`-qualified form.
The expected USES sets in test cases other than A-16 show short-form keys for readability; in the
implementation they will be `(database.table, column)` triples consistent with A-16's resolved form.

---

## Fixture definitions (shared across cases)

```
PAYROLL_COLUMNS = [
    "ClientCode", "EmployeeCode", "RegisterType", "Amount", "TypeHours",
    "TypeCode", "TypeCodeDescription", "ProfileCode", "DistributedDepartmentCode",
    "distributedDepartmentDescription", "TypeRate", "PayDate",
    "PayPeriodStartDate", "PayPeriodEndDate", "TransactionNumber"
]

EMPLOYEE_COLUMNS = [
    "ClientCode", "EmployeeCode", "Department", "Position", "EmployeeName",
    "EmployeeStatus", "DepartmentCode", "LivesInState", "WorksInState", "SUIState",
    "TerminationDate", "OldTerminationDate", "FirstName", "MiddleName", "LastName",
    "Nickname", "HireDate", "MostRecentHireDate", "LeaveAbsenceStart", "LeaveAbsenceEnd",
    "PrimarySupervisorEmployeeName", "SecondarySupervisorEmployeeName",
    "TertiarySupervisorEmployeeName", "QuaternarySupervisorEmployeeName",
    "CountryCode", "TermReason", "FullTimeToPartTimeDate", "LastCheckDate",
    "PositionFamilyName", "AccrualProfileDesc", "BusinessTitlePositionSeat",
    "PositionLevel", "PrimaryAddressLine1", "PrimaryAddressLine2",
    "PrimaryCityMunicipality", "PrimaryState", "PrimaryZipCode", "RehireDate",
    "LastPositionChangeDate", "LastWorkedDate", "PositionTitlePositionInfo",
    "PayTypeDescription", "WorkLocationDescription", "WorkLocationAddress",
    "WorkLocationCity", "WorkLocationState", "WorkLocationZip", "WorkLocationCountry",
    "sales_sl_gradution", "sales_sl_start_date", "AnnualSalary", "EmploymentType", "Rate1"
]

CATALOG_SCHEMA = {
    "payroll": PAYROLL_COLUMNS,
    "employee": EMPLOYEE_COLUMNS,
}
```

A `FULL_CATALOG_SCHEMA` fixture may additionally include `accrual_events`, `performance_discussions`,
`personnel_action_form_changes`, `applicant_tracking_application`, and `applicant_tracking_requisition`
for the multi-join cases.

---

## PART 1 — POSITIVE CASES (correct extraction)

### P-01 · `prov-qualified-columns`

**Decision refs:** D62, D57, D44

**Input SQL:**
```sql
SELECT payroll.EmployeeCode, payroll.Amount
FROM payroll
WHERE payroll.RegisterType = 'EARN'
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected USES set:**
```
{(payroll, EmployeeCode), (payroll, Amount), (payroll, RegisterType)}
```

**Rationale:** All columns are already fully qualified; this is the baseline case that must work
before any resolution path is tested.

**Oracle note:** Executable against the real warehouse; `system.query_log.columns` should report
the same three columns. Include in the oracle validation job.

---

### P-02 · `prov-unqualified-resolve`

**Decision refs:** D62, D57, D44

**Input SQL:**
```sql
SELECT EmployeeCode, Amount
FROM payroll
WHERE RegisterType = 'EARN'
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected USES set:**
```
{(payroll, EmployeeCode), (payroll, Amount), (payroll, RegisterType)}
```

**Rationale:** `qualify_columns` must resolve bare column references to their owning table using the
catalog schema. This is the canonical unqualified-resolution path from D62.

**Oracle note:** Same query is runnable; include in oracle harness.

---

### P-03 · `prov-table-alias`

**Decision refs:** D62, D57

**Input SQL:**
```sql
SELECT p.EmployeeCode, p.Amount
FROM payroll AS p
WHERE p.RegisterType = 'EARN'
  AND p.PayPeriodStartDate >= '2025-01-01'
  AND p.PayPeriodEndDate < '2025-04-01'
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected USES set:**
```
{(payroll, EmployeeCode), (payroll, Amount), (payroll, RegisterType),
 (payroll, PayPeriodStartDate), (payroll, PayPeriodEndDate)}
```

**Rationale:** The alias `p` must be resolved back to `payroll`. A naive column-name extractor that
does not dereference aliases would produce `(p, EmployeeCode)` — a wrong result that would defeat
scope checking against the real table name.

---

### P-04 · `prov-join-two-tables`

**Decision refs:** D62, D57, D44

**Input SQL:**
```sql
SELECT e.EmployeeCode, e.Department, SUM(p.Amount) AS gross_earnings
FROM employee AS e
JOIN payroll AS p ON e.EmployeeCode = p.EmployeeCode
WHERE p.RegisterType = 'EARN'
  AND e.EmployeeStatus = 'A'
  AND p.PayPeriodStartDate >= '2025-01-01'
GROUP BY e.EmployeeCode, e.Department
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected USES set:**
```
{(employee, EmployeeCode), (employee, Department), (employee, EmployeeStatus),
 (payroll, Amount), (payroll, EmployeeCode), (payroll, RegisterType),
 (payroll, PayPeriodStartDate)}
```

**Rationale:** Multi-table join; columns from both sides must appear in the USES set. The JOIN
predicate columns (`EmployeeCode` on both sides) must be captured from each table independently.
This is also the canonical "fan-out" join scenario from the grain notes in payroll.yaml.

**Oracle note:** Core multi-table case; include in oracle harness.

---

### P-05 · `prov-avg-gross-pay-gated`

**Decision refs:** D57, D44, D62 — this is the canonical derived-aggregate USES case

**Input SQL:**
```sql
SELECT e.Department, AVG(p.Amount) AS avg_gross_pay
FROM employee AS e
JOIN payroll AS p ON e.EmployeeCode = p.EmployeeCode
WHERE p.RegisterType = 'EARN'
  AND p.PayPeriodStartDate >= '2025-01-01'
GROUP BY e.Department
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected USES set:**
```
{(employee, Department), (employee, EmployeeCode),
 (payroll, Amount), (payroll, EmployeeCode), (payroll, RegisterType),
 (payroll, PayPeriodStartDate)}
```

**Rationale:** `AVG(p.Amount)` returns a derived aggregate — no raw `payroll.Amount` value appears
in the output column — but `payroll.Amount` is unambiguously REFERENCED in the query computation.
D57 and D44 both state explicitly that "an `AVG(gross_pay)` is gated even though it returns no raw
payroll column." A user without `payroll.Amount` in their scope MUST be blocked even though they only
see the departmental average. This case is the single most important security invariant for the unit.

**Oracle note:** Run against real warehouse; `system.query_log.columns` must include `payroll.Amount`.
If the oracle does NOT report it, that is an oracle gap to document (ClickHouse may report aggregated
columns differently than accessed columns).

---

### P-06 · `prov-where-subquery-forbidden-table`

**Decision refs:** D57, D44, D62

**Input SQL:**
```sql
SELECT e.EmployeeCode, e.Department
FROM employee AS e
WHERE e.EmployeeCode IN (
    SELECT p.EmployeeCode
    FROM payroll AS p
    WHERE p.RegisterType = 'EARN'
      AND p.Amount > 100000
      AND p.PayPeriodStartDate >= '2025-01-01'
)
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected USES set:**
```
{(employee, EmployeeCode), (employee, Department),
 (payroll, EmployeeCode), (payroll, RegisterType), (payroll, Amount),
 (payroll, PayPeriodStartDate)}
```

**Rationale:** The subquery references `payroll.Amount` even though the outer query only projects
`employee` columns. A user without payroll scope must be blocked. This covers the "forbidden table in
subquery" injection vector.

---

### P-07 · `prov-cte-single`

**Decision refs:** D62, D57, D44

**Input SQL:**
```sql
WITH earn_rows AS (
    SELECT EmployeeCode, Amount, PayPeriodStartDate
    FROM payroll
    WHERE RegisterType = 'EARN'
      AND PayPeriodStartDate >= '2025-01-01'
)
SELECT e.EmployeeCode, e.Department, er.Amount
FROM employee AS e
JOIN earn_rows AS er ON e.EmployeeCode = er.EmployeeCode
WHERE e.EmployeeStatus = 'A'
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected USES set:**
```
{(payroll, EmployeeCode), (payroll, Amount), (payroll, RegisterType),
 (payroll, PayPeriodStartDate),
 (employee, EmployeeCode), (employee, Department), (employee, EmployeeStatus)}
```

**Rationale:** CTE `earn_rows` aliases `payroll`; the outer query must still be attributed back to
the base table columns. Unresolved CTE references would collapse to CTE-scoped names rather than
real table columns, defeating scope enforcement.

---

### P-08 · `prov-cte-chained`

**Decision refs:** D62, D57, D44

**Input SQL:**
```sql
WITH
earn_rows AS (
    SELECT EmployeeCode, SUM(Amount) AS total_earn
    FROM payroll
    WHERE RegisterType = 'EARN'
      AND PayPeriodStartDate >= '2025-01-01'
    GROUP BY EmployeeCode
),
dept_totals AS (
    SELECT e.Department, SUM(er.total_earn) AS dept_earn
    FROM employee AS e
    JOIN earn_rows AS er ON e.EmployeeCode = er.EmployeeCode
    WHERE e.EmployeeStatus = 'A'
    GROUP BY e.Department
)
SELECT Department, dept_earn
FROM dept_totals
ORDER BY dept_earn DESC
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected USES set:**
```
{(payroll, EmployeeCode), (payroll, Amount), (payroll, RegisterType),
 (payroll, PayPeriodStartDate),
 (employee, EmployeeCode), (employee, Department), (employee, EmployeeStatus)}
```

**Rationale:** Two-level CTE chain; each level must be fully unpacked back to base tables. The
final SELECT references only CTE names — the extractor must trace all the way through.

---

### P-09 · `prov-window-function`

**Decision refs:** D62, D57

**Input SQL:**
```sql
SELECT
    e.EmployeeCode,
    e.Department,
    p.Amount,
    p.PayDate,
    ROW_NUMBER() OVER (PARTITION BY e.Department ORDER BY p.Amount DESC) AS rank_in_dept
FROM employee AS e
JOIN payroll AS p ON e.EmployeeCode = p.EmployeeCode
WHERE p.RegisterType = 'EARN'
  AND p.PayPeriodStartDate >= '2025-01-01'
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected USES set:**
```
{(employee, EmployeeCode), (employee, Department), (employee, EmployeeCode),
 (payroll, Amount), (payroll, PayDate), (payroll, EmployeeCode),
 (payroll, RegisterType), (payroll, PayPeriodStartDate)}
```

**Rationale:** Window function `PARTITION BY` and `ORDER BY` clauses reference columns that do not
appear in the output projection. All partition/order columns must be captured in the USES set. A
scope checker that only looks at SELECT-list columns would miss them.

---

### P-10 · `prov-argmax-combinator`

**Decision refs:** D62, D57

**Input SQL:**
```sql
SELECT
    e.Department,
    argMax(p.Amount, p.PayDate) AS latest_amount,
    argMax(p.PayPeriodEndDate, p.PayDate) AS latest_period_end
FROM employee AS e
JOIN payroll AS p ON e.EmployeeCode = p.EmployeeCode
WHERE p.RegisterType = 'EARN'
GROUP BY e.Department
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected USES set:**
```
{(employee, Department), (employee, EmployeeCode),
 (payroll, Amount), (payroll, PayDate), (payroll, PayPeriodEndDate),
 (payroll, EmployeeCode), (payroll, RegisterType)}
```

**Rationale:** `argMax(value_col, ordering_col)` is a ClickHouse aggregate combinator; the second
argument (`PayDate`) is an ordering reference that must be included in the USES set. `argMin` is
symmetric and should be covered by the same test logic.

---

### P-11 · `prov-count-with-where-forbidden`

**Decision refs:** D57, D44, D62

**Input SQL:**
```sql
SELECT count() AS payroll_rows
FROM payroll
WHERE RegisterType = 'EARN'
  AND Amount > 0
  AND PayPeriodStartDate >= '2025-01-01'
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected USES set:**
```
{(payroll, RegisterType), (payroll, Amount), (payroll, PayPeriodStartDate)}
```

**Rationale:** `count()` with no arguments returns a scalar integer — no raw column appears in the
output. Yet `payroll.Amount` is referenced in the WHERE clause and MUST gate the query for users
without payroll scope. This is the "COUNT with forbidden WHERE column" variant of the D57 canonical
example.

---

### P-12 · `prov-scratch-own-session`

**Decision refs:** D64, D62, D57

**Input SQL:**
```sql
SELECT s.employee_id, s.hire_date_override, e.Department
FROM scratch.s_sess_abc123_onboarding_data AS s
JOIN employee AS e ON s.employee_id = e.EmployeeCode
WHERE e.EmployeeStatus = 'A'
```

**Catalog schema:** `CATALOG_SCHEMA` (scratch table is NOT in the catalog — resolved by D69/OQ-4)

**Injected session_id:** `sess_abc123`

**Expected USES set (column provenance) — RESOLVED (D69/OQ-4):** Scratch tables are NOT
column-scope-checked; they are session-gated only. Scratch column references are accepted without
catalog qualification. The extractor returns the scratch table columns as parsed from the SQL itself
(not from `qualify_columns`, which is not run for scratch tables). Warehouse table columns are still
fully qualified at `(database.table, column)` per OQ-3/D69.
```
{(scratch.s_sess_abc123_onboarding_data, employee_id),
 (scratch.s_sess_abc123_onboarding_data, hire_date_override),
 (dbpcm_warehouse.employee, Department), (dbpcm_warehouse.employee, EmployeeCode),
 (dbpcm_warehouse.employee, EmployeeStatus)}
```

**Expected scratch isolation check result:** PASS — scratch table name matches
`s_<injected_session_id>_*`

**Rationale:** D69/OQ-4: scratch tables are session-gated via the D64 `s_<sessionId>_*` name-match;
column scope enforcement applies only to warehouse tables. The MCP must: (a) extract the referenced
scratch table name from the USES set and (b) verify it matches the injected session_id. Scratch
column references are accepted without catalog qualification because scratch schema is user-supplied
at upload time and unknown to the Semantic Catalog.

---

### P-13 · `prov-multi-join-three-tables`

**Decision refs:** D62, D57, D44

**Input SQL:**
```sql
SELECT
    e.EmployeeCode,
    e.Department,
    e.EmployeeStatus,
    SUM(p.Amount) AS total_earn,
    MAX(paf.NewValue) AS latest_position_change
FROM employee AS e
JOIN payroll AS p ON e.EmployeeCode = p.EmployeeCode
JOIN personnel_action_form_changes AS paf ON e.EmployeeCode = paf.EmployeeCode
WHERE p.RegisterType = 'EARN'
  AND p.PayPeriodStartDate >= '2025-01-01'
  AND paf.FieldName = 'Position'
GROUP BY e.EmployeeCode, e.Department, e.EmployeeStatus
```

**Catalog schema:** `FULL_CATALOG_SCHEMA` (includes personnel_action_form_changes)

**Expected USES set:**
```
{(employee, EmployeeCode), (employee, Department), (employee, EmployeeStatus),
 (payroll, Amount), (payroll, EmployeeCode), (payroll, RegisterType),
 (payroll, PayPeriodStartDate),
 (personnel_action_form_changes, EmployeeCode), (personnel_action_form_changes, NewValue),
 (personnel_action_form_changes, FieldName)}
```

**Rationale:** Three-table fan-out joins are the realistic maximum complexity in typical HR queries.
Demonstrates that the extractor scales linearly with join complexity.

---

## PART 2 — ADVERSARIAL / NEGATIVE CASES

### A-01 · `prov-unparseable-failclosed`

**Decision refs:** D63, D52, D57, D44

**Input SQL:**
```sql
SELECT * FROM generateRandom('a UInt8, b Float32', 1, 10, 2) LIMIT 5
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected behavior:** `ProvenanceExtractionError` is raised (or equivalent: returns an empty/None
sentinel that the caller must treat as rejection).

**Critically, the following must NOT happen:** the function returns an empty set `{}` that is
silently interpreted as "no columns used" and therefore "in scope" — that is fail-OPEN.

**Fail-closed contract (D63):** unparseable query → no provenance → consumer REJECTS.

**Rationale:** `generateRandom` is a ClickHouse table-valued function that `sqlglot` is unlikely to
parse correctly. This is the canonical "exotic syntax" gap case from D62. The fail-closed invariant
is: the empty/error return must unambiguously signal parse failure, not "zero columns accessed."
The consumer (MCP D57/D63) maps this to reject-and-alert, never run.

**Oracle note:** Cannot validate against `system.query_log.columns` (query is rejected before
execution). Track as a parser-gap false-reject in the D63 false-reject rate metric.

---

### A-02 · `prov-unparseable-clickhouse-lambdas`

**Decision refs:** D63, D52, D62

**Input SQL:**
```sql
SELECT arrayMap(x -> x * 2, groupArray(p.Amount)) AS doubled_amounts,
       EmployeeCode
FROM payroll AS p
WHERE RegisterType = 'EARN'
  AND PayPeriodStartDate >= '2025-01-01'
GROUP BY EmployeeCode
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected behavior — RESOLVED (D69/OQ-1):** Lambda-body columns ARE part of the USES set.
Exactly two outcomes are acceptable:
- **Outcome A (parse succeeds and lambda body is walked):** USES set contains
  `{(scratch.payroll, Amount), (scratch.payroll, EmployeeCode), (scratch.payroll, RegisterType), (scratch.payroll, PayPeriodStartDate)}` — lambda body was fully walked.
- **Outcome B (parse fails or lambda body is not proved walked):** `ProvenanceExtractionError`
  — fail-closed, query rejected. This is the required behavior if `sqlglot` silently drops body
  columns (D69: no silent skip, consistent with D63).

**What is NEVER acceptable:** parse "succeeds" but `payroll.Amount` is absent from the USES set
because the lambda body was silently skipped. This is an under-extraction and a security gap.

**Rationale:** Higher-order / lambda functions (`arrayMap`, `arrayFilter`, `arraySplit`) are a
documented `sqlglot` dialect-gap risk called out in D62. The implementer must empirically verify
which outcome `sqlglot` produces; if Outcome B, record as a known false-reject feeding the D63
false-reject rate. D69 locks the contract: silent skip is never acceptable (OQ-1 resolved).

---

### A-03 · `prov-select-star-single-table`

**Decision refs:** D62, D57, D44

**Input SQL:**
```sql
SELECT *
FROM payroll
WHERE RegisterType = 'EARN'
  AND PayPeriodStartDate >= '2025-01-01'
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected behavior — RESOLVED (D69/OQ-2):** `SELECT *` expands to ALL columns of the referenced
table(s) via the catalog schema. **Outcome A is the locked behavior:**
- **Outcome A (expand via catalog):** USES set = all 15 columns of `payroll` from the catalog
  schema (i.e. all entries in `PAYROLL_COLUMNS`). The query passes only if every expanded column
  ∈ scope; any out-of-scope column → reject. If `payroll` were not in the catalog (columns cannot
  be enumerated) → fail-closed / reject.

**What is NEVER acceptable:** `SELECT *` is treated as "no columns" (empty USES set) and the query
runs unchecked. Outcome B (fail-closed without expansion) was also considered and rejected — the
canonical behavior is expansion, not categorical refusal.

**Rationale:** Scope is column-level (D57); the engine does not strip columns (option 2b rejected).
A representative-column check would under-gate and could leak columns like `gross_pay`. Catalog
expansion preserves "referenced columns ⊄ scope ⇒ reject" intact for the star case (D69, OQ-2
resolved).

---

### A-04 · `prov-select-star-join`

**Decision refs:** D62, D57

**Input SQL:**
```sql
SELECT *
FROM employee AS e
JOIN payroll AS p ON e.EmployeeCode = p.EmployeeCode
WHERE p.RegisterType = 'EARN'
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected behavior — RESOLVED (D69/OQ-2):** Same locked rule as A-03. `SELECT *` expands via
catalog to ALL columns of ALL referenced tables:
- **Outcome A (locked):** USES set = all columns of `employee` (all entries in `EMPLOYEE_COLUMNS`)
  + all columns of `payroll` (all entries in `PAYROLL_COLUMNS`), as well as the JOIN predicate
  columns already in both sets.

A user with `employee` scope but not `payroll` scope must be blocked because `payroll` columns
are in the expanded USES set. This is the multi-table `SELECT *` variant; the same catalog-expansion
rule applies (D69, OQ-2 resolved).

---

### A-05 · `prov-union-forbidden-table`

**Decision refs:** D57, D44, D62

**Input SQL:**
```sql
SELECT EmployeeCode, Department, NULL AS Amount
FROM employee
WHERE EmployeeStatus = 'A'

UNION ALL

SELECT EmployeeCode, NULL AS Department, Amount
FROM payroll
WHERE RegisterType = 'EARN'
  AND PayPeriodStartDate >= '2025-01-01'
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected USES set:**
```
{(employee, EmployeeCode), (employee, Department), (employee, EmployeeStatus),
 (payroll, EmployeeCode), (payroll, Amount), (payroll, RegisterType),
 (payroll, PayPeriodStartDate)}
```

**Rationale:** UNION injection attempt — the second branch accesses `payroll.Amount`, which must
appear in the USES set even though the first branch projects `NULL` in its place. A scope checker
that only walks the first UNION branch would miss the payroll access entirely.

---

### A-06 · `prov-comment-trick-union`

**Decision refs:** D57, D63, D62

**Input SQL:**
```sql
SELECT EmployeeCode, Department
FROM employee
WHERE EmployeeStatus = 'A'
-- UNION ALL SELECT EmployeeCode, Amount FROM payroll WHERE RegisterType = 'EARN'
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected USES set:**
```
{(employee, EmployeeCode), (employee, Department), (employee, EmployeeStatus)}
```

**Rationale:** Comment-stripped UNION — the payroll branch is commented out. The extractor must NOT
extract `payroll.Amount` (the comment is not executable SQL). This is the inverse of A-05: prove
that comment stripping does not introduce false positives in the USES set. If the parser returns
`payroll.Amount`, that is an over-extraction bug that could wrongly block a legitimate query
(correctness bug, not a security bug, but still a hard failure).

---

### A-07 · `prov-stacked-statements`

**Decision refs:** D57, D63, D62

**Input SQL:**
```sql
SELECT EmployeeCode, Department FROM employee WHERE EmployeeStatus = 'A';
SELECT EmployeeCode, Amount FROM payroll WHERE RegisterType = 'EARN'
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected behavior:** `ProvenanceExtractionError` — stacked statements are not a valid single
SELECT and must be treated as an unparseable (or explicitly multi-statement, thus invalid) input.
Fail-closed: reject, do not run either statement. Do not extract from just the first statement and
silently discard the second.

**Rationale:** Multi-statement injection (semicolon-separated). `sqlglot`'s `parse_one` (singular)
is specified in D62; it is expected to raise on multiple statements. The test verifies that behavior
rather than assuming it.

---

### A-08 · `prov-subquery-hidden-forbidden-column`

**Decision refs:** D57, D44, D62

**Input SQL:**
```sql
SELECT dept_summary.Department, dept_summary.row_count
FROM (
    SELECT e.Department, COUNT(*) AS row_count
    FROM employee AS e
    JOIN payroll AS p ON e.EmployeeCode = p.EmployeeCode
    WHERE p.RegisterType = 'EARN'
      AND p.Amount > 50000
      AND p.PayPeriodStartDate >= '2025-01-01'
    GROUP BY e.Department
) AS dept_summary
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected USES set:**
```
{(employee, Department), (employee, EmployeeCode),
 (payroll, EmployeeCode), (payroll, RegisterType), (payroll, Amount),
 (payroll, PayPeriodStartDate)}
```

**Rationale:** Derived subquery wrapping — the outer query projects only `Department` and `row_count`
(a count), but the inner subquery references `payroll.Amount` in a WHERE predicate. The outer result
exposes no raw payroll data. `payroll.Amount` must still appear in the USES set. This is the
"derived-subquery wrapper to hide forbidden access" injection pattern.

---

### A-09 · `prov-scratch-crosssession-rejected`

**Decision refs:** D64, D57, D63

**Input SQL:**
```sql
SELECT s.EmployeeCode, s.salary_adjustment, e.Department
FROM scratch.s_sess_xyz789_compensation_data AS s
JOIN employee AS e ON s.EmployeeCode = e.EmployeeCode
WHERE e.EmployeeStatus = 'A'
```

**Catalog schema:** `CATALOG_SCHEMA`

**Injected session_id:** `sess_abc123`  (different from `sess_xyz789` in the SQL)

**Expected behavior:** Query rejected — the scratch table name `s_sess_xyz789_compensation_data`
does not match `s_<injected_session_id>_*` (`s_sess_abc123_*`). Rejection surfaces via the graceful
denial path (D64, D63).

**Critically, column provenance extraction itself may succeed** (the table name is parseable), but
the cross-session scratch check at the MCP layer must detect the mismatch and reject.

**Rationale:** Cross-session scratch access attempt. D64 specifies that the MCP enumerates referenced
scratch tables and rejects any that do not match the injected session_id. The model never sees or
supplies session_id (D5), so this attack vector can only occur if the model somehow constructs a
known session_id string — the check must hold regardless.

---

### A-10 · `prov-scratch-malformed-name-rejected`

**Decision refs:** D64, D57

**Input SQL:**
```sql
SELECT * FROM scratch.compensation_export
JOIN employee AS e ON compensation_export.EmployeeCode = e.EmployeeCode
```

**Catalog schema:** `CATALOG_SCHEMA`

**Injected session_id:** `sess_abc123`

**Expected behavior:** Query rejected — `scratch.compensation_export` does not conform to the
`s_<sessionId>_<file>` naming convention. The name is neither in-scope for the injected session
nor does it follow the required format.

**Rationale:** Malformed scratch table name (no session prefix). Proves that even a scratch-database
reference without a valid session-prefix pattern is rejected, not silently allowed.

---

### A-11 · `prov-ddl-blocked`

**Decision refs:** D57, D63

**Input SQL:**
```sql
CREATE TABLE payroll_export AS SELECT * FROM payroll WHERE RegisterType = 'EARN'
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected behavior:** Rejected — DDL is blocked at the MCP level before provenance extraction
is even invoked. If provenance extraction is called, it must either raise `ProvenanceExtractionError`
or the MCP layer must intercept before calling it.

**Rationale:** DDL injection. `runQuery` blocks INSERT/UPDATE/DELETE/DDL per D02/D21. This test
verifies the DDL block is upstream of any provenance extraction path, not a fallback from it.

---

### A-12 · `prov-insert-blocked`

**Decision refs:** D57, D63, D21

**Input SQL:**
```sql
INSERT INTO payroll (EmployeeCode, Amount, RegisterType)
SELECT EmployeeCode, Amount * 1.1, RegisterType FROM payroll WHERE RegisterType = 'EARN'
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected behavior:** Rejected before provenance extraction — INSERT is blocked at the MCP
read-only gate.

**Rationale:** Complementary to A-11. INSERT is not a SELECT, but the subquery does reference
`payroll.Amount`. Tests that the read-only guard fires before the provenance extractor sees it.

---

### A-13 · `prov-with-clause-forbidden-col-hidden`

**Decision refs:** D57, D44, D62

**Input SQL:**
```sql
WITH anon_counts AS (
    SELECT e.Department, COUNT(*) AS headcount, SUM(p.Amount) AS dept_total
    FROM employee AS e
    JOIN payroll AS p ON e.EmployeeCode = p.EmployeeCode
    WHERE p.RegisterType = 'EARN'
      AND p.PayPeriodStartDate >= '2025-01-01'
    GROUP BY e.Department
)
SELECT Department, headcount
FROM anon_counts
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected USES set:**
```
{(employee, Department), (employee, EmployeeCode),
 (payroll, Amount), (payroll, EmployeeCode), (payroll, RegisterType),
 (payroll, PayPeriodStartDate)}
```

**Rationale:** CTE hiding — the outer SELECT only projects `Department` and `headcount` (a count),
masking `payroll.Amount` which is computed in the CTE but not surfaced. `payroll.Amount` must still
appear in the USES set because it is referenced inside the CTE definition. This is the CTE-level
analog of A-08.

---

### A-14 · `prov-case-expression-forbidden-col`

**Decision refs:** D57, D44, D62

**Input SQL:**
```sql
SELECT
    e.EmployeeCode,
    e.Department,
    CASE
        WHEN p.Amount > 100000 THEN 'high'
        WHEN p.Amount > 50000 THEN 'mid'
        ELSE 'standard'
    END AS pay_band
FROM employee AS e
JOIN payroll AS p ON e.EmployeeCode = p.EmployeeCode
WHERE p.RegisterType = 'EARN'
  AND p.PayPeriodStartDate >= '2025-01-01'
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected USES set:**
```
{(employee, EmployeeCode), (employee, Department), (employee, EmployeeCode),
 (payroll, Amount), (payroll, EmployeeCode), (payroll, RegisterType),
 (payroll, PayPeriodStartDate)}
```

**Rationale:** CASE expression that returns a string label — no raw `Amount` value is output, but
`payroll.Amount` is referenced in all WHEN predicates. A user without payroll scope asking for a
pay-band classification must still be blocked. The CASE expression is semantically equivalent to
`AVG(gross_pay)` in D57's canonical example.

---

### A-15 · `prov-cross-database-reference`

**Decision refs:** D62, D57, D63

**Input SQL:**
```sql
SELECT e.EmployeeCode, e.Department, ext.salary_band
FROM dbpcm_warehouse.employee AS e
JOIN external_hr_db.salary_bands AS ext ON e.EmployeeCode = ext.EmployeeCode
WHERE e.EmployeeStatus = 'A'
```

**Catalog schema:** `CATALOG_SCHEMA` (does not include `external_hr_db.salary_bands`)

**Expected behavior:** `ProvenanceExtractionError` — the extractor cannot qualify columns for
`external_hr_db.salary_bands` because that table is not in the catalog schema fed to
`qualify_columns`. Fail-closed: query is rejected.

**Rationale:** Cross-database reference to a table not in the Semantic Catalog. D52 specifies that
fail behavior on qualification failure is fail-closed for the D57/D63 consumer. An unknown table
must never be assumed in-scope. This also covers the `uncatalogued-table` path noted in docs/02
(getTableSchema structural-only path) — even if structural introspection exists, if the table is
not in the catalog schema dict, column qualification must fail.

---

### A-16 · `prov-fully-qualified-database-prefix`

**Decision refs:** D62, D57

**Input SQL:**
```sql
SELECT dbpcm_warehouse.payroll.EmployeeCode,
       dbpcm_warehouse.payroll.Amount
FROM dbpcm_warehouse.payroll
WHERE dbpcm_warehouse.payroll.RegisterType = 'EARN'
  AND dbpcm_warehouse.payroll.PayPeriodStartDate >= '2025-01-01'
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected USES set — RESOLVED (D69/OQ-3):** The canonical USES pair is FULLY-QUALIFIED
`(database.table, column)`. Three-part qualification is the locked normalization:
```
{(dbpcm_warehouse.payroll, EmployeeCode), (dbpcm_warehouse.payroll, Amount),
 (dbpcm_warehouse.payroll, RegisterType), (dbpcm_warehouse.payroll, PayPeriodStartDate)}
```

**Rationale:** D69 locks the canonical pair as `(database.table, column)` — a three-part
granularity — to disambiguate warehouse vs. scratch (D64) vs. cross-database references and prevent
bare-name namespace collisions. Consequence: the catalog schema dict fed to `qualify_columns` must
key tables as `database.table` (e.g. `"dbpcm_warehouse.payroll"`), and the scope vector injected
by D5 must express allowed columns as `database.table.column` triples. The extractor resolves the
three-part SQL reference `dbpcm_warehouse.payroll.Amount` directly to
`(dbpcm_warehouse.payroll, Amount)` without stripping the database prefix. (OQ-3 resolved.)

---

### A-17 · `prov-empty-string-input`

**Decision refs:** D62, D63

**Input SQL:** `""` (empty string)

**Catalog schema:** `CATALOG_SCHEMA`

**Expected behavior:** `ProvenanceExtractionError` — fail-closed. Empty SQL is not a valid query.

---

### A-18 · `prov-whitespace-only-input`

**Decision refs:** D62, D63

**Input SQL:** `"   \n\t  "` (whitespace only)

**Catalog schema:** `CATALOG_SCHEMA`

**Expected behavior:** `ProvenanceExtractionError` — fail-closed. Whitespace-only input is not a
valid query. Must not return an empty set (which would pass scope check as "no columns").

---

### A-19 · `prov-explain-statement`

**Decision refs:** D62, D63

**Input SQL:**
```sql
EXPLAIN SELECT EmployeeCode, Amount FROM payroll WHERE RegisterType = 'EARN'
```

**Catalog schema:** `CATALOG_SCHEMA`

**Expected behavior — RESOLVED (D69/OQ-5):** `extract_column_provenance` is NEVER called on
EXPLAIN queries. This is a **caller precondition** enforced at the call site (the MCP and any other
D52 consumer), not handled inside the extractor. The `explainQuery` tool is a separate code path;
EXPLAIN statements never reach `runQuery` and therefore never reach the provenance extractor.

**Test posture:** This case documents the precondition rather than testing extractor behavior. The
test should assert that the MCP's EXPLAIN gate fires before the extractor is invoked — i.e., assert
that `extract_column_provenance` is NOT called when an EXPLAIN query is received. If for any reason
the extractor is called with an EXPLAIN statement, it may raise `ProvenanceExtractionError` (the
most conservative behavior), but this path must not occur in production. (OQ-5 resolved.)

---

### A-20 · `prov-nonselect-show`

**Decision refs:** D62, D63

**Input SQL:** `SHOW TABLES`

**Catalog schema:** `CATALOG_SCHEMA`

**Expected behavior:** `ProvenanceExtractionError` — SHOW produces no column-provenance; fail-closed
if called in a context expecting USES.

---

## PART 3 — DETERMINISM AND ISOLATION CASES

These cases verify the unit properties (no I/O, deterministic, isolated) required for Layer-1 tests
per docs/11-testing.md.

### D-01 · `prov-deterministic-same-input`

**Input:** Any of P-05 (`prov-avg-gross-pay-gated`)

**Assertion:** Calling `extract_column_provenance` with identical `(sql, schema)` inputs in the
same process 100 times always returns the same `frozenset`. No randomness, no caching side-effects
between calls.

---

### D-02 · `prov-order-independent`

**Input SQL A:**
```sql
SELECT e.Department, AVG(p.Amount) AS avg_earn
FROM payroll AS p JOIN employee AS e ON p.EmployeeCode = e.EmployeeCode
WHERE p.RegisterType = 'EARN' GROUP BY e.Department
```

**Input SQL B:**
```sql
SELECT e.Department, AVG(p.Amount) AS avg_earn
FROM employee AS e JOIN payroll AS p ON e.EmployeeCode = p.EmployeeCode
WHERE p.RegisterType = 'EARN' GROUP BY e.Department
```

**Expected:** Both return the same USES set. The join order in the FROM clause must not affect
which columns are attributed to which table.

---

### D-03 · `prov-case-insensitive-keywords`

**Input SQL:**
```sql
select employeecode, amount from payroll where registertype = 'EARN'
```

**Catalog schema:** `CATALOG_SCHEMA` (with lowercase `payroll` key, columns as authored)

**Expected:** Same USES set as P-02. SQL keyword case must not affect extraction.

**Note:** Column name case-sensitivity depends on the ClickHouse collation and the catalog schema
dict key casing. The test must use catalog keys that match the casing strategy chosen by the
implementation. Document the normalization contract.

---

## PART 4 — ORACLE VALIDATION CASES

These cases are marked for later validation against `system.query_log.columns` (the D62
ground-truth oracle) once a real ClickHouse instance is available. They are run as unit tests
against the extractor now; the oracle comparison is a separate Layer-2/CI job.

| Case slug | Oracle runnable? | Why / caveat |
|---|---|---|
| `prov-qualified-columns` (P-01) | Yes | Straightforward; oracle baseline. |
| `prov-unqualified-resolve` (P-02) | Yes | Core qualification path. |
| `prov-join-two-tables` (P-04) | Yes | Multi-table attribution. |
| `prov-avg-gross-pay-gated` (P-05) | Yes | Most important; verify oracle includes `Amount`. |
| `prov-cte-single` (P-07) | Yes | CTE unpacking. |
| `prov-window-function` (P-09) | Yes | Partition/order cols. |
| `prov-argmax-combinator` (P-10) | Yes | ClickHouse-specific; oracle may behave unexpectedly. |
| `prov-count-with-where-forbidden` (P-11) | Yes | WHERE-col-only case. |
| `prov-unparseable-failclosed` (A-01) | No | Query is rejected; never reaches `query_log`. |
| `prov-union-forbidden-table` (A-05) | Yes | Both UNION branches must appear. |
| `prov-subquery-hidden-forbidden-column` (A-08) | Yes | Inner-subquery attribution. |
| `prov-with-clause-forbidden-col-hidden` (A-13) | Yes | CTE + hidden column. |

**Oracle harness design note (from D62/D11-testing):** the oracle job replays the runnable cases
against the seeded ClickHouse fixture, captures `system.query_log.columns`, and diffs against the
extractor's output. Discrepancies are classified:
- Extractor has column, oracle does not: over-extraction (false positive in scope blocking — a
  correctness bug that may cause spurious rejections).
- Oracle has column, extractor does not: under-extraction (false negative — a security gap; the
  extractor missed a referenced column, allowing an out-of-scope query through).

Under-extraction is a security bug and must be treated with zero tolerance. Over-extraction is a
reliability bug (spurious rejections inflate the D63 false-reject rate).

---

## Open Questions for Human Review

All five open questions are now RESOLVED. Decisions recorded in **D69 (locked, 2026-06-30)**
in `docs/decisions/DECISIONS.md`. The test plan is implementation-ready.

**OQ-1 (lambda/higher-order) — RESOLVED → D69.**
Lambda-body column references ARE part of the USES set. If `sqlglot` cannot prove it walked the
lambda body, the extractor MUST raise `ProvenanceExtractionError` (fail-closed, no silent skip).
See A-02 for the updated expected behavior.

**OQ-2 (SELECT \* expansion) — RESOLVED → D69.**
`SELECT *` is expanded to ALL columns of each referenced table via the catalog schema. Query passes
only if every expanded column ∈ scope; any out-of-scope column → reject; table not in catalog →
fail-closed. See A-03 and A-04 for updated expected behavior.

**OQ-3 (USES pair normalization) — RESOLVED → D69.**
The canonical USES pair is FULLY-QUALIFIED `(database.table, column)` — three-part granularity.
Scope vectors and catalog schema dict keys use the same three-part granularity. See A-16 for the
updated expected USES set and the fixture note above for implementation impact.

**OQ-4 (scratch table columns) — RESOLVED → D69.**
Scratch tables are NOT column-scope-checked. Scratch column references are accepted without catalog
qualification; the session-ID name-match (D64) is the gate. `qualify_columns` is not run for
scratch tables. See P-12 for the updated expected USES set.

**OQ-5 (EXPLAIN statement behavior) — RESOLVED → D69.**
`extract_column_provenance` is NEVER called on EXPLAIN queries. This is a caller precondition,
enforced at the call site, not inside the extractor. See A-19 for the updated test posture.

---

## Traceability Matrix

| Test slug | Decision(s) | Security invariant proven |
|---|---|---|
| `prov-qualified-columns` | D62, D57, D44 | Baseline extraction |
| `prov-unqualified-resolve` | D62, D57, D44 | qualify_columns resolves unqualified refs |
| `prov-table-alias` | D62, D57 | Alias dereferencing |
| `prov-join-two-tables` | D62, D57, D44 | Multi-table attribution |
| `prov-avg-gross-pay-gated` | D57, D44, D62 | **Core: derived aggregate gates forbidden column** |
| `prov-where-subquery-forbidden-table` | D57, D44, D62 | Subquery hidden access |
| `prov-cte-single` | D62, D57, D44 | CTE → base table attribution |
| `prov-cte-chained` | D62, D57, D44 | Multi-level CTE chain |
| `prov-window-function` | D62, D57 | Partition/order clause columns |
| `prov-argmax-combinator` | D62, D57 | ClickHouse-specific aggregate |
| `prov-count-with-where-forbidden` | D57, D44, D62 | COUNT with WHERE on forbidden column |
| `prov-scratch-own-session` | D64, D62, D57 | Scratch isolation pass |
| `prov-multi-join-three-tables` | D62, D57, D44 | Three-table fan-out |
| `prov-unparseable-failclosed` | D63, D52, D57, D44 | **Core: fail-closed on parse failure** |
| `prov-unparseable-clickhouse-lambdas` | D63, D52, D62 | Lambda/higher-order gap |
| `prov-select-star-single-table` | D62, D57, D44 | SELECT * expansion |
| `prov-select-star-join` | D62, D57 | SELECT * multi-table |
| `prov-union-forbidden-table` | D57, D44, D62 | UNION both-branch extraction |
| `prov-comment-trick-union` | D57, D63, D62 | Comment stripping |
| `prov-stacked-statements` | D57, D63, D62 | Multi-statement rejection |
| `prov-subquery-hidden-forbidden-column` | D57, D44, D62 | Derived subquery wrapper |
| `prov-scratch-crosssession-rejected` | D64, D57, D63 | Cross-session scratch blocked |
| `prov-scratch-malformed-name-rejected` | D64, D57 | Malformed scratch name blocked |
| `prov-ddl-blocked` | D57, D63 | DDL blocked upstream |
| `prov-insert-blocked` | D57, D63, D21 | INSERT blocked upstream |
| `prov-with-clause-forbidden-col-hidden` | D57, D44, D62 | CTE hides forbidden column |
| `prov-case-expression-forbidden-col` | D57, D44, D62 | CASE expr references forbidden column |
| `prov-cross-database-reference` | D62, D57, D63 | Unknown DB/table → fail-closed |
| `prov-fully-qualified-database-prefix` | D62, D57 | Three-part name normalization |
| `prov-empty-string-input` | D62, D63 | Empty input → fail-closed |
| `prov-whitespace-only-input` | D62, D63 | Whitespace input → fail-closed |
| `prov-explain-statement` | D62, D63 | EXPLAIN behavior (see OQ-5) |
| `prov-nonselect-show` | D62, D63 | Non-SELECT → fail-closed |
| `prov-deterministic-same-input` | D62 | Determinism invariant |
| `prov-order-independent` | D62 | Join-order independence |
| `prov-case-insensitive-keywords` | D62 | Keyword case normalization |
