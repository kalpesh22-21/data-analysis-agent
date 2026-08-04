-- =============================================================================
-- hr-4tables-snake-migration.sql
--
-- DEV migration (Phase 1 of the snake_case + row-level-security alignment).
--
-- Replaces the legacy PascalCase `dbpcm_warehouse.employee` and
-- `dbpcm_warehouse.payroll` with new snake_case physical tables, and CREATEs the
-- new `dbpcm_warehouse.department` and `dbpcm_warehouse.labor_allocation`
-- dimension tables — all four aligned column-for-column with the Semantic Catalog
-- YAMLs in clickhouse-api/app/semantic_catalog/data/ (case-sensitive, D70) so the
-- MCP `getTableSchema` overlay binds by exact column name.
--
-- Scope is STRICTLY these four tables + one security database/table. The other
-- seven warehouse tables (accrual_events, applicant_tracking_*, candidate_*,
-- performance_discussions, personnel_action_form_changes) are NOT touched.
--
-- Physical columns deliberately stripped from the catalog but required for RLS /
-- versioning are re-added here: `client_code` + `proc_center` on all four tables,
-- and `version` on labor_allocation (the ReplacingMergeTree version column).
--
-- Type deviations from the catalog (documented):
--   * payroll.employee_code — catalog says FixedString(4); widened to String so
--     the fixed test-principal contract codes (EMP001..EMP005, 6 chars) fit and
--     so the employee_code join / RLS IN-subquery aligns with
--     employee.employee_code (String) and user_employee_access.employee_code
--     (String). getTableSchema binds by column NAME, so the physical type may
--     differ from the catalog without breaking the overlay.
--
-- Idempotent: DROP TABLE IF EXISTS before CREATE; row policies are dropped before
-- (re)create; the security table + role use IF NOT EXISTS.
--
-- Load:  docker exec -i l2-ch clickhouse-client --multiquery < this-file
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. employee  (MergeTree ORDER BY employee_code)
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS dbpcm_warehouse.employee;

