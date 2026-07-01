-- Layer-2 integration fixture: minimal HR warehouse matching databaseSchemaDocs/*.yaml.
-- Column NAMES are case-sensitive-exact to the Semantic Catalog (D70) so the D83
-- getTableSchema overlay + column-scope filter tests are meaningful. A handful of rows
-- is enough to prove scope enforcement (D57/D83) against real ClickHouse — this is a
-- test fixture, not production data.

CREATE DATABASE IF NOT EXISTS dbpcm_warehouse;

CREATE TABLE IF NOT EXISTS dbpcm_warehouse.employee
(
    ClientCode      Nullable(String),
    EmployeeCode    String,
    Department      Nullable(String),
    EmployeeName    Nullable(String),
    EmployeeStatus  Nullable(String),
    AnnualSalary    Nullable(Decimal(18, 6))
)
ENGINE = MergeTree
ORDER BY EmployeeCode;

INSERT INTO dbpcm_warehouse.employee
    (ClientCode, EmployeeCode, Department, EmployeeName, EmployeeStatus, AnnualSalary) VALUES
    ('CLIENT_A', 'EMP001', 'Sales',       'Alice Smith', 'A', 75000.00),
    ('CLIENT_A', 'EMP002', 'Engineering', 'Bob Jones',   'A', 95000.00),
    ('CLIENT_B', 'EMP003', 'Sales',       'Carol White', 'A', 72000.00),
    ('CLIENT_B', 'EMP004', 'HR',          'David Lee',   'I', 68000.00),
    ('CLIENT_A', 'EMP005', 'Finance',     'Eve Brown',   'A', 85000.00);

CREATE TABLE IF NOT EXISTS dbpcm_warehouse.payroll
(
    ClientCode        Nullable(String),
    EmployeeCode      String,
    RegisterType      Nullable(String),
    Amount            Nullable(Decimal(18, 6)),
    -- catalog declares DateTime64(6); simplified to Date for this fixture (no
    -- integration test exercises payroll temporal columns). PayDate /
    -- PayPeriodStartDate from the catalog are intentionally not seeded here.
    PayPeriodEndDate  Nullable(Date)
)
ENGINE = MergeTree
ORDER BY EmployeeCode;

INSERT INTO dbpcm_warehouse.payroll
    (ClientCode, EmployeeCode, RegisterType, Amount, PayPeriodEndDate) VALUES
    ('CLIENT_A', 'EMP001', 'EARN',       3750.00, '2026-05-31'),
    ('CLIENT_A', 'EMP001', 'DEDUCTION',  -150.00, '2026-05-31'),
    ('CLIENT_A', 'EMP002', 'EARN',       4750.00, '2026-05-31'),
    ('CLIENT_B', 'EMP003', 'EARN',       3600.00, '2026-05-31'),
    ('CLIENT_A', 'EMP005', 'EARN',       4250.00, '2026-05-31');
