-- Layer-2 integration fixture: full-fidelity synthetic HR warehouse seeding ALL 9 tables
-- described in databaseSchemaDocs/*.yaml. Column NAMES and TYPES are case-sensitive-exact to the
-- Semantic Catalog (D70) so the D83 getTableSchema overlay, column-scope, resolveValues, grain and
-- referential-integrity checks are meaningful against real ClickHouse.
--
-- This is a SYNTHETIC TEST FIXTURE, not production data. All PII (applicant phones/emails/DOB/
-- addresses) is OBVIOUSLY FAKE (555-xxxx phones, @example.test emails, "Fake St" addresses).
--
-- Design invariants (verified by the throwaway-container checks in the task):
--   * Referential integrity: every child EmployeeCode (payroll, accrual_events, PAF, ATS-application
--     for hired candidates, performance_discussions) exists in employee (EMP001-EMP005). Every
--     candidate_education/candidate_employment_history.ApplicationId exists in
--     applicant_tracking_application.
--   * Grain-verifiable tables (employee, applicant_tracking_application, applicant_tracking_requisition,
--     candidate_education, candidate_employment_history) have a UNIQUE grain key.
--   * grain=[] tables (payroll, accrual_events, personnel_action_form_changes, performance_discussions)
--     carry DELIBERATE duplicates on their logical grouping key (EmployeeCode / PafTransactionId /
--     DiscussionId), matching grain_verifiable: false in the catalog.
--   * Two clients: CLIENT_A (EMP001/EMP002/EMP005) and CLIENT_B (EMP003/EMP004). Each has a small,
--     consistent set of client-defined codes reused across rows, with sibling description columns
--     kept code-consistent (EarnCode<->EarnDescription, TypeCode<->TypeCodeDescription,
--     FieldId<->FieldLabel, DistributedDepartmentCode<->distributedDepartmentDescription,
--     RequisitionDepartmentCode<->RequisitionDepartmentDescription).
--   * payroll Amount meaning is register-scoped and internally consistent per employee/period:
--     NETPAYDIST = EARN - EETAX - DDUCT. That identity covers STANDARD statutory/benefit deductions
--     only; expense-reimbursement DDUCT codes (EP*, AIR, REI, EQ6, ...) are add-back reimbursements
--     and are NOT included in the NETPAYDIST identity.

CREATE DATABASE IF NOT EXISTS dbpcm_warehouse;

-- =====================================================================================================
-- employee — grain [EmployeeCode] (UNIQUE). Current-state record, one row per employee.
-- =====================================================================================================
DROP TABLE IF EXISTS dbpcm_warehouse.employee;
CREATE TABLE dbpcm_warehouse.employee
(
    ClientCode                        Nullable(String),
    EmployeeCode                      String,
    Department                        Nullable(String),
    Position                          Nullable(String),
    EmployeeName                      Nullable(String),
    EmployeeStatus                    Nullable(String),
    DepartmentCode                    Nullable(String),
    LivesInState                      Nullable(String),
    WorksInState                      Nullable(String),
    SUIState                          Nullable(String),
    TerminationDate                   Nullable(DateTime64(6)),
    OldTerminationDate                Nullable(DateTime64(6)),
    FirstName                         Nullable(String),
    MiddleName                        Nullable(String),
    LastName                          Nullable(String),
    Nickname                          Nullable(String),
    HireDate                          Nullable(DateTime64(6)),
    MostRecentHireDate                Nullable(DateTime64(6)),
    LeaveAbsenceStart                 Nullable(DateTime64(6)),
    LeaveAbsenceEnd                   Nullable(DateTime64(6)),
    PrimarySupervisorEmployeeName     Nullable(String),
    SecondarySupervisorEmployeeName   Nullable(String),
    TertiarySupervisorEmployeeName    Nullable(String),
    QuaternarySupervisorEmployeeName  Nullable(String),
    CountryCode                       Nullable(String),
    TermReason                        Nullable(String),
    FullTimeToPartTimeDate            Nullable(DateTime64(6)),
    LastCheckDate                     Nullable(DateTime64(6)),
    PositionFamilyName                Nullable(String),
    AccrualProfileDesc                Nullable(String),
    BusinessTitlePositionSeat         Nullable(String),
    PositionLevel                     Nullable(Int32),
    PrimaryAddressLine1               Nullable(String),
    PrimaryAddressLine2               Nullable(String),
    PrimaryCityMunicipality           Nullable(String),
    PrimaryState                      Nullable(String),
    PrimaryZipCode                    Nullable(String),
    RehireDate                        Nullable(DateTime64(6)),
    LastPositionChangeDate            Nullable(DateTime64(6)),
    LastWorkedDate                    Nullable(DateTime64(6)),
    PositionTitlePositionInfo         Nullable(String),
    PayTypeDescription                Nullable(String),
    WorkLocationDescription           Nullable(String),
    WorkLocationAddress               Nullable(String),
    WorkLocationCity                  Nullable(String),
    WorkLocationState                 Nullable(String),
    WorkLocationZip                   Nullable(String),
    WorkLocationCountry               Nullable(String),
    sales_sl_gradution                Nullable(String),
    sales_sl_start_date               Nullable(String),
    AnnualSalary                      Nullable(Decimal(18, 6)),
    EmploymentType                    Nullable(String),
    Rate1                             Nullable(Decimal(18, 6))
)
ENGINE = MergeTree
ORDER BY EmployeeCode;

INSERT INTO dbpcm_warehouse.employee
(ClientCode, EmployeeCode, Department, Position, EmployeeName, EmployeeStatus, DepartmentCode, LivesInState, WorksInState, SUIState, TerminationDate, OldTerminationDate, FirstName, MiddleName, LastName, Nickname, HireDate, MostRecentHireDate, LeaveAbsenceStart, LeaveAbsenceEnd, PrimarySupervisorEmployeeName, SecondarySupervisorEmployeeName, TertiarySupervisorEmployeeName, QuaternarySupervisorEmployeeName, CountryCode, TermReason, FullTimeToPartTimeDate, LastCheckDate, PositionFamilyName, AccrualProfileDesc, BusinessTitlePositionSeat, PositionLevel, PrimaryAddressLine1, PrimaryAddressLine2, PrimaryCityMunicipality, PrimaryState, PrimaryZipCode, RehireDate, LastPositionChangeDate, LastWorkedDate, PositionTitlePositionInfo, PayTypeDescription, WorkLocationDescription, WorkLocationAddress, WorkLocationCity, WorkLocationState, WorkLocationZip, WorkLocationCountry, sales_sl_gradution, sales_sl_start_date, AnnualSalary, EmploymentType, Rate1) VALUES
('CLIENT_A','EMP001','Sales','Sales Representative','Alice Smith','A','SLS','TX','TX','TX',NULL,NULL,'Alice','Marie','Smith','Ali','2021-03-15 00:00:00','2021-03-15 00:00:00',NULL,NULL,'Eve Brown',NULL,NULL,NULL,'US',NULL,NULL,'2026-06-05 00:00:00','SALES_FAM','Standard PTO Plan','Sales Rep I',2,'100 Fake St',NULL,'Austin','TX','78701',NULL,'2023-01-01 00:00:00',NULL,'Sales Representative','Salary','HQ - Austin','1 Corporate Way','Austin','TX','78701','US','2021-04-01','2021-03-20',75000.00,'W2',36.06),
('CLIENT_A','EMP002','Engineering','Software Engineer','Bob Jones','A','ENG','TX','TX','TX',NULL,NULL,'Bob',NULL,'Jones','Bobby','2019-07-01 00:00:00','2019-07-01 00:00:00',NULL,NULL,'Eve Brown',NULL,NULL,NULL,'US',NULL,NULL,'2026-06-05 00:00:00','TECH_FAM','Standard PTO Plan','Software Engineer II',3,'200 Fake Ave',NULL,'Austin','TX','78702',NULL,'2022-06-01 00:00:00',NULL,'Software Engineer','Salary','HQ - Austin','1 Corporate Way','Austin','TX','78701','US',NULL,NULL,95000.00,'W2',45.67),
('CLIENT_B','EMP003','Sales','Sales Manager','Carol White','A','D100','CA','CA','CA',NULL,NULL,'Carol',NULL,'White','Caz','2020-02-10 00:00:00','2020-02-10 00:00:00',NULL,NULL,'David Lee',NULL,NULL,NULL,'US',NULL,NULL,'2026-06-05 00:00:00','SALES','Manager Accrual Plan','Sales Manager',4,'300 Fake Blvd',NULL,'San Diego','CA','92101',NULL,'2024-03-01 00:00:00',NULL,'Sales Manager','Salary','Branch - San Diego','500 Branch Rd','San Diego','CA','92101','US',NULL,NULL,72000.00,'W2',34.62),
('CLIENT_B','EMP004','HR','HR Generalist','David Lee','T','D200','CA','CA','CA','2025-11-30 00:00:00',NULL,'David',NULL,'Lee',NULL,'2018-05-20 00:00:00','2018-05-20 00:00:00',NULL,NULL,'Grace Kim',NULL,NULL,NULL,'US','VOL',NULL,'2025-12-05 00:00:00','ADMIN','Manager Accrual Plan','HR Generalist',3,'400 Fake Ln',NULL,'San Diego','CA','92102',NULL,'2021-09-01 00:00:00','2025-11-30 00:00:00','HR Generalist','Salary','Branch - San Diego','500 Branch Rd','San Diego','CA','92101','US',NULL,NULL,68000.00,'W2',32.69),
('CLIENT_A','EMP005','Finance','Financial Analyst','Eve Brown','A','FIN','TX','TX','TX',NULL,NULL,'Eve',NULL,'Brown',NULL,'2017-11-06 00:00:00','2017-11-06 00:00:00',NULL,NULL,'Frank Miller',NULL,NULL,NULL,'US',NULL,NULL,'2026-06-05 00:00:00','FIN_FAM','Standard PTO Plan','Financial Analyst III',4,'500 Fake Ct',NULL,'Austin','TX','78703',NULL,'2023-05-01 00:00:00',NULL,'Financial Analyst','Salary','HQ - Austin','1 Corporate Way','Austin','TX','78701','US',NULL,NULL,85000.00,'W2',40.87);

-- =====================================================================================================
-- payroll — grain [] (grain_verifiable: false). Register LINE ITEMS; duplicate EmployeeCode/PayDate
-- expected. Amount meaning is register-scoped; NETPAYDIST = EARN - EETAX - DDUCT per employee/period.
-- =====================================================================================================
DROP TABLE IF EXISTS dbpcm_warehouse.payroll;
CREATE TABLE dbpcm_warehouse.payroll
(
    ClientCode                       Nullable(String),
    EmployeeCode                     String,
    RegisterType                     String,
    Amount                           Nullable(Decimal(18, 6)),
    TypeHours                        Nullable(Decimal(18, 6)),
    TypeCode                         Nullable(String),
    TypeCodeDescription              Nullable(String),
    ProfileCode                      Nullable(String),
    DistributedDepartmentCode        Nullable(String),
    distributedDepartmentDescription Nullable(String),
    TypeRate                         Nullable(Decimal(18, 6)),
    PayDate                          DateTime64(6),
    PayPeriodStartDate               DateTime64(6),
    PayPeriodEndDate                 DateTime64(6),
    TransactionNumber                Nullable(String)
)
ENGINE = MergeTree
ORDER BY EmployeeCode;

INSERT INTO dbpcm_warehouse.payroll
(ClientCode, EmployeeCode, RegisterType, Amount, TypeHours, TypeCode, TypeCodeDescription, ProfileCode, DistributedDepartmentCode, distributedDepartmentDescription, TypeRate, PayDate, PayPeriodStartDate, PayPeriodEndDate, TransactionNumber) VALUES
('CLIENT_A','EMP001','EARN',3125.00,86.67,'REG','Regular Earnings','STD','SLS','Sales',36.06,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-001'),
('CLIENT_A','EMP001','EETAX',625.00,NULL,'FIT','Federal Income Tax','STD','SLS','Sales',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-001'),
('CLIENT_A','EMP001','DDUCT',187.50,NULL,'401K','401(k) Contribution','STD','SLS','Sales',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-001'),
('CLIENT_A','EMP001','EEBEN',150.00,NULL,'MED','Medical Premium','STD','SLS','Sales',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-001'),
('CLIENT_A','EMP001','ERTAX',239.06,NULL,'SSER','Social Security (Employer)','STD','SLS','Sales',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-001'),
('CLIENT_A','EMP001','NETPAYDIST',2312.50,NULL,'DD','Direct Deposit','STD','SLS','Sales',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-001'),
('CLIENT_A','EMP002','EARN',3958.33,86.67,'REG','Regular Earnings','STD','ENG','Engineering',45.67,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-002'),
('CLIENT_A','EMP002','EETAX',831.25,NULL,'FIT','Federal Income Tax','STD','ENG','Engineering',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-002'),
('CLIENT_A','EMP002','DDUCT',237.50,NULL,'401K','401(k) Contribution','STD','ENG','Engineering',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-002'),
('CLIENT_A','EMP002','EEBEN',200.00,NULL,'MED','Medical Premium','STD','ENG','Engineering',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-002'),
('CLIENT_A','EMP002','ERTAX',302.81,NULL,'SSER','Social Security (Employer)','STD','ENG','Engineering',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-002'),
('CLIENT_A','EMP002','NETPAYDIST',2889.58,NULL,'DD','Direct Deposit','STD','ENG','Engineering',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-002'),
('CLIENT_A','EMP005','EARN',3541.67,86.67,'REG','Regular Earnings','STD','FIN','Finance',40.87,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-005'),
('CLIENT_A','EMP005','EETAX',744.00,NULL,'FIT','Federal Income Tax','STD','FIN','Finance',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-005'),
('CLIENT_A','EMP005','DDUCT',212.50,NULL,'401K','401(k) Contribution','STD','FIN','Finance',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-005'),
('CLIENT_A','EMP005','EEBEN',175.00,NULL,'MED','Medical Premium','STD','FIN','Finance',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-005'),
('CLIENT_A','EMP005','ERTAX',270.94,NULL,'SSER','Social Security (Employer)','STD','FIN','Finance',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-005'),
('CLIENT_A','EMP005','NETPAYDIST',2585.17,NULL,'DD','Direct Deposit','STD','FIN','Finance',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-005'),
('CLIENT_B','EMP003','EARN',3000.00,86.67,'BASE','Base Pay','STD','D100','Sales',34.62,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-B-0605-003'),
('CLIENT_B','EMP003','EETAX',600.00,NULL,'FED','Federal Withholding','STD','D100','Sales',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-B-0605-003'),
('CLIENT_B','EMP003','DDUCT',180.00,NULL,'DENT','Dental Premium','STD','D100','Sales',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-B-0605-003'),
('CLIENT_B','EMP003','EEBEN',140.00,NULL,'VIS','Vision Premium','STD','D100','Sales',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-B-0605-003'),
('CLIENT_B','EMP003','ERTAX',229.50,NULL,'FICA','FICA (Employer)','STD','D100','Sales',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-B-0605-003'),
('CLIENT_B','EMP003','NETPAYDIST',2220.00,NULL,'ACH','ACH Deposit','STD','D100','Sales',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-B-0605-003'),
('CLIENT_A','EMP001','DDUCT',45.00,NULL,'EP7','Meals (-)','STD','SLS','Sales',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-001'),
('CLIENT_A','EMP001','DDUCT',120.00,NULL,'EP6','Mileage (-)','STD','SLS','Sales',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-001'),
('CLIENT_A','EMP002','DDUCT',350.00,NULL,'AIR','Airfare (-)','STD','ENG','Engineering',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-002'),
('CLIENT_A','EMP005','DDUCT',75.00,NULL,'REI','Reimbursement (-)','STD','FIN','Finance',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-A-0605-005'),
('CLIENT_B','EMP003','DDUCT',30.00,NULL,'EQ6','Taxi/Uber/Lyft (-)','STD','D100','Sales',NULL,'2026-06-05 00:00:00','2026-05-16 00:00:00','2026-05-31 00:00:00','TXN-B-0605-003');

-- =====================================================================================================
-- accrual_events — grain [] (grain_verifiable: false). ONE ROW PER DAY of a request.
-- EarnCode<->EarnDescription kept consistent. EmployeeCode FK -> employee.
-- =====================================================================================================
DROP TABLE IF EXISTS dbpcm_warehouse.accrual_events;
CREATE TABLE dbpcm_warehouse.accrual_events
(
    ClientCode                  Nullable(String),
    EmployeeCode                String,
    RequestDate                 Nullable(DateTime64(6)),
    Hours                       Nullable(Decimal(18, 6)),
    EarnCode                    Nullable(String),
    EarnDescription             Nullable(String),
    Status                      Nullable(String),
    Reason                      Nullable(String),
    EventType                   Nullable(String),
    DateAdded                   Nullable(DateTime64(6)),
    Comments                    Nullable(String),
    GoneReason                  Nullable(String),
    AccrualEventDepartmentCode  Nullable(String)
)
ENGINE = MergeTree
ORDER BY EmployeeCode;

INSERT INTO dbpcm_warehouse.accrual_events
(ClientCode, EmployeeCode, RequestDate, Hours, EarnCode, EarnDescription, Status, Reason, EventType, DateAdded, Comments, GoneReason, AccrualEventDepartmentCode) VALUES
('CLIENT_A','EMP001','2026-04-06 00:00:00',8.00,'PTO','Paid Time Off','Approved','Vacation','Time Off Request','2026-03-20 09:00:00','Auto-approved',NULL,'SLS'),
('CLIENT_A','EMP001','2026-04-07 00:00:00',8.00,'PTO','Paid Time Off','Approved','Vacation','Time Off Request','2026-03-20 09:00:00','Auto-approved',NULL,'SLS'),
('CLIENT_A','EMP001','2026-04-08 00:00:00',8.00,'PTO','Paid Time Off','Approved','Vacation','Time Off Request','2026-03-20 09:00:00','Auto-approved',NULL,'SLS'),
('CLIENT_A','EMP001','2026-04-20 00:00:00',8.00,'PTO','Paid Time Off','Removed','Changed plans','Time Off Request','2026-04-10 11:30:00',NULL,NULL,'SLS'),
('CLIENT_A','EMP002','2026-03-11 00:00:00',8.00,'SICK','Sick Leave','Approved','Illness','Time Off Request','2026-03-11 07:45:00',NULL,NULL,'ENG'),
('CLIENT_A','EMP002','2026-03-12 00:00:00',8.00,'SICK','Sick Leave','Approved','Illness','Time Off Request','2026-03-11 07:45:00',NULL,NULL,'ENG'),
('CLIENT_A','EMP002','2026-06-15 00:00:00',8.00,'PTO','Paid Time Off','Requested','Vacation','Time Off Request','2026-05-30 14:00:00',NULL,NULL,'ENG'),
('CLIENT_A','EMP002','2026-06-16 00:00:00',8.00,'PTO','Paid Time Off','Requested','Vacation','Time Off Request','2026-05-30 14:00:00',NULL,NULL,'ENG'),
('CLIENT_A','EMP005','2026-05-04 00:00:00',8.00,'BRV','Bereavement','Approved','Family bereavement','Time Off Request','2026-05-01 08:15:00',NULL,NULL,'FIN'),
('CLIENT_A','EMP005','2026-06-30 00:00:00',16.00,'PTO','Paid Time Off','Approved','Annual payout','Payout Request','2026-06-20 10:00:00','PTO payout',NULL,'FIN'),
('CLIENT_B','EMP003','2026-05-18 00:00:00',8.00,'VAC','Vacation','Approved','Holiday','Time Off Request','2026-05-01 09:30:00',NULL,NULL,'D100'),
('CLIENT_B','EMP003','2026-05-19 00:00:00',8.00,'VAC','Vacation','Approved','Holiday','Time Off Request','2026-05-01 09:30:00',NULL,NULL,'D100'),
('CLIENT_B','EMP003','2026-05-20 00:00:00',8.00,'VAC','Vacation','Approved','Holiday','Time Off Request','2026-05-01 09:30:00',NULL,NULL,'D100'),
('CLIENT_B','EMP003','2026-07-03 00:00:00',8.00,'VAC','Vacation','Denied','Insufficient balance','Time Off Request','2026-06-25 16:20:00','Denied by manager','Balance exhausted','D100'),
('CLIENT_B','EMP003','2026-06-01 00:00:00',8.00,'JURY','Jury Duty','Calendar Only','Civic duty','Time Off Request','2026-05-15 12:00:00',NULL,NULL,'D100');

-- =====================================================================================================
-- personnel_action_form_changes — logical grain [PafTransactionId, FieldId] (UNIQUE per pair).
-- PafTransactionId repeats across FieldId rows (multiple changed fields per transaction).
-- FieldId<->FieldLabel kept consistent. EmployeeCode FK -> employee.
-- =====================================================================================================
DROP TABLE IF EXISTS dbpcm_warehouse.personnel_action_form_changes;
CREATE TABLE dbpcm_warehouse.personnel_action_form_changes
(
    ClientCode             Nullable(String),
    PafTransactionId       Int32,
    EmployeeCode           Nullable(String),
    EmployeeName           Nullable(String),
    PafEffectiveDate       Nullable(DateTime64(6)),
    PafEffectiveDateTime   Nullable(DateTime64(6)),
    PafStatus              Nullable(String),
    PafComments            Nullable(String),
    ForwardTo              Nullable(String),
    CreatedBy              Nullable(String),
    ModifiedBy             Nullable(String),
    CreatedDate            Nullable(DateTime64(6)),
    ModifiedDate           Nullable(DateTime64(6)),
    ManagerSignDate        Nullable(DateTime64(6)),
    FieldId                Nullable(String),
    FieldLabel             Nullable(String),
    OldValue               Nullable(String),
    ProposedValue          Nullable(String)
)
ENGINE = MergeTree
ORDER BY PafTransactionId;

INSERT INTO dbpcm_warehouse.personnel_action_form_changes
(ClientCode, PafTransactionId, EmployeeCode, EmployeeName, PafEffectiveDate, PafEffectiveDateTime, PafStatus, PafComments, ForwardTo, CreatedBy, ModifiedBy, CreatedDate, ModifiedDate, ManagerSignDate, FieldId, FieldLabel, OldValue, ProposedValue) VALUES
('CLIENT_A',1001,'EMP002','Bob Jones','2026-06-01 00:00:00','2026-06-01 09:00:00','Final Approval','Promotion to senior','hr.admin','manager.eng','hr.admin','2026-05-10 10:00:00','2026-05-20 15:00:00','2026-05-20 15:05:00','F_TITLE','Job Title','Software Engineer','Senior Software Engineer'),
('CLIENT_A',1001,'EMP002','Bob Jones','2026-06-01 00:00:00','2026-06-01 09:00:00','Final Approval','Promotion to senior','hr.admin','manager.eng','hr.admin','2026-05-10 10:00:00','2026-05-20 15:00:00','2026-05-20 15:05:00','F_SAL','Annual Salary','95000','105000'),
('CLIENT_A',1001,'EMP002','Bob Jones','2026-06-01 00:00:00','2026-06-01 09:00:00','Final Approval','Promotion to senior','hr.admin','manager.eng','hr.admin','2026-05-10 10:00:00','2026-05-20 15:00:00','2026-05-20 15:05:00','F_STAT','Employee Status','A','A'),
('CLIENT_A',1002,'EMP001','Alice Smith','2026-07-15 00:00:00','2026-07-15 09:00:00','Pending','Department transfer to Marketing','manager.mktg','hr.admin',NULL,'2026-06-25 13:00:00','2026-06-25 13:00:00',NULL,'F_DEPT','Department','Sales','Marketing'),
('CLIENT_A',1002,'EMP001','Alice Smith','2026-07-15 00:00:00','2026-07-15 09:00:00','Pending','Department transfer to Marketing','manager.mktg','hr.admin',NULL,'2026-06-25 13:00:00','2026-06-25 13:00:00',NULL,'F_TITLE','Job Title','Sales Representative','Marketing Specialist'),
('CLIENT_B',2001,'EMP004','David Lee','2025-11-30 00:00:00','2025-11-30 17:00:00','Final Approval','Voluntary termination','hr.cb','cb.manager','hr.cb','2025-11-15 09:00:00','2025-11-28 16:00:00','2025-11-28 16:10:00','FLD_TERM','Termination Date',NULL,'2025-11-30'),
('CLIENT_B',2001,'EMP004','David Lee','2025-11-30 00:00:00','2025-11-30 17:00:00','Final Approval','Voluntary termination','hr.cb','cb.manager','hr.cb','2025-11-15 09:00:00','2025-11-28 16:00:00','2025-11-28 16:10:00','FLD_SUP','Supervisor','Grace Kim',NULL),
('CLIENT_B',2002,'EMP003','Carol White','2026-08-01 00:00:00','2026-08-01 09:00:00','Draft','Merit pay increase','hr.cb','cb.manager',NULL,'2026-07-05 11:00:00','2026-07-05 11:00:00',NULL,'FLD_PAY','Pay Rate','34.62','36.00'),
('CLIENT_B',2002,'EMP003','Carol White','2026-08-01 00:00:00','2026-08-01 09:00:00','Draft','Merit pay increase','hr.cb','cb.manager',NULL,'2026-07-05 11:00:00','2026-07-05 11:00:00',NULL,'FLD_SUP','Supervisor','David Lee','David Lee');

-- =====================================================================================================
-- applicant_tracking_application — grain [ApplicationId] (UNIQUE, Int32). Hired rows carry EmployeeCode
-- (FK -> employee). All PII is OBVIOUSLY FAKE synthetic data.
-- =====================================================================================================
DROP TABLE IF EXISTS dbpcm_warehouse.applicant_tracking_application;
CREATE TABLE dbpcm_warehouse.applicant_tracking_application
(
    ClientCode                          Nullable(String),
    ApplicationId                       Int32,
    ReferralSource                      Nullable(String),
    ApplicationDate                     DateTime64(6),
    Disposition                         Nullable(String),
    DispositionDate                     Nullable(DateTime64(6)),
    WorkFlowGroupName                   Nullable(String),
    CandidateId                         Nullable(Int32),
    ApplicationSource                   Nullable(String),
    ApplicantFirstName                  Nullable(String),
    ApplicantLastName                   Nullable(String),
    ApplicantPreferredFirstName         Nullable(String),
    ApplicantPreferredLastName          Nullable(String),
    ApplicantPrimaryPhone               Nullable(String),
    ApplicantSecondaryPhone             Nullable(String),
    ApplicantEmailAddress               Nullable(String),
    ApplicantBirthDate                  Nullable(DateTime64(6)),
    ApplicantFullStreetAddress          Nullable(String),
    ApplicantStreetAddress1             Nullable(String),
    ApplicantStreetAddress2             Nullable(String),
    ApplicantCity                       Nullable(String),
    ApplicantState                      Nullable(String),
    ApplicantZipCode                    Nullable(String),
    ApplicantCounty                     Nullable(String),
    ApplicationStatusEnum               Nullable(String),
    ApplicationType                     Nullable(String),
    CompleteIncomplete                  Nullable(String),
    OfferLetterAcceptedRejectedDate     Nullable(DateTime64(6)),
    OfferLetterStatusEnum               Nullable(String),
    OfferLetterSentDate                 Nullable(DateTime64(6)),
    ApprovalStep                        Nullable(String),
    WorkFlowStep                        Nullable(String),
    OfferLetterCreatedOnDate            Nullable(DateTime64(6)),
    OfferLetterApprovalDate             Nullable(DateTime64(6)),
    OfferVersion                        Nullable(String),
    ApplicationJobWageStart             Nullable(Decimal(18, 6)),
    ApplicationJobWageEnd               Nullable(Decimal(18, 6)),
    HiringProcessStep                   Nullable(String),
    UserAssignedToStep                  Nullable(String),
    DaysInOfferStatus                   Nullable(Int32),
    HoursInOfferStatus                  Nullable(Int32),
    DaysInHiringStep                    Nullable(Int32),
    HoursInHiringStep                   Nullable(Int32),
    MinutesInHiringStep                 Nullable(Int32),
    HoursInNewHireQueue                 Nullable(Int32),
    DaysInNewHireQueue                  Nullable(Int32),
    TotalOnboardingHours                Nullable(Int32),
    ApplicationPositionSeatCode         Nullable(String),
    ApplicationPositionSeatDescription  Nullable(String),
    EmployeeCode                        Nullable(String)
)
ENGINE = MergeTree
ORDER BY ApplicationId;

INSERT INTO dbpcm_warehouse.applicant_tracking_application
(ClientCode, ApplicationId, ReferralSource, ApplicationDate, Disposition, DispositionDate, WorkFlowGroupName, CandidateId, ApplicationSource, ApplicantFirstName, ApplicantLastName, ApplicantPreferredFirstName, ApplicantPreferredLastName, ApplicantPrimaryPhone, ApplicantSecondaryPhone, ApplicantEmailAddress, ApplicantBirthDate, ApplicantFullStreetAddress, ApplicantStreetAddress1, ApplicantStreetAddress2, ApplicantCity, ApplicantState, ApplicantZipCode, ApplicantCounty, ApplicationStatusEnum, ApplicationType, CompleteIncomplete, OfferLetterAcceptedRejectedDate, OfferLetterStatusEnum, OfferLetterSentDate, ApprovalStep, WorkFlowStep, OfferLetterCreatedOnDate, OfferLetterApprovalDate, OfferVersion, ApplicationJobWageStart, ApplicationJobWageEnd, HiringProcessStep, UserAssignedToStep, DaysInOfferStatus, HoursInOfferStatus, DaysInHiringStep, HoursInHiringStep, MinutesInHiringStep, HoursInNewHireQueue, DaysInNewHireQueue, TotalOnboardingHours, ApplicationPositionSeatCode, ApplicationPositionSeatDescription, EmployeeCode) VALUES
('CLIENT_A',5001,'Employee Referral','2021-02-01 09:00:00','Hired','2021-03-01 10:00:00','Sales Hiring',9001,'Company Website','Alice','Smith','Ali','Smith','555-0101','555-0102','applicant5001@example.test','1990-01-15 00:00:00','100 Fake St, Austin TX','100 Fake St',NULL,'Austin','TX','78701','Travis','Hired','External','Complete','2021-02-20 12:00:00','Offer Signed','2021-02-15 09:00:00','Completed','Onboarded','2021-02-10 09:00:00','2021-02-14 09:00:00','v1',65000.00,80000.00,'Completed','recruiter.a',3,72,2,48,2880,24,1,40,'SEAT-SLS-01','Sales Representative Seat','EMP001'),
('CLIENT_A',5002,'Indeed','2026-05-01 09:00:00','Offer Extended','2026-06-01 10:00:00','Marketing Hiring',9002,'Job Board','Frank','Green','Frank','Green','555-0103',NULL,'applicant5002@example.test','1988-07-22 00:00:00','210 Sample Rd, Austin TX','210 Sample Rd',NULL,'Austin','TX','78702','Travis','Offered','External','Complete','2026-06-05 12:00:00','Offer Sent','2026-06-02 09:00:00','Offer','Offer Review','2026-05-30 09:00:00','2026-06-01 09:00:00','v1',55000.00,70000.00,'Offer','recruiter.a',5,120,4,96,5760,NULL,NULL,NULL,'SEAT-MKT-01','Marketing Specialist Seat',NULL),
('CLIENT_A',5003,'LinkedIn','2026-06-10 09:00:00',NULL,NULL,'Engineering Hiring',9003,'Social Media','Gina','Hall','Gina','Hall','555-0104',NULL,'applicant5003@example.test','1995-03-30 00:00:00','320 Demo Ln, Austin TX','320 Demo Ln',NULL,'Austin','TX','78703','Travis','Active','External','Incomplete',NULL,'No Offer Letter',NULL,'Screening','Phone Screen',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,7,168,10080,NULL,NULL,NULL,'SEAT-ENG-01','Software Engineer Seat',NULL),
('CLIENT_B',5004,'Employee Referral','2020-01-05 09:00:00','Hired','2020-02-01 10:00:00','Sales Hiring',9004,'Company Website','Carol','White','Caz','White','555-0105',NULL,'applicant5004@example.test','1985-11-02 00:00:00','300 Fake Blvd, San Diego CA','300 Fake Blvd',NULL,'San Diego','CA','92101','San Diego','Hired','External','Complete','2020-01-20 12:00:00','Offer Signed','2020-01-15 09:00:00','Completed','Onboarded','2020-01-10 09:00:00','2020-01-14 09:00:00','v1',60000.00,75000.00,'Completed','recruiter.b',2,48,3,72,4320,12,1,36,'SEAT-D100-01','Sales Manager Seat','EMP003'),
('CLIENT_B',5005,'Career Fair','2026-06-20 09:00:00','Rejected','2026-06-25 10:00:00','HR Hiring',9005,'Career Fair','Henry','Irwin','Hank','Irwin','555-0106',NULL,'applicant5005@example.test','1992-09-14 00:00:00','450 Trial Ct, San Diego CA','450 Trial Ct',NULL,'San Diego','CA','92102','San Diego','Rejected','External','Complete',NULL,'No Offer Letter',NULL,'Screening','Rejected',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,4,96,5760,NULL,NULL,NULL,'SEAT-D200-01','HR Generalist Seat',NULL);

-- =====================================================================================================
-- applicant_tracking_requisition — grain [RequisitionId] (UNIQUE, Int32). Standalone (no joins).
-- RequisitionDepartmentCode<->RequisitionDepartmentDescription kept consistent.
-- =====================================================================================================
DROP TABLE IF EXISTS dbpcm_warehouse.applicant_tracking_requisition;
CREATE TABLE dbpcm_warehouse.applicant_tracking_requisition
(
    ClientCode                        Nullable(String),
    RequisitionId                     Int32,
    RequisitionStatusEnum             Nullable(String),
    RequisitionPositionTitle          Nullable(String),
    RequisitionPositionCode           Nullable(String),
    RequisitionPositionSeatCodes      Nullable(String),
    RequisitionPositionSeatTitles     Nullable(String),
    RequisitionJobTitle               Nullable(String),
    RequisitionPublishedJobTitle      Nullable(String),
    RequisitionPositionType           Nullable(String),
    RequisitionPostingStartDate       Nullable(DateTime64(6)),
    RequisitionPostingEndDate         Nullable(DateTime64(6)),
    RequisitionPostingFilledDate      Nullable(DateTime64(6)),
    RequisitionPositionLevelEnum      Nullable(String),
    RequisitionDepartmentCode         Nullable(String),
    RequisitionDepartmentDescription  Nullable(String),
    RequisitionOpenPositions          Nullable(String),
    RequisitionNumberOfHires          Nullable(Int32),
    RequisitionLocation               Nullable(String),
    RequestTypeEnum                   Nullable(String),
    RequestReason                     Nullable(String),
    PrimaryRecruiter                  Nullable(String),
    HiringManagerUsername             Nullable(String)
)
ENGINE = MergeTree
ORDER BY RequisitionId;

INSERT INTO dbpcm_warehouse.applicant_tracking_requisition
(ClientCode, RequisitionId, RequisitionStatusEnum, RequisitionPositionTitle, RequisitionPositionCode, RequisitionPositionSeatCodes, RequisitionPositionSeatTitles, RequisitionJobTitle, RequisitionPublishedJobTitle, RequisitionPositionType, RequisitionPostingStartDate, RequisitionPostingEndDate, RequisitionPostingFilledDate, RequisitionPositionLevelEnum, RequisitionDepartmentCode, RequisitionDepartmentDescription, RequisitionOpenPositions, RequisitionNumberOfHires, RequisitionLocation, RequestTypeEnum, RequestReason, PrimaryRecruiter, HiringManagerUsername) VALUES
('CLIENT_A',7001,'Posted','Software Engineer','PC-ENG-2','SEAT-ENG-01','Software Engineer Seat','Software Engineer','Software Engineer (Remote)','Full Time','2026-06-01 00:00:00','2026-08-01 00:00:00',NULL,'Experienced','ENG','Engineering','2',0,'Austin HQ','Additional','Team growth','recruiter.a','manager.eng'),
('CLIENT_A',7002,'Filled','Sales Representative','PC-SLS-1','SEAT-SLS-01','Sales Representative Seat','Sales Representative','Sales Representative','Full Time','2021-01-01 00:00:00','2021-03-01 00:00:00','2021-03-01 00:00:00','Entry','SLS','Sales','0',1,'Austin HQ','Replacement','Backfill departure','recruiter.a','manager.sales'),
('CLIENT_A',7003,'Draft','Financial Analyst','PC-FIN-3','SEAT-FIN-01','Financial Analyst Seat','Financial Analyst','Financial Analyst','Full Time',NULL,NULL,NULL,'Senior','FIN','Finance','1',0,'Austin HQ','Additional','New headcount','recruiter.a','manager.finance'),
('CLIENT_B',7004,'Filled','Sales Manager','CB-SLS-4','SEAT-D100-01','Sales Manager Seat','Sales Manager','Sales Manager','Full Time','2020-01-01 00:00:00','2020-02-01 00:00:00','2020-02-01 00:00:00','Management','D100','Sales','0',1,'San Diego Branch','Replacement','Manager backfill','recruiter.b','cb.manager'),
('CLIENT_B',7005,'Closed','HR Generalist Intern','CB-HR-5','SEAT-D200-02','HR Intern Seat','HR Intern','HR Generalist Intern','Internship','2026-04-01 00:00:00','2026-05-01 00:00:00',NULL,'Entry','D200','Human Resources','0',0,'San Diego Branch','Seasonal','Summer internship','recruiter.b','cb.manager');

-- =====================================================================================================
-- candidate_education — grain [EducationId] (UNIQUE). ApplicationId FK -> applicant_tracking_application.
-- =====================================================================================================
DROP TABLE IF EXISTS dbpcm_warehouse.candidate_education;
CREATE TABLE dbpcm_warehouse.candidate_education
(
    ClientCode                 Nullable(String),
    EducationId                Nullable(Int32),
    ApplicationId              Nullable(Int32),
    InstituteName              Nullable(String),
    EducationCity              Nullable(String),
    EducationState             Nullable(String),
    EducationCountry           Nullable(String),
    StudentDegree              Nullable(String),
    StudentMajor               Nullable(String),
    StudentMinor               Nullable(String),
    StudentAlias               Nullable(String),
    StudentGPA                 Nullable(Decimal(18, 6)),
    Graduated                  Nullable(String),
    CurrentlyEnrolled          Nullable(String),
    EducationDateAttendedStart Nullable(String),
    EducationDateAttendedEnd   Nullable(String),
    AnticipatedGraduationDate  Nullable(String),
    EducationPhoneNumber       Nullable(String),
    InstitutionType            Nullable(String)
)
ENGINE = MergeTree
ORDER BY EducationId
SETTINGS allow_nullable_key = 1;

INSERT INTO dbpcm_warehouse.candidate_education
(ClientCode, EducationId, ApplicationId, InstituteName, EducationCity, EducationState, EducationCountry, StudentDegree, StudentMajor, StudentMinor, StudentAlias, StudentGPA, Graduated, CurrentlyEnrolled, EducationDateAttendedStart, EducationDateAttendedEnd, AnticipatedGraduationDate, EducationPhoneNumber, InstitutionType) VALUES
('CLIENT_A',8001,5001,'State University','Austin','TX','US','BS','Business Administration','Economics','Alice Smith',3.50,'Yes','No','2008-08-01','2012-05-15',NULL,'555-0201','University'),
('CLIENT_A',8002,5002,'City Community College','Austin','TX','US','AA','Marketing',NULL,'Frank Green',3.20,'Yes','No','2006-09-01','2008-06-01',NULL,'555-0202','University / Graduate School'),
('CLIENT_B',8003,5004,'Lincoln High School','San Diego','CA','US','High School Diploma',NULL,NULL,'Carol White',NULL,'Yes','No','2000-09-01','2004-06-10',NULL,'555-0203','High School');

-- =====================================================================================================
-- candidate_employment_history — grain [EmploymentId] (UNIQUE). ApplicationId FK -> application.
-- =====================================================================================================
DROP TABLE IF EXISTS dbpcm_warehouse.candidate_employment_history;
CREATE TABLE dbpcm_warehouse.candidate_employment_history
(
    ClientCode            Nullable(String),
    EmploymentId          Nullable(Int32),
    ApplicationId         Nullable(Int32),
    EmployerName          Nullable(String),
    EmployerStreetAddress Nullable(String),
    EmployerCity          Nullable(String),
    EmployerState         Nullable(String),
    EmployerZipCode       Nullable(String),
    EmployerCountry       Nullable(String),
    EmployerPhone         Nullable(String),
    PreviousJobTitle      Nullable(String),
    PreviousJobDuties     Nullable(String),
    PreviousSupervisor    Nullable(String),
    ReasonLeft            Nullable(String),
    CanContactEmployer    Nullable(String),
    CurrentEmployer       Nullable(String),
    EmploymentStartDate   Nullable(String),
    EmploymentEndDate     Nullable(String)
)
ENGINE = MergeTree
ORDER BY EmploymentId
SETTINGS allow_nullable_key = 1;

INSERT INTO dbpcm_warehouse.candidate_employment_history
(ClientCode, EmploymentId, ApplicationId, EmployerName, EmployerStreetAddress, EmployerCity, EmployerState, EmployerZipCode, EmployerCountry, EmployerPhone, PreviousJobTitle, PreviousJobDuties, PreviousSupervisor, ReasonLeft, CanContactEmployer, CurrentEmployer, EmploymentStartDate, EmploymentEndDate) VALUES
('CLIENT_A',8101,5001,'Prior Retail Corp','12 Sample Blvd','Dallas','TX','75201','US','555-0301','Junior Sales Associate','Retail floor sales and customer service','Pat Nolan','Career growth','Yes','No','2016-06-01','2021-02-15'),
('CLIENT_B',8102,5004,'Regional Retail Co','88 Trial Ave','Los Angeles','CA','90001','US','555-0302','Store Associate','Merchandising and inventory','Jordan Reyes','Relocation','No','No','2015-03-01','2020-01-01'),
('CLIENT_B',8103,5005,'Tech Solutions LLC','77 Demo Way','San Diego','CA','92103','US','555-0303','Operations Analyst','Reporting and process support','Sam Park','Seeking new opportunity','Yes','Yes','2019-01-01',NULL);

-- =====================================================================================================
-- performance_discussions — grain [] (grain_verifiable: false). Field-response rows; DiscussionId
-- repeats. DiscussionTemplate/DiscussionType client-defined. EmployeeCode FK -> employee.
-- =====================================================================================================
DROP TABLE IF EXISTS dbpcm_warehouse.performance_discussions;
CREATE TABLE dbpcm_warehouse.performance_discussions
(
    ClientCode                 Nullable(String),
    EmployeeCode               String,
    DiscussionId               Nullable(Int32),
    ApprovalNotes              Nullable(String),
    Approver                   Nullable(String),
    CreatedBy                  Nullable(String),
    CreationDate               Nullable(DateTime64(6)),
    DiscussionNotes            Nullable(String),
    DiscussionNotesUpdatedBy   Nullable(String),
    DiscussionReason           Nullable(String),
    DiscussionState            Nullable(String),
    DiscussionTemplate         Nullable(String),
    DiscussionType             Nullable(String),
    DocumentUploads            Nullable(String),
    DocumentUploadsUpdatedBy   Nullable(String),
    EmployeeAcknowledgment     Nullable(String),
    EmployeeAcknowledgmentDate Nullable(DateTime64(6)),
    EmployeeSignature          Nullable(String),
    ExceptionPointThreshold    Nullable(String),
    ExceptionPointTotal        Nullable(String),
    FieldAnswer                Nullable(String),
    FieldCompletedBy           Nullable(String),
    FieldDescription           Nullable(String),
    LastActionDate             Nullable(DateTime64(6)),
    LastModifiedBy             Nullable(String),
    ManagerAcknowledgment      Nullable(String),
    ManagerApprovalDate        Nullable(DateTime64(6)),
    ManagerSignature           Nullable(String),
    MonitoringPeriodDays       Nullable(String),
    MonitoringPeriodEndDate    Nullable(DateTime64(6)),
    ResolvedDate               Nullable(DateTime64(6)),
    SendBackNotes              Nullable(String),
    WaitingOn                  Nullable(String)
)
ENGINE = MergeTree
ORDER BY EmployeeCode;

INSERT INTO dbpcm_warehouse.performance_discussions
(ClientCode, EmployeeCode, DiscussionId, ApprovalNotes, Approver, CreatedBy, CreationDate, DiscussionNotes, DiscussionNotesUpdatedBy, DiscussionReason, DiscussionState, DiscussionTemplate, DiscussionType, DocumentUploads, DocumentUploadsUpdatedBy, EmployeeAcknowledgment, EmployeeAcknowledgmentDate, EmployeeSignature, ExceptionPointThreshold, ExceptionPointTotal, FieldAnswer, FieldCompletedBy, FieldDescription, LastActionDate, LastModifiedBy, ManagerAcknowledgment, ManagerApprovalDate, ManagerSignature, MonitoringPeriodDays, MonitoringPeriodEndDate, ResolvedDate, SendBackNotes, WaitingOn) VALUES
('CLIENT_A','EMP001',3001,'Reviewed and closed','manager.sales','manager.sales','2026-02-10 09:00:00','Quarterly check-in','manager.sales','Quarterly 1:1','Resolved','TMPL_1ON1','1 on 1',NULL,NULL,'Acknowledgment','2026-02-12 10:00:00','Alice Smith',NULL,NULL,'Exceeding sales targets','manager.sales','What went well this quarter?','2026-02-12 10:00:00','manager.sales','Acknowledgment','2026-02-11 09:00:00','J. Manager',NULL,NULL,'2026-02-12 11:00:00',NULL,NULL),
('CLIENT_A','EMP001',3001,'Reviewed and closed','manager.sales','manager.sales','2026-02-10 09:00:00','Quarterly check-in','manager.sales','Quarterly 1:1','Resolved','TMPL_1ON1','1 on 1',NULL,NULL,'Acknowledgment','2026-02-12 10:00:00','Alice Smith',NULL,NULL,'Wants a mentorship path','manager.sales','Development goals?','2026-02-12 10:00:00','manager.sales','Acknowledgment','2026-02-11 09:00:00','J. Manager',NULL,NULL,'2026-02-12 11:00:00',NULL,NULL),
('CLIENT_A','EMP002',3002,NULL,NULL,'manager.eng','2026-05-01 09:00:00','PIP for delivery timelines','manager.eng','Performance concern','Final Approval','TMPL_PIP','Performance Improvement Plan','pip_plan_v1.pdf','manager.eng','Acknowledgment','2026-05-03 10:00:00','Bob Jones','Tier 2','6.0','Committed to improvement plan','manager.eng','Employee response to plan','2026-05-05 15:00:00','manager.eng','Acknowledgment','2026-05-04 09:00:00','E. Manager','30','2026-06-05 00:00:00',NULL,NULL,'HR Review'),
('CLIENT_A','EMP002',3002,NULL,NULL,'manager.eng','2026-05-01 09:00:00','PIP for delivery timelines','manager.eng','Performance concern','Final Approval','TMPL_PIP','Performance Improvement Plan','pip_plan_v1.pdf','manager.eng','Acknowledgment','2026-05-03 10:00:00','Bob Jones','Tier 2','6.0','Milestones defined for 30 days','manager.eng','Improvement milestones','2026-05-05 15:00:00','manager.eng','Acknowledgment','2026-05-04 09:00:00','E. Manager','30','2026-06-05 00:00:00',NULL,NULL,'HR Review'),
('CLIENT_B','EMP003',3003,NULL,NULL,'cb.manager','2026-06-01 09:00:00','Corrective action - missed quota','cb.manager','Performance','Employee Approval','CB_CAF','Corrective Action Form - Performance',NULL,NULL,'Declined','2026-06-03 10:00:00',NULL,'Tier 1','3.0','Disputes the assessment','cb.manager','Employee statement','2026-06-03 12:00:00','cb.manager','Acknowledgment','2026-06-02 09:00:00','C. Manager','45','2026-07-16 00:00:00',NULL,NULL,'Employee'),
('CLIENT_B','EMP003',3003,NULL,NULL,'cb.manager','2026-06-01 09:00:00','Corrective action - missed quota','cb.manager','Performance','Employee Approval','CB_CAF','Corrective Action Form - Performance',NULL,NULL,'Declined','2026-06-03 10:00:00',NULL,'Tier 1','3.0','Manager to schedule follow-up','cb.manager','Next steps','2026-06-03 12:00:00','cb.manager','Acknowledgment','2026-06-02 09:00:00','C. Manager','45','2026-07-16 00:00:00',NULL,NULL,'Employee'),
('CLIENT_B','EMP004',3004,'Exit documented','hr.cb','hr.cb','2025-11-25 09:00:00','Voluntary exit interview','hr.cb','Offboarding','Archived','CB_EXIT','Exit Interview',NULL,NULL,'Acknowledgment','2025-11-26 10:00:00','David Lee',NULL,NULL,'Left for relocation','hr.cb','Reason for leaving','2025-11-26 11:00:00','hr.cb','Acknowledgment','2025-11-26 09:00:00','H. Manager',NULL,NULL,'2025-11-30 12:00:00',NULL,NULL);