CREATE TABLE dbpcm_warehouse.employee
(
    employee_code String,
    clock_sequence Nullable(String),
    employee_gl_code Nullable(String),
    badge_number Nullable(String),
    employee_name Nullable(String),
    legal_employee_name Nullable(String),
    first_name Nullable(String),
    middle_name Nullable(String),
    middle_initial Nullable(String),
    last_name Nullable(String),
    suffix Nullable(String),
    nickname Nullable(String),
    preferred_first_name Nullable(String),
    preferred_middle_name Nullable(String),
    preferred_last_name Nullable(String),
    preferred_employee_suffix Nullable(String),
    employee_status Nullable(String),
    hire_date Nullable(DateTime64(6)),
    most_recent_hire_date Nullable(DateTime64(6)),
    rehire_date Nullable(DateTime64(6)),
    termination_date Nullable(DateTime64(6)),
    old_termination_date Nullable(DateTime64(6)),
    term_reason Nullable(String),
    seniority_date Nullable(DateTime64(6)),
    leave_absence_start Nullable(DateTime64(6)),
    leave_absence_end Nullable(DateTime64(6)),
    full_time_to_part_time_date Nullable(DateTime64(6)),
    last_worked_date Nullable(DateTime64(6)),
    department_code Nullable(String),
    department_name Nullable(String),
    lives_in_state Nullable(String),
    works_in_state Nullable(String),
    sui_state Nullable(String),
    local_tax_1 Nullable(String),
    date_of_birth Nullable(DateTime64(6)),
    marital_status_code Nullable(String),
    marital_status_description Nullable(String),
    gender Nullable(String),
    ethnic_background Nullable(String),
    country_code Nullable(String),
    street_address_1 Nullable(String),
    street_address_2 Nullable(String),
    city Nullable(String),
    state Nullable(String),
    zip_code Nullable(String),
    work_email Nullable(String),
    personal_email Nullable(String),
    secondary_email Nullable(String),
    primary_phone Nullable(String),
    primary_phone_type Nullable(String),
    secondary_phone Nullable(String),
    secondary_phone_type Nullable(String),
    primary_emergency_contact_phone Nullable(String),
    secondary_emergency_contact_phone Nullable(String),
    tertiary_emergency_contact_phone Nullable(String),
    primary_supervisor_ee_code Nullable(String),
    secondary_supervisor_ee_code Nullable(String),
    tertiary_supervisor_ee_code Nullable(String),
    quaternary_supervisor_ee_code Nullable(String),
    primary_supervisor_name Nullable(String),
    secondary_supervisor_name Nullable(String),
    tertiary_supervisor_name Nullable(String),
    quaternary_supervisor_name Nullable(String),
    primary_supervisor_email Nullable(String),
    secondary_supervisor_email Nullable(String),
    tertiary_supervisor_email Nullable(String),
    quaternary_supervisor_email Nullable(String),
    manager_level_code Nullable(String),
    manager_level_description Nullable(String),
    position_code Nullable(String),
    position_level Nullable(Int32),
    position_title_position_info Nullable(String),
    status_position_info Nullable(String),
    business_title_position_info Nullable(String),
    short_title Nullable(String),
    effective_date_position_info Nullable(DateTime64(6)),
    job_category Nullable(String),
    last_position_change_date Nullable(DateTime64(6)),
    position_seat_number Nullable(String),
    effective_date_position_seat Nullable(DateTime64(6)),
    status_position_seat Nullable(String),
    union_code Nullable(String),
    eeo1_category Nullable(String),
    work_location_description Nullable(String),
    work_location_address Nullable(String),
    work_location_city Nullable(String),
    work_location_state Nullable(String),
    work_location_zip Nullable(String),
    work_location_country Nullable(String),
    dol_status_description Nullable(String),
    employment_type Nullable(String),
    pay_basis Nullable(String),
    hourly_salary Nullable(Decimal(18, 6)),
    annual_salary Nullable(Decimal(18, 6)),
    scheduled_pay_period_hours Nullable(Decimal(18, 6)),
    salary_min Nullable(Decimal(18, 6)),
    salary_mid Nullable(Decimal(18, 6)),
    salary_max Nullable(Decimal(18, 6)),
    rate_1 Nullable(Decimal(18, 6)),
    employee_pay_type LowCardinality(Nullable(String)),
    position_pay_type LowCardinality(Nullable(String)),
    paycode_profile_code Nullable(String),
    paycode_profile_description Nullable(String),
    pay_frequency Nullable(String),
    payroll_profile_fein Nullable(String),
    pay_class Nullable(String),
    earning_profile Nullable(String),
    schedule_group Nullable(String),
    last_check_date Nullable(DateTime64(6)),
    direct_deposit Nullable(UInt8),
    labor_allocation_key_1 Nullable(String),
    labor_allocation_key_2 Nullable(String),
    labor_allocation_key_3 Nullable(String),
    labor_allocation_key_4 Nullable(String),
    labor_allocation_key_5 Nullable(String),
    labor_allocation_key_6 Nullable(String),
    labor_allocation_key_7 Nullable(String),
    labor_allocation_key_8 Nullable(String),
    labor_allocation_key_9 Nullable(String),
    labor_allocation_key_10 Nullable(String),
    labor_allocation_key_11 Nullable(String),
    labor_allocation_key_12 Nullable(String),
    labor_allocation_key_13 Nullable(String),
    labor_allocation_key_14 Nullable(String),
    labor_allocation_key_15 Nullable(String),
    labor_allocation_key_16 Nullable(String),
    labor_allocation_key_17 Nullable(String),
    labor_allocation_key_18 Nullable(String),
    labor_allocation_key_19 Nullable(String),
    labor_allocation_key_20 Nullable(String),
    -- physical (stripped from catalog; required for RLS)
    client_code String,
    proc_center String
)
ENGINE = MergeTree
ORDER BY employee_code;

-- -----------------------------------------------------------------------------
-- 2. payroll  (MergeTree ORDER BY the catalog grain tuple, employee_code first)
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS dbpcm_warehouse.payroll;

