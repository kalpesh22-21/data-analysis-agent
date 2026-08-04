-- Layer-2 integration fixture (snake_case): synthetic HR warehouse seed for the 7 non-core tables
-- (accrual_events, personnel_action_form_changes, applicant_tracking_application,
--  applicant_tracking_requisition, candidate_education, candidate_employment_history,
--  performance_discussions). Column NAMES are snake_case-exact to the Wave-1 Semantic Catalog
-- (clickhouse-api/app/semantic_catalog/data/*.yaml) so the getTableSchema overlay, column-scope,
-- resolveValues, grain and referential-integrity checks are meaningful against real ClickHouse.
--
-- NOTE: the 4 CORE tables (employee, payroll, department, labor_allocation) are seeded by the
-- sibling migration hr-4tables-snake-migration.sql and are NO LONGER defined here (the old
-- PascalCase employee/payroll blocks were superseded by that migration). Run the 4-table migration
-- FIRST so the employee parent rows (EMP001-EMP005) exist before these child tables load.
--
-- This is a SYNTHETIC TEST FIXTURE, not production data. All PII (applicant phones/emails/DOB/
-- addresses) is OBVIOUSLY FAKE (555-xxxx phones, @example.test emails, "Fake St" addresses).
--
-- Design invariants:
--   * Referential integrity: every child employee_code (accrual_events, PAF, ATS-application for
--     hired candidates, performance_discussions) exists in employee (EMP001-EMP005). Every
--     candidate_education/candidate_employment_history.application_id exists in
--     applicant_tracking_application.
--   * grain=[] tables (accrual_events, personnel_action_form_changes, performance_discussions)
--     carry DELIBERATE duplicates on their logical grouping key (employee_code / paf_transaction_id /
--     discussion_id), matching grain_verifiable: false in the catalog.
--   * Sibling description columns kept code-consistent (earn_code<->earn_description,
--     field_id<->field_label, requisition_department_code<->requisition_department_description).

CREATE DATABASE IF NOT EXISTS dbpcm_warehouse;

-- =====================================================================================================
-- accrual_events — grain [] (grain_verifiable: false). ONE ROW PER DAY of a request.
-- EarnCode<->EarnDescription kept consistent. EmployeeCode FK -> employee.
-- =====================================================================================================
DROP TABLE IF EXISTS dbpcm_warehouse.accrual_events;
CREATE TABLE dbpcm_warehouse.accrual_events
(
    client_code                  Nullable(String),
    employee_code                String,
    request_date                 Nullable(DateTime64(6)),
    hours                       Nullable(Decimal(18, 6)),
    earn_code                    Nullable(String),
    earn_description             Nullable(String),
    status                      Nullable(String),
    reason                      Nullable(String),
    event_type                   Nullable(String),
    date_added                   Nullable(DateTime64(6)),
    comments                    Nullable(String),
    gone_reason                  Nullable(String),
    accrual_event_department_code  Nullable(String)
)
ENGINE = MergeTree
ORDER BY employee_code;

INSERT INTO dbpcm_warehouse.accrual_events
(client_code, employee_code, request_date, hours, earn_code, earn_description, status, reason, event_type, date_added, comments, gone_reason, accrual_event_department_code) VALUES
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
    client_code             Nullable(String),
    paf_transaction_id       Int32,
    employee_code           Nullable(String),
    employee_name           Nullable(String),
    paf_effective_date       Nullable(DateTime64(6)),
    paf_effective_date_time   Nullable(DateTime64(6)),
    paf_status              Nullable(String),
    paf_comments            Nullable(String),
    forward_to              Nullable(String),
    created_by              Nullable(String),
    modified_by             Nullable(String),
    created_date            Nullable(DateTime64(6)),
    modified_date           Nullable(DateTime64(6)),
    manager_sign_date        Nullable(DateTime64(6)),
    field_id                Nullable(String),
    field_label             Nullable(String),
    old_value               Nullable(String),
    proposed_value          Nullable(String)
)
ENGINE = MergeTree
ORDER BY paf_transaction_id;

INSERT INTO dbpcm_warehouse.personnel_action_form_changes
(client_code, paf_transaction_id, employee_code, employee_name, paf_effective_date, paf_effective_date_time, paf_status, paf_comments, forward_to, created_by, modified_by, created_date, modified_date, manager_sign_date, field_id, field_label, old_value, proposed_value) VALUES
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
    client_code                          Nullable(String),
    application_id                       Int32,
    referral_source                      Nullable(String),
    application_date                     DateTime64(6),
    disposition                         Nullable(String),
    disposition_date                     Nullable(DateTime64(6)),
    work_flow_group_name                   Nullable(String),
    candidate_id                         Nullable(Int32),
    application_source                   Nullable(String),
    applicant_first_name                  Nullable(String),
    applicant_last_name                   Nullable(String),
    applicant_preferred_first_name         Nullable(String),
    applicant_preferred_last_name          Nullable(String),
    applicant_primary_phone               Nullable(String),
    applicant_secondary_phone             Nullable(String),
    applicant_email_address               Nullable(String),
    applicant_birth_date                  Nullable(DateTime64(6)),
    applicant_full_street_address          Nullable(String),
    applicant_street_address_1             Nullable(String),
    applicant_street_address_2             Nullable(String),
    applicant_city                       Nullable(String),
    applicant_state                      Nullable(String),
    applicant_zip_code                    Nullable(String),
    applicant_county                     Nullable(String),
    application_status_enum               Nullable(String),
    application_type                     Nullable(String),
    complete_incomplete                  Nullable(String),
    offer_letter_accepted_rejected_date     Nullable(DateTime64(6)),
    offer_letter_status_enum               Nullable(String),
    offer_letter_sent_date                 Nullable(DateTime64(6)),
    approval_step                        Nullable(String),
    work_flow_step                        Nullable(String),
    offer_letter_created_on_date            Nullable(DateTime64(6)),
    offer_letter_approval_date             Nullable(DateTime64(6)),
    offer_version                        Nullable(String),
    application_job_wage_start             Nullable(Decimal(18, 6)),
    application_job_wage_end               Nullable(Decimal(18, 6)),
    hiring_process_step                   Nullable(String),
    user_assigned_to_step                  Nullable(String),
    days_in_offer_status                   Nullable(Int32),
    hours_in_offer_status                  Nullable(Int32),
    days_in_hiring_step                    Nullable(Int32),
    hours_in_hiring_step                   Nullable(Int32),
    minutes_in_hiring_step                 Nullable(Int32),
    hours_in_new_hire_queue                 Nullable(Int32),
    days_in_new_hire_queue                  Nullable(Int32),
    total_onboarding_hours                Nullable(Int32),
    application_position_seat_code         Nullable(String),
    application_position_seat_description  Nullable(String),
    employee_code                        Nullable(String)
)
ENGINE = MergeTree
ORDER BY application_id;

INSERT INTO dbpcm_warehouse.applicant_tracking_application
(client_code, application_id, referral_source, application_date, disposition, disposition_date, work_flow_group_name, candidate_id, application_source, applicant_first_name, applicant_last_name, applicant_preferred_first_name, applicant_preferred_last_name, applicant_primary_phone, applicant_secondary_phone, applicant_email_address, applicant_birth_date, applicant_full_street_address, applicant_street_address_1, applicant_street_address_2, applicant_city, applicant_state, applicant_zip_code, applicant_county, application_status_enum, application_type, complete_incomplete, offer_letter_accepted_rejected_date, offer_letter_status_enum, offer_letter_sent_date, approval_step, work_flow_step, offer_letter_created_on_date, offer_letter_approval_date, offer_version, application_job_wage_start, application_job_wage_end, hiring_process_step, user_assigned_to_step, days_in_offer_status, hours_in_offer_status, days_in_hiring_step, hours_in_hiring_step, minutes_in_hiring_step, hours_in_new_hire_queue, days_in_new_hire_queue, total_onboarding_hours, application_position_seat_code, application_position_seat_description, employee_code) VALUES
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
    client_code                        Nullable(String),
    requisition_id                     Int32,
    requisition_status_enum             Nullable(String),
    requisition_position_title          Nullable(String),
    requisition_position_code           Nullable(String),
    requisition_position_seat_codes      Nullable(String),
    requisition_position_seat_titles     Nullable(String),
    requisition_job_title               Nullable(String),
    requisition_published_job_title      Nullable(String),
    requisition_position_type           Nullable(String),
    requisition_posting_start_date       Nullable(DateTime64(6)),
    requisition_posting_end_date         Nullable(DateTime64(6)),
    requisition_posting_filled_date      Nullable(DateTime64(6)),
    requisition_position_level_enum      Nullable(String),
    requisition_department_code         Nullable(String),
    requisition_department_description  Nullable(String),
    requisition_open_positions          Nullable(String),
    requisition_number_of_hires          Nullable(Int32),
    requisition_location               Nullable(String),
    request_type_enum                   Nullable(String),
    request_reason                     Nullable(String),
    primary_recruiter                  Nullable(String),
    hiring_manager_username             Nullable(String)
)
ENGINE = MergeTree
ORDER BY requisition_id;

INSERT INTO dbpcm_warehouse.applicant_tracking_requisition
(client_code, requisition_id, requisition_status_enum, requisition_position_title, requisition_position_code, requisition_position_seat_codes, requisition_position_seat_titles, requisition_job_title, requisition_published_job_title, requisition_position_type, requisition_posting_start_date, requisition_posting_end_date, requisition_posting_filled_date, requisition_position_level_enum, requisition_department_code, requisition_department_description, requisition_open_positions, requisition_number_of_hires, requisition_location, request_type_enum, request_reason, primary_recruiter, hiring_manager_username) VALUES
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
    client_code                 Nullable(String),
    education_id                Nullable(Int32),
    application_id              Nullable(Int32),
    institute_name              Nullable(String),
    education_city              Nullable(String),
    education_state             Nullable(String),
    education_country           Nullable(String),
    student_degree              Nullable(String),
    student_major               Nullable(String),
    student_minor               Nullable(String),
    student_alias               Nullable(String),
    student_gpa                 Nullable(Decimal(18, 6)),
    graduated                  Nullable(String),
    currently_enrolled          Nullable(String),
    education_date_attended_start Nullable(String),
    education_date_attended_end   Nullable(String),
    anticipated_graduation_date  Nullable(String),
    education_phone_number       Nullable(String),
    institution_type            Nullable(String)
)
ENGINE = MergeTree
ORDER BY education_id
SETTINGS allow_nullable_key = 1;

INSERT INTO dbpcm_warehouse.candidate_education
(client_code, education_id, application_id, institute_name, education_city, education_state, education_country, student_degree, student_major, student_minor, student_alias, student_gpa, graduated, currently_enrolled, education_date_attended_start, education_date_attended_end, anticipated_graduation_date, education_phone_number, institution_type) VALUES
('CLIENT_A',8001,5001,'State University','Austin','TX','US','BS','Business Administration','Economics','Alice Smith',3.50,'Yes','No','2008-08-01','2012-05-15',NULL,'555-0201','University'),
('CLIENT_A',8002,5002,'City Community College','Austin','TX','US','AA','Marketing',NULL,'Frank Green',3.20,'Yes','No','2006-09-01','2008-06-01',NULL,'555-0202','University / Graduate School'),
('CLIENT_B',8003,5004,'Lincoln High School','San Diego','CA','US','High School Diploma',NULL,NULL,'Carol White',NULL,'Yes','No','2000-09-01','2004-06-10',NULL,'555-0203','High School');

-- =====================================================================================================
-- candidate_employment_history — grain [EmploymentId] (UNIQUE). ApplicationId FK -> application.
-- =====================================================================================================
DROP TABLE IF EXISTS dbpcm_warehouse.candidate_employment_history;
CREATE TABLE dbpcm_warehouse.candidate_employment_history
(
    client_code            Nullable(String),
    employment_id          Nullable(Int32),
    application_id         Nullable(Int32),
    employer_name          Nullable(String),
    employer_street_address Nullable(String),
    employer_city          Nullable(String),
    employer_state         Nullable(String),
    employer_zip_code       Nullable(String),
    employer_country       Nullable(String),
    employer_phone         Nullable(String),
    previous_job_title      Nullable(String),
    previous_job_duties     Nullable(String),
    previous_supervisor    Nullable(String),
    reason_left            Nullable(String),
    can_contact_employer    Nullable(String),
    current_employer       Nullable(String),
    employment_start_date   Nullable(String),
    employment_end_date     Nullable(String)
)
ENGINE = MergeTree
ORDER BY employment_id
SETTINGS allow_nullable_key = 1;

INSERT INTO dbpcm_warehouse.candidate_employment_history
(client_code, employment_id, application_id, employer_name, employer_street_address, employer_city, employer_state, employer_zip_code, employer_country, employer_phone, previous_job_title, previous_job_duties, previous_supervisor, reason_left, can_contact_employer, current_employer, employment_start_date, employment_end_date) VALUES
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
    client_code                 Nullable(String),
    employee_code               String,
    discussion_id               Nullable(Int32),
    approval_notes              Nullable(String),
    approver                   Nullable(String),
    created_by                  Nullable(String),
    creation_date               Nullable(DateTime64(6)),
    discussion_notes            Nullable(String),
    discussion_notes_updated_by   Nullable(String),
    discussion_reason           Nullable(String),
    discussion_state            Nullable(String),
    discussion_template         Nullable(String),
    discussion_type             Nullable(String),
    document_uploads            Nullable(String),
    document_uploads_updated_by   Nullable(String),
    employee_acknowledgment     Nullable(String),
    employee_acknowledgment_date Nullable(DateTime64(6)),
    employee_signature          Nullable(String),
    exception_point_threshold    Nullable(String),
    exception_point_total        Nullable(String),
    field_answer                Nullable(String),
    field_completed_by           Nullable(String),
    field_description           Nullable(String),
    last_action_date             Nullable(DateTime64(6)),
    last_modified_by             Nullable(String),
    manager_acknowledgment      Nullable(String),
    manager_approval_date        Nullable(DateTime64(6)),
    manager_signature           Nullable(String),
    monitoring_period_days       Nullable(String),
    monitoring_period_end_date    Nullable(DateTime64(6)),
    resolved_date               Nullable(DateTime64(6)),
    send_back_notes              Nullable(String),
    waiting_on                  Nullable(String)
)
ENGINE = MergeTree
ORDER BY employee_code;

INSERT INTO dbpcm_warehouse.performance_discussions
(client_code, employee_code, discussion_id, approval_notes, approver, created_by, creation_date, discussion_notes, discussion_notes_updated_by, discussion_reason, discussion_state, discussion_template, discussion_type, document_uploads, document_uploads_updated_by, employee_acknowledgment, employee_acknowledgment_date, employee_signature, exception_point_threshold, exception_point_total, field_answer, field_completed_by, field_description, last_action_date, last_modified_by, manager_acknowledgment, manager_approval_date, manager_signature, monitoring_period_days, monitoring_period_end_date, resolved_date, send_back_notes, waiting_on) VALUES
('CLIENT_A','EMP001',3001,'Reviewed and closed','manager.sales','manager.sales','2026-02-10 09:00:00','Quarterly check-in','manager.sales','Quarterly 1:1','Resolved','TMPL_1ON1','1 on 1',NULL,NULL,'Acknowledgment','2026-02-12 10:00:00','Alice Smith',NULL,NULL,'Exceeding sales targets','manager.sales','What went well this quarter?','2026-02-12 10:00:00','manager.sales','Acknowledgment','2026-02-11 09:00:00','J. Manager',NULL,NULL,'2026-02-12 11:00:00',NULL,NULL),
('CLIENT_A','EMP001',3001,'Reviewed and closed','manager.sales','manager.sales','2026-02-10 09:00:00','Quarterly check-in','manager.sales','Quarterly 1:1','Resolved','TMPL_1ON1','1 on 1',NULL,NULL,'Acknowledgment','2026-02-12 10:00:00','Alice Smith',NULL,NULL,'Wants a mentorship path','manager.sales','Development goals?','2026-02-12 10:00:00','manager.sales','Acknowledgment','2026-02-11 09:00:00','J. Manager',NULL,NULL,'2026-02-12 11:00:00',NULL,NULL),
('CLIENT_A','EMP002',3002,NULL,NULL,'manager.eng','2026-05-01 09:00:00','PIP for delivery timelines','manager.eng','Performance concern','Final Approval','TMPL_PIP','Performance Improvement Plan','pip_plan_v1.pdf','manager.eng','Acknowledgment','2026-05-03 10:00:00','Bob Jones','Tier 2','6.0','Committed to improvement plan','manager.eng','Employee response to plan','2026-05-05 15:00:00','manager.eng','Acknowledgment','2026-05-04 09:00:00','E. Manager','30','2026-06-05 00:00:00',NULL,NULL,'HR Review'),
('CLIENT_A','EMP002',3002,NULL,NULL,'manager.eng','2026-05-01 09:00:00','PIP for delivery timelines','manager.eng','Performance concern','Final Approval','TMPL_PIP','Performance Improvement Plan','pip_plan_v1.pdf','manager.eng','Acknowledgment','2026-05-03 10:00:00','Bob Jones','Tier 2','6.0','Milestones defined for 30 days','manager.eng','Improvement milestones','2026-05-05 15:00:00','manager.eng','Acknowledgment','2026-05-04 09:00:00','E. Manager','30','2026-06-05 00:00:00',NULL,NULL,'HR Review'),
('CLIENT_B','EMP003',3003,NULL,NULL,'cb.manager','2026-06-01 09:00:00','Corrective action - missed quota','cb.manager','Performance','Employee Approval','CB_CAF','Corrective Action Form - Performance',NULL,NULL,'Declined','2026-06-03 10:00:00',NULL,'Tier 1','3.0','Disputes the assessment','cb.manager','Employee statement','2026-06-03 12:00:00','cb.manager','Acknowledgment','2026-06-02 09:00:00','C. Manager','45','2026-07-16 00:00:00',NULL,NULL,'Employee'),
('CLIENT_B','EMP003',3003,NULL,NULL,'cb.manager','2026-06-01 09:00:00','Corrective action - missed quota','cb.manager','Performance','Employee Approval','CB_CAF','Corrective Action Form - Performance',NULL,NULL,'Declined','2026-06-03 10:00:00',NULL,'Tier 1','3.0','Manager to schedule follow-up','cb.manager','Next steps','2026-06-03 12:00:00','cb.manager','Acknowledgment','2026-06-02 09:00:00','C. Manager','45','2026-07-16 00:00:00',NULL,NULL,'Employee'),
('CLIENT_B','EMP004',3004,'Exit documented','hr.cb','hr.cb','2025-11-25 09:00:00','Voluntary exit interview','hr.cb','Offboarding','Archived','CB_EXIT','Exit Interview',NULL,NULL,'Acknowledgment','2025-11-26 10:00:00','David Lee',NULL,NULL,'Left for relocation','hr.cb','Reason for leaving','2025-11-26 11:00:00','hr.cb','Acknowledgment','2025-11-26 09:00:00','H. Manager',NULL,NULL,'2025-11-30 12:00:00',NULL,NULL);
