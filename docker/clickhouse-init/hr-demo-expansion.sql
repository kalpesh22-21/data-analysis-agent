-- =============================================================================
-- Demo-data expansion — enough employees for a MULTI-ROW answer.
--
-- WHY THIS EXISTS
-- The base seed leaves the CLIENT_A/PC01 tenant with three employees across three
-- departments, and `user_employee_access` entitles only two of them. Every
-- "by department" answer therefore collapsed to one or two rows, and
-- `bp-departments-above-company-average-salary` — a COMPOSED blueprint — always
-- returned a single row, so the multi-row table path could never be exercised
-- end-to-end against real data.
--
-- Salaries below are chosen so TWO departments clear the company average rather
-- than one (Engineering 125000 and Sales ~108333 against a company mean of
-- 100000). That is the whole point: a single-row result does not exercise a table.
--
-- RE-RUNNABLE by construction: each INSERT is preceded by a DELETE of the codes it
-- writes. Do not remove those — they are what makes a second apply a no-op.
--
-- It is tempting to rely on the engine instead. Do not: despite what
-- hr-4tables-snake-migration.sql declares, the DEPLOYED table is
--
--     MergeTree ORDER BY employee_code
--
-- a PLAIN MergeTree, so nothing ever collapses duplicates and OPTIMIZE ... FINAL
-- does nothing for them. Assuming otherwise cost real accuracy here: re-applying
-- this file twice left 15 rows for 7 employees and walked Engineering's average
-- salary 125000 -> 126666 -> 127500 and Sales' 108333 -> 112000 -> 113571. Note
-- WHICH aggregates lie — `count(DISTINCT employee_code)` looked perfectly correct
-- throughout, so headcount answers stayed right while every average silently drifted.
--
-- APPLY (surgical load into a RUNNING l2-ch — never `down -v`):
--   docker exec -i l2-ch clickhouse-client --user default --multiquery \
--     < docker/clickhouse-init/hr-demo-expansion.sql
--
-- RE-APPLY AFTER ~24h. `user_employee_access` carries
-- `TTL updated_at + INTERVAL 1 DAY`, and nothing repopulates it here:
-- `employee_access_enabled` defaults False and the compose stack sets no
-- EEACCESS_BASE_URL, so `ensure_employee_access_fresh` is a no-op. When the TTL
-- sweeps, `employee` and `payroll` silently return ZERO rows to every query and
-- the agent answers "no data" while looking perfectly healthy. Re-run this file.
-- =============================================================================

-- Idempotence guard: drop any previous copy of the seeded codes before rewriting
-- them (plain MergeTree — see the header).
ALTER TABLE dbpcm_warehouse.employee
    DELETE WHERE employee_code IN ('EMP006','EMP007','EMP008','EMP009')
    SETTINGS mutations_sync = 2;
ALTER TABLE dbpcm_warehouse.payroll
    DELETE WHERE employee_code IN ('EMP006','EMP007','EMP008','EMP009')
    SETTINGS mutations_sync = 2;

-- New employees, cloned from existing rows so every one of the ~131 columns keeps
-- a schema-valid value; only the identity, department and salary are overridden.
INSERT INTO dbpcm_warehouse.employee
SELECT * REPLACE ('EMP006' AS employee_code, 'D01' AS department_code,
                  'Engineering' AS department_name, 130000 AS annual_salary)
FROM dbpcm_warehouse.employee WHERE employee_code = 'EMP001';

INSERT INTO dbpcm_warehouse.employee
SELECT * REPLACE ('EMP007' AS employee_code, 'D02' AS department_code,
                  'Sales' AS department_name, 115000 AS annual_salary)
FROM dbpcm_warehouse.employee WHERE employee_code = 'EMP002';

INSERT INTO dbpcm_warehouse.employee
SELECT * REPLACE ('EMP008' AS employee_code, 'D02' AS department_code,
                  'Sales' AS department_name, 120000 AS annual_salary)
FROM dbpcm_warehouse.employee WHERE employee_code = 'EMP002';

INSERT INTO dbpcm_warehouse.employee
SELECT * REPLACE ('EMP009' AS employee_code, 'D03' AS department_code,
                  'Operations' AS department_name, 50000 AS annual_salary)
FROM dbpcm_warehouse.employee WHERE employee_code = 'EMP005';

-- Matching payroll, so the earnings/scratch-join blueprints see the new staff
-- instead of joining them to nothing. Amounts are scaled off the source row.
INSERT INTO dbpcm_warehouse.payroll
SELECT * REPLACE ('EMP006' AS employee_code, 'D01' AS department_code, amount * 1.1 AS amount)
FROM dbpcm_warehouse.payroll WHERE employee_code = 'EMP001';

INSERT INTO dbpcm_warehouse.payroll
SELECT * REPLACE ('EMP007' AS employee_code, 'D02' AS department_code, amount * 1.3 AS amount)
FROM dbpcm_warehouse.payroll WHERE employee_code = 'EMP002';

INSERT INTO dbpcm_warehouse.payroll
SELECT * REPLACE ('EMP008' AS employee_code, 'D02' AS department_code, amount * 1.4 AS amount)
FROM dbpcm_warehouse.payroll WHERE employee_code = 'EMP002';

INSERT INTO dbpcm_warehouse.payroll
SELECT * REPLACE ('EMP009' AS employee_code, 'D03' AS department_code, amount * 0.7 AS amount)
FROM dbpcm_warehouse.payroll WHERE employee_code = 'EMP005';

-- A COMPLETE access pull for the test principal.
--
-- Every entitled code must appear in ONE pull, because the row policy reads only
-- `pull_id = max(pull_id)`. Adding just the new codes under a higher pull_id would
-- REVOKE the ones left behind — that gate is what makes revocation work, and it
-- makes a partial pull silently destructive. Bump `pull_id` together when re-seeding.
INSERT INTO dbpcm_warehouse_security.user_employee_access
    (jti, employee_code, pull_id, updated_at)
VALUES
    ('TESTJTI001', 'EMP001', 2, now()),
    ('TESTJTI001', 'EMP002', 2, now()),
    ('TESTJTI001', 'EMP005', 2, now()),
    ('TESTJTI001', 'EMP006', 2, now()),
    ('TESTJTI001', 'EMP007', 2, now()),
    ('TESTJTI001', 'EMP008', 2, now()),
    ('TESTJTI001', 'EMP009', 2, now());

-- EMP003 / EMP004 are deliberately absent: they belong to CLIENT_B/PC02, which the
-- tenant half of the row policy already excludes. Entitlement and tenancy are
-- separate gates and this file only speaks to entitlement.