CREATE TABLE dbpcm_warehouse.payroll
(
    employee_code String,                       -- catalog FixedString(4); widened (see header)
    register_type String,
    amount Nullable(Decimal(19, 2)),
    type_hours Nullable(Decimal(19, 2)),
    type_code FixedString(3),
    type_code_description FixedString(64),
    profile_code FixedString(5),
    distributed_job_cost_code FixedString(119),
    department_code FixedString(12),
    type_rate Decimal(12, 2),
    pay_date Nullable(Date32),
    pay_period_start_date Nullable(Date32),
    pay_period_end_date Nullable(Date32),
    transaction_number FixedString(9),
    check_number UInt32,
    labor_allocation_key_1 Nullable(String),
    labor_allocation_key_2 Nullable(String),
    labor_allocation_key_3 Nullable(String),
    labor_allocation_key_4 Nullable(String),
    labor_allocation_key_5 Nullable(String),
    labor_allocation_key_6 Nullable(String),
    labor_allocation_key_7 Nullable(String),
    labor_allocation_key_8 Nullable(String),
    labor_allocation_key_9 Nullable(String),
    labor_allocation_key_10 Nullable(String),
    labor_allocation_key_11 Nullable(String),
    labor_allocation_key_12 Nullable(String),
    labor_allocation_key_13 Nullable(String),
    labor_allocation_key_14 Nullable(String),
    labor_allocation_key_15 Nullable(String),
    labor_allocation_key_16 Nullable(String),
    labor_allocation_key_17 Nullable(String),
    labor_allocation_key_18 Nullable(String),
    labor_allocation_key_19 Nullable(String),
    labor_allocation_key_20 Nullable(String),
    -- physical (stripped from catalog; required for RLS)
    client_code String,
    proc_center String
)
ENGINE = MergeTree
ORDER BY (employee_code, register_type, profile_code, department_code,
          transaction_number, type_code, type_code_description, type_rate,
          distributed_job_cost_code, check_number);

-- -----------------------------------------------------------------------------
-- 3. department  (MergeTree ORDER BY department_code)
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS dbpcm_warehouse.department;

CREATE TABLE dbpcm_warehouse.department
(
    department_code String,
    department_name Nullable(String),
    -- physical (stripped from catalog; required for RLS)
    client_code String,
    proc_center String
)
ENGINE = MergeTree
ORDER BY department_code;

-- -----------------------------------------------------------------------------
-- 4. labor_allocation  (ReplacingMergeTree(version) ORDER BY (code, join_key))
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS dbpcm_warehouse.labor_allocation;

CREATE TABLE dbpcm_warehouse.labor_allocation
(
    code String,
    description Nullable(String),
    join_key String,
    -- physical (stripped from catalog; required for RLS + versioning)
    client_code String,
    proc_center String,
    version UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (code, join_key);

-- =============================================================================
-- 5. Row-level-security objects
-- =============================================================================
CREATE DATABASE IF NOT EXISTS dbpcm_warehouse_security;

CREATE TABLE IF NOT EXISTS dbpcm_warehouse_security.user_employee_access
(
    jti String,
    employee_code String,
    pull_id UInt64,
    updated_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(pull_id)
ORDER BY (jti, employee_code)
TTL updated_at + INTERVAL 1 DAY;

CREATE ROLE IF NOT EXISTS pcm_mcp_dev;

-- Row policies (drop-then-create for idempotency). All read the per-request
-- paycom_* custom settings and apply TO the pcm_mcp_dev role.
--
-- employee + payroll: tenant scope (client_code + proc_center) AND the caller's
--   allowed employee set (latest pull only, via pull_id = max(pull_id)).
-- department + labor_allocation: tenant scope only.

DROP ROW POLICY IF EXISTS employee_rls ON dbpcm_warehouse.employee;
CREATE ROW POLICY employee_rls ON dbpcm_warehouse.employee
USING
        client_code = getSetting('paycom_client_code')
    AND proc_center = getSetting('paycom_proc_center')
    AND employee_code IN (
            SELECT employee_code FROM dbpcm_warehouse_security.user_employee_access
            WHERE jti = getSetting('paycom_authenticated_user')
              AND pull_id = (SELECT max(pull_id) FROM dbpcm_warehouse_security.user_employee_access
                             WHERE jti = getSetting('paycom_authenticated_user'))
        )
TO pcm_mcp_dev;

DROP ROW POLICY IF EXISTS payroll_rls ON dbpcm_warehouse.payroll;
CREATE ROW POLICY payroll_rls ON dbpcm_warehouse.payroll
USING
        client_code = getSetting('paycom_client_code')
    AND proc_center = getSetting('paycom_proc_center')
    AND employee_code IN (
            SELECT employee_code FROM dbpcm_warehouse_security.user_employee_access
            WHERE jti = getSetting('paycom_authenticated_user')
              AND pull_id = (SELECT max(pull_id) FROM dbpcm_warehouse_security.user_employee_access
                             WHERE jti = getSetting('paycom_authenticated_user'))
        )
TO pcm_mcp_dev;

DROP ROW POLICY IF EXISTS department_rls ON dbpcm_warehouse.department;
CREATE ROW POLICY department_rls ON dbpcm_warehouse.department
USING
        client_code = getSetting('paycom_client_code')
    AND proc_center = getSetting('paycom_proc_center')
TO pcm_mcp_dev;

DROP ROW POLICY IF EXISTS labor_allocation_rls ON dbpcm_warehouse.labor_allocation;
CREATE ROW POLICY labor_allocation_rls ON dbpcm_warehouse.labor_allocation
USING
        client_code = getSetting('paycom_client_code')
    AND proc_center = getSetting('paycom_proc_center')
TO pcm_mcp_dev;

-- Grant the RLS role to the ClickHouse user the MCP connects as.
--
-- ROW POLICIES ONLY APPLY TO USERS/ROLES LISTED IN THEIR `TO` CLAUSE. So the MCP
-- user must be a member of pcm_mcp_dev for these policies to take effect on its
-- queries; a user NOT in `TO` sees ALL rows (policies are permissive).
--
-- In the l2 stack the MCP connects as `default` (docker-compose.integration.yml
-- CLICKHOUSE_USER=default). `default` lives in the read-only users_xml access
-- storage, so it CANNOT be granted a role:
--   * `GRANT pcm_mcp_dev TO default` over SQL -> ACCESS_STORAGE_READONLY, and
--   * a users.d `<grants>` overlay on `default` -> startup crash (ClickHouse
--     forbids `<grants>` alongside any other user setting, and `default` already
--     has password/profile/access_management).
-- Therefore `default` currently BYPASSES these policies. This needs the operator's
-- hand — pick one:
--   (A) [recommended] Point the l2 MCP at a SQL-managed user that holds the role
--       (docker-compose.integration.yml: CLICKHOUSE_USER=<sql_user> /
--       CLICKHOUSE_PASSWORD=...), created + granted like the demo user below.
--   (B) Add `default` directly to each policy's TO clause (TO pcm_mcp_dev, default)
--       — simplest for the l2 dev box, but ties the policy to the XML account.
--
-- A SQL-managed user CAN be granted the role directly (this is exactly how the
-- migration was verified — the demo account is created out-of-band, not here):
--     CREATE USER mcp_rls_demo IDENTIFIED WITH plaintext_password BY 'demo';
--     GRANT SELECT ON dbpcm_warehouse.* TO mcp_rls_demo;
--     GRANT SELECT ON dbpcm_warehouse_security.* TO mcp_rls_demo;  -- for the RLS subquery
--     GRANT pcm_mcp_dev TO mcp_rls_demo;
--     SET DEFAULT ROLE ALL TO mcp_rls_demo;

-- =============================================================================
-- 6. Seed data — the shared test-principal contract
--    CLIENT_A/PC01: EMP001, EMP002, EMP005   |   CLIENT_B/PC02: EMP003, EMP004
--    Departments D01/D02/D03 per client.
-- =============================================================================

-- employee ------------------------------------------------------------------
INSERT INTO dbpcm_warehouse.employee
    (employee_code, employee_name, first_name, last_name, employee_status,
     hire_date, department_code, department_name, pay_basis, annual_salary,
     position_title_position_info, labor_allocation_key_1, labor_allocation_key_2,
     labor_allocation_key_3, client_code, proc_center)
VALUES
    ('EMP001', 'Anderson, Alice A', 'Alice', 'Anderson', 'A', '2020-01-15 00:00:00', 'D01', 'Engineering', 'Salary', 120000.000000, 'Senior Engineer',  'LA-A-100', 'LA-A-200', 'LA-A-300', 'CLIENT_A', 'PC01'),
    ('EMP002', 'Brown, Bob B',      'Bob',   'Brown',    'A', '2021-03-01 00:00:00', 'D02', 'Sales',       'Salary', 90000.000000,  'Account Executive','LA-A-101', 'LA-A-201', 'LA-A-301', 'CLIENT_A', 'PC01'),
    ('EMP005', 'Evans, Eve E',      'Eve',   'Evans',    'A', '2019-06-10 00:00:00', 'D03', 'Operations',  'Hourly', 75000.000000,  'Ops Specialist',   'LA-A-102', 'LA-A-202', 'LA-A-302', 'CLIENT_A', 'PC01'),
    ('EMP003', 'Clark, Carol C',    'Carol', 'Clark',    'A', '2022-02-20 00:00:00', 'D01', 'Engineering', 'Salary', 110000.000000, 'Software Engineer','LA-B-100', 'LA-B-200', 'LA-B-300', 'CLIENT_B', 'PC02'),
    ('EMP004', 'Davis, Dan D',      'Dan',   'Davis',    'T', '2018-09-05 00:00:00', 'D02', 'Sales',       'Salary', 85000.000000,  'Sales Manager',    'LA-B-101', 'LA-B-201', 'LA-B-301', 'CLIENT_B', 'PC02');

-- department ----------------------------------------------------------------
INSERT INTO dbpcm_warehouse.department (department_code, department_name, client_code, proc_center) VALUES
    ('D01', 'Engineering', 'CLIENT_A', 'PC01'),
    ('D02', 'Sales',       'CLIENT_A', 'PC01'),
    ('D03', 'Operations',  'CLIENT_A', 'PC01'),
    ('D01', 'Engineering', 'CLIENT_B', 'PC02'),
    ('D02', 'Sales',       'CLIENT_B', 'PC02'),
    ('D03', 'Operations',  'CLIENT_B', 'PC02');

-- labor_allocation ----------------------------------------------------------
-- join_key values match the employees' labor_allocation_key_* so the dimension
-- join is real. version = 1.
INSERT INTO dbpcm_warehouse.labor_allocation (code, description, join_key, client_code, proc_center, version) VALUES
    ('CC100', 'Engineering Cost Center', 'LA-A-100', 'CLIENT_A', 'PC01', 1),
    ('CC200', 'R&D Project',             'LA-A-200', 'CLIENT_A', 'PC01', 1),
    ('CC300', 'HQ Location',             'LA-A-300', 'CLIENT_A', 'PC01', 1),
    ('CC101', 'Sales Cost Center',       'LA-A-101', 'CLIENT_A', 'PC01', 1),
    ('CC201', 'Sales Project',           'LA-A-201', 'CLIENT_A', 'PC01', 1),
    ('CC301', 'Branch Location',         'LA-A-301', 'CLIENT_A', 'PC01', 1),
    ('CC102', 'Ops Cost Center',         'LA-A-102', 'CLIENT_A', 'PC01', 1),
    ('CC202', 'Ops Project',             'LA-A-202', 'CLIENT_A', 'PC01', 1),
    ('CC302', 'Warehouse Location',      'LA-A-302', 'CLIENT_A', 'PC01', 1),
    ('CC100', 'Engineering Cost Center', 'LA-B-100', 'CLIENT_B', 'PC02', 1),
    ('CC200', 'R&D Project',             'LA-B-200', 'CLIENT_B', 'PC02', 1),
    ('CC300', 'HQ Location',             'LA-B-300', 'CLIENT_B', 'PC02', 1),
    ('CC101', 'Sales Cost Center',       'LA-B-101', 'CLIENT_B', 'PC02', 1),
    ('CC201', 'Sales Project',           'LA-B-201', 'CLIENT_B', 'PC02', 1),
    ('CC301', 'Branch Location',         'LA-B-301', 'CLIENT_B', 'PC02', 1);

-- payroll -------------------------------------------------------------------
-- Several rows per employee across register types (EARN / EETAX / DDUCT), with
-- department_code, amount, labor_allocation_key_* (matching the employee), and
-- tenant scope. distributed_job_cost_code = the concatenated labor keys.
INSERT INTO dbpcm_warehouse.payroll
    (employee_code, register_type, amount, type_hours, type_code, type_code_description,
     profile_code, distributed_job_cost_code, department_code, type_rate,
     pay_date, pay_period_start_date, pay_period_end_date, transaction_number, check_number,
     labor_allocation_key_1, labor_allocation_key_2, labor_allocation_key_3,
     client_code, proc_center)
VALUES
    ('EMP001', 'EARN',  4615.38, 80.00, 'REG', 'Regular Earnings',   'PRF01', 'LA-A-100|LA-A-200|LA-A-300', 'D01', 57.69, '2024-01-31', '2024-01-01', '2024-01-15', 'TXN000101', 1001, 'LA-A-100', 'LA-A-200', 'LA-A-300', 'CLIENT_A', 'PC01'),
    ('EMP001', 'EETAX',  923.08,  NULL, 'FIT', 'Federal Income Tax', 'PRF01', 'LA-A-100|LA-A-200|LA-A-300', 'D01',  0.00, '2024-01-31', '2024-01-01', '2024-01-15', 'TXN000102', 1001, 'LA-A-100', 'LA-A-200', 'LA-A-300', 'CLIENT_A', 'PC01'),
    ('EMP001', 'DDUCT',  230.77,  NULL, '401', '401k Deduction',     'PRF01', 'LA-A-100|LA-A-200|LA-A-300', 'D01',  0.00, '2024-01-31', '2024-01-01', '2024-01-15', 'TXN000103', 1001, 'LA-A-100', 'LA-A-200', 'LA-A-300', 'CLIENT_A', 'PC01'),
    ('EMP002', 'EARN',  3461.54, 80.00, 'REG', 'Regular Earnings',   'PRF01', 'LA-A-101|LA-A-201|LA-A-301', 'D02', 43.27, '2024-01-31', '2024-01-01', '2024-01-15', 'TXN000201', 1002, 'LA-A-101', 'LA-A-201', 'LA-A-301', 'CLIENT_A', 'PC01'),
    ('EMP002', 'EETAX',  692.31,  NULL, 'FIT', 'Federal Income Tax', 'PRF01', 'LA-A-101|LA-A-201|LA-A-301', 'D02',  0.00, '2024-01-31', '2024-01-01', '2024-01-15', 'TXN000202', 1002, 'LA-A-101', 'LA-A-201', 'LA-A-301', 'CLIENT_A', 'PC01'),
    ('EMP002', 'DDUCT',  173.08,  NULL, '401', '401k Deduction',     'PRF01', 'LA-A-101|LA-A-201|LA-A-301', 'D02',  0.00, '2024-01-31', '2024-01-01', '2024-01-15', 'TXN000203', 1002, 'LA-A-101', 'LA-A-201', 'LA-A-301', 'CLIENT_A', 'PC01'),
    ('EMP005', 'EARN',  2884.62, 80.00, 'REG', 'Regular Earnings',   'PRF01', 'LA-A-102|LA-A-202|LA-A-302', 'D03', 36.06, '2024-01-31', '2024-01-01', '2024-01-15', 'TXN000501', 1005, 'LA-A-102', 'LA-A-202', 'LA-A-302', 'CLIENT_A', 'PC01'),
    ('EMP005', 'EETAX',  576.92,  NULL, 'FIT', 'Federal Income Tax', 'PRF01', 'LA-A-102|LA-A-202|LA-A-302', 'D03',  0.00, '2024-01-31', '2024-01-01', '2024-01-15', 'TXN000502', 1005, 'LA-A-102', 'LA-A-202', 'LA-A-302', 'CLIENT_A', 'PC01'),
    ('EMP005', 'DDUCT',  144.23,  NULL, '401', '401k Deduction',     'PRF01', 'LA-A-102|LA-A-202|LA-A-302', 'D03',  0.00, '2024-01-31', '2024-01-01', '2024-01-15', 'TXN000503', 1005, 'LA-A-102', 'LA-A-202', 'LA-A-302', 'CLIENT_A', 'PC01'),
    ('EMP003', 'EARN',  4230.77, 80.00, 'REG', 'Regular Earnings',   'PRF01', 'LA-B-100|LA-B-200|LA-B-300', 'D01', 52.88, '2024-01-31', '2024-01-01', '2024-01-15', 'TXN000301', 1003, 'LA-B-100', 'LA-B-200', 'LA-B-300', 'CLIENT_B', 'PC02'),
    ('EMP003', 'EETAX',  846.15,  NULL, 'FIT', 'Federal Income Tax', 'PRF01', 'LA-B-100|LA-B-200|LA-B-300', 'D01',  0.00, '2024-01-31', '2024-01-01', '2024-01-15', 'TXN000302', 1003, 'LA-B-100', 'LA-B-200', 'LA-B-300', 'CLIENT_B', 'PC02'),
    ('EMP003', 'DDUCT',  211.54,  NULL, '401', '401k Deduction',     'PRF01', 'LA-B-100|LA-B-200|LA-B-300', 'D01',  0.00, '2024-01-31', '2024-01-01', '2024-01-15', 'TXN000303', 1003, 'LA-B-100', 'LA-B-200', 'LA-B-300', 'CLIENT_B', 'PC02'),
    ('EMP004', 'EARN',  3269.23, 80.00, 'REG', 'Regular Earnings',   'PRF01', 'LA-B-101|LA-B-201|LA-B-301', 'D02', 40.87, '2024-01-31', '2024-01-01', '2024-01-15', 'TXN000401', 1004, 'LA-B-101', 'LA-B-201', 'LA-B-301', 'CLIENT_B', 'PC02'),
    ('EMP004', 'EETAX',  653.85,  NULL, 'FIT', 'Federal Income Tax', 'PRF01', 'LA-B-101|LA-B-201|LA-B-301', 'D02',  0.00, '2024-01-31', '2024-01-01', '2024-01-15', 'TXN000402', 1004, 'LA-B-101', 'LA-B-201', 'LA-B-301', 'CLIENT_B', 'PC02'),
    ('EMP004', 'DDUCT',  163.46,  NULL, '401', '401k Deduction',     'PRF01', 'LA-B-101|LA-B-201|LA-B-301', 'D02',  0.00, '2024-01-31', '2024-01-01', '2024-01-15', 'TXN000403', 1004, 'LA-B-101', 'LA-B-201', 'LA-B-301', 'CLIENT_B', 'PC02');

-- Second pay period (period-close 2024-01-31) for EMP001 so a single employee has
-- TWO distinct pay_period_end_date values — the data the two-period paycheck
-- comparison blueprint (bp-compare-employee-check-detail-two-periods) needs to run
-- end-to-end. Same three line-items as the 2024-01-15 check, with a raise + a few
-- overtime hours so the per-line-item deltas are non-trivial. CLIENT_A / PC01 so a
-- CLIENT_A/PC01 RLS token sees it.
INSERT INTO dbpcm_warehouse.payroll
    (employee_code, register_type, amount, type_hours, type_code, type_code_description,
     profile_code, distributed_job_cost_code, department_code, type_rate,
     pay_date, pay_period_start_date, pay_period_end_date, transaction_number, check_number,
     labor_allocation_key_1, labor_allocation_key_2, labor_allocation_key_3,
     client_code, proc_center)
VALUES
    ('EMP001', 'EARN',  5000.00, 88.00, 'REG', 'Regular Earnings',   'PRF01', 'LA-A-100|LA-A-200|LA-A-300', 'D01', 56.82, '2024-02-15', '2024-01-16', '2024-01-31', 'TXN000111', 1011, 'LA-A-100', 'LA-A-200', 'LA-A-300', 'CLIENT_A', 'PC01'),
    ('EMP001', 'EETAX', 1000.00,  NULL, 'FIT', 'Federal Income Tax', 'PRF01', 'LA-A-100|LA-A-200|LA-A-300', 'D01',  0.00, '2024-02-15', '2024-01-16', '2024-01-31', 'TXN000112', 1011, 'LA-A-100', 'LA-A-200', 'LA-A-300', 'CLIENT_A', 'PC01'),
    ('EMP001', 'DDUCT',  250.00,  NULL, '401', '401k Deduction',     'PRF01', 'LA-A-100|LA-A-200|LA-A-300', 'D01',  0.00, '2024-02-15', '2024-01-16', '2024-01-31', 'TXN000113', 1011, 'LA-A-100', 'LA-A-200', 'LA-A-300', 'CLIENT_A', 'PC01');

-- user_employee_access ------------------------------------------------------
-- Test principal TESTJTI001 may see ONLY EMP001 + EMP002.
INSERT INTO dbpcm_warehouse_security.user_employee_access (jti, employee_code, pull_id, updated_at) VALUES
    ('TESTJTI001', 'EMP001', 1, now()),
    ('TESTJTI001', 'EMP002', 1, now());
