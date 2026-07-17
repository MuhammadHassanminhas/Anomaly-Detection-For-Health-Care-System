-- Phase 2 step 5: disposable fixture DB for tests/execution.
-- Entirely synthetic data, shaped like a subset of the real catalog views
-- referenced by examples/checks/*.yaml -- never real INDICI_BI_Full data.
-- Applied by scripts/fixture_db.ps1 against a throwaway Docker SQL Server.

IF DB_ID('cdss_fixture') IS NOT NULL
BEGIN
    ALTER DATABASE cdss_fixture SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
    DROP DATABASE cdss_fixture;
END
GO
CREATE DATABASE cdss_fixture;
GO
USE cdss_fixture;
GO
CREATE SCHEMA fqb;
GO

-- dbo.Appointments -------------------------------------------------------
-- WaitingForpayment/CancelledTime/Notes/ConsultStartTime: Phase 4 step 5
-- additions -- real semantic-catalog-v3.json columns the 12 LLM-drafted
-- checks reference that the original 6-example fixture schema lacked.
CREATE TABLE dbo.Appointments_Base (
    AppointmentID INT NOT NULL PRIMARY KEY,
    PatientID INT NOT NULL,
    ScheduleDate DATETIME2 NULL,
    AppointmentCompleted BIT NULL,
    AppointmentStatus NVARCHAR(50) NULL,
    IsDeleted BIT NOT NULL,
    IsDummy BIT NOT NULL,
    PracticeID INT NOT NULL,
    AppointmentType NVARCHAR(50) NULL,
    Provider NVARCHAR(50) NULL,
    WaitingForpayment DATETIME2 NULL,
    CancelledTime DATETIME2 NULL,
    Notes NVARCHAR(500) NULL,
    ConsultStartTime DATETIME2 NULL
);
GO
CREATE VIEW dbo.Appointments AS SELECT * FROM dbo.Appointments_Base;
GO

-- Rows 1-10: appointment-completed-no-invoice / appointment-invalid-status-code
-- fixture data. PatientID=901 is a placeholder with no dbo.Patient row --
-- these checks never join to dbo.Patient, so no correspondence is needed.
-- Row 11 is dedicated to patient-no-recent-appointment's not_exists join
-- (PatientID=2, a real dbo.Patient row below) and deliberately kept out of
-- the other two checks' results via IsDummy=1.
INSERT INTO dbo.Appointments_Base
    (AppointmentID, PatientID, ScheduleDate, AppointmentCompleted, AppointmentStatus, IsDeleted, IsDummy, PracticeID, AppointmentType, Provider)
VALUES
    (1,  901, DATEADD(day, -40, SYSDATETIME()), 1,    'Completed', 0, 0, 100, 'Consult', 'Dr A'),  -- fail: completed, stale, no invoice
    (2,  901, DATEADD(day, -40, SYSDATETIME()), 1,    'Completed', 0, 0, 100, 'Consult', 'Dr A'),  -- pass: has an active invoice
    (3,  901, DATEADD(day, -40, SYSDATETIME()), 0,    'Completed', 0, 0, 100, 'Consult', 'Dr A'),  -- pass: not completed
    (4,  901, SYSDATETIME(),                    1,    'Completed', 0, 0, 100, 'Consult', 'Dr A'),  -- pass: not stale yet
    (5,  901, DATEADD(day, -40, SYSDATETIME()), NULL, 'Completed', 0, 0, 100, 'Consult', 'Dr A'),  -- indeterminate: AppointmentCompleted NULL
    (6,  901, NULL,                             1,    'Completed', 0, 0, 100, 'Consult', 'Dr A'),  -- indeterminate: ScheduleDate NULL
    (7,  901, DATEADD(day, -40, SYSDATETIME()), 1,    'Bogus',     1, 0, 100, 'Consult', 'Dr A'),  -- excluded: IsDeleted = 1
    (8,  901, DATEADD(day, -40, SYSDATETIME()), 1,    NULL,        0, 1, 100, 'Consult', 'Dr A'),  -- excluded from check A: IsDummy = 1
    (9,  901, DATEADD(day, -40, SYSDATETIME()), 1,    'Booked',    0, 1, 100, 'Consult', 'Dr A'),  -- excluded from check A: IsDummy = 1
    (10, 901, DATEADD(day, -40, SYSDATETIME()), 1,    'Bogus',     0, 1, 100, 'Consult', 'Dr A'),  -- excluded from check A: IsDummy = 1
    (11, 2,   DATEADD(day, -10, SYSDATETIME()), 1,    'Completed', 0, 1, 100, 'Consult', 'Dr A');  -- recent appt for patient 2 (recall-window join target)
GO

-- Rows 12-19: Phase 4 step 5 -- fixture scenarios for the LLM-drafted checks
-- reviewed this phase. IsDummy=1 throughout (same convention as rows 8-10)
-- so none of these are picked up by appointment-completed-no-invoice.yaml's
-- own IsDummy=0 base filter.
INSERT INTO dbo.Appointments_Base
    (AppointmentID, PatientID, ScheduleDate, AppointmentCompleted, AppointmentStatus, IsDeleted, IsDummy, PracticeID, AppointmentType, Provider, WaitingForpayment, CancelledTime, Notes, ConsultStartTime)
VALUES
    -- appointment-activity-left-open / open-activity-with-no-follow-up: fail (On Hold, no wait-for-payment timestamp)
    (12, 901, DATEADD(day, -40, SYSDATETIME()), NULL, 'On Hold', 0, 1, 100, 'Consult', 'Dr A', NULL, NULL, NULL, DATEADD(day, -5, SYSDATETIME())),
    -- appointment-activity-left-open: pass (On Hold but already flagged waiting-for-payment)
    (13, 901, DATEADD(day, -40, SYSDATETIME()), NULL, 'On Hold', 0, 1, 100, 'Consult', 'Dr A', DATEADD(day, -2, SYSDATETIME()), NULL, NULL, DATEADD(day, -5, SYSDATETIME())),
    -- appointment-activity-left-open / open-activity-with-no-follow-up / uncompleted-appointment-requires-followup: indeterminate (AppointmentStatus NULL, AppointmentCompleted has a value)
    (14, 901, DATEADD(day, -40, SYSDATETIME()), 1,    NULL,       0, 1, 100, 'Consult', 'Dr A', NULL, NULL, NULL, DATEADD(day, -5, SYSDATETIME())),
    -- uncompleted-appointment-requires-followup: fail (Cancelled)
    (15, 901, DATEADD(day, -40, SYSDATETIME()), NULL, 'Cancelled', 0, 1, 100, 'Consult', 'Dr A', NULL, DATEADD(day, -1, SYSDATETIME()), NULL, NULL),
    -- missing-notes-on-completed-appointment / appointment-completed-no-invoice (LLM): fail (completed, no notes, no invoice)
    (16, 901, DATEADD(day, -40, SYSDATETIME()), 1,    'Appointment Completed', 0, 1, 100, 'Consult', 'Dr A', NULL, NULL, NULL, NULL),
    -- missing-notes-on-completed-appointment / appointment-completed-no-invoice (LLM): pass (completed, has notes, has invoice below)
    (17, 901, DATEADD(day, -40, SYSDATETIME()), 1,    'Appointment Completed', 0, 1, 100, 'Consult', 'Dr A', NULL, NULL, 'Patient discussed treatment plan.', NULL),
    -- missing-notes-on-completed-appointment / appointment-completed-no-invoice (LLM): indeterminate (AppointmentCompleted NULL)
    (18, 901, DATEADD(day, -40, SYSDATETIME()), NULL, NULL,       0, 1, 100, 'Consult', 'Dr A', NULL, NULL, NULL, NULL),
    -- high-risk-patient-no-follow-up / no-recent-appointment-high-needs-patient / no-appointment-overdue-follow-up: pass (recent appt for patient 7)
    (19, 7,   DATEADD(day, -10, SYSDATETIME()), 1,    'Completed', 0, 1, 100, 'Consult', 'Dr A', NULL, NULL, NULL, NULL),
    -- open-activity-with-no-follow-up: pass (has a consult-start timestamp, status matches neither On Hold nor Waiting for Payment)
    (20, 901, DATEADD(day, -40, SYSDATETIME()), 1,    'Completed', 0, 1, 100, 'Consult', 'Dr A', NULL, NULL, NULL, DATEADD(day, -3, SYSDATETIME()));
GO

-- Rows 21-30: Phase 4 step 6 (cdss.calibration) -- 10 completed appointments
-- under a dedicated PracticeID (200) linked 1:1 to fqb.Invoices rows 9-18
-- below with a controlled, known lag in days (n=1..10) -- gives
-- appointment_to_invoice_lag a hand-computable expectation (median of
-- 1..10 = 5.5) for at least MIN_SAMPLE_SIZE (10) observations.
INSERT INTO dbo.Appointments_Base
    (AppointmentID, PatientID, ScheduleDate, AppointmentCompleted, AppointmentStatus, IsDeleted, IsDummy, PracticeID, AppointmentType, Provider)
SELECT 20 + n, 901, DATEADD(day, -100, SYSDATETIME()), 1, 'Completed', 0, 1, 200, 'Consult', 'Dr A'
FROM (VALUES (1), (2), (3), (4), (5), (6), (7), (8), (9), (10)) AS numbers(n);
GO

-- dbo.Invoices (not_exists join target for appointment-completed-no-invoice) --
CREATE TABLE dbo.Invoices_Base (
    AppointmentID INT NOT NULL,
    IsActive BIT NOT NULL
);
GO
CREATE VIEW dbo.Invoices AS SELECT * FROM dbo.Invoices_Base;
GO
INSERT INTO dbo.Invoices_Base (AppointmentID, IsActive) VALUES
    (1, 0),  -- inactive invoice: must NOT block appointment 1's not_exists (still fails)
    (2, 1);  -- active invoice: blocks appointment 2's not_exists (passes)
GO

-- fqb.Invoices -------------------------------------------------------------
-- AppointmentID: Phase 4 step 5 addition -- a real semantic-catalog-v3.json
-- column appointment-completed-no-invoice (LLM-drafted) joins on, absent
-- from the original 6-example fixture schema.
CREATE TABLE fqb.Invoices_Base (
    InvoiceTransactionID INT NOT NULL PRIMARY KEY,
    PatientID INT NOT NULL,
    InvoiceDate DATETIME2 NULL,
    UnpaidAmount DECIMAL(18, 2) NULL,
    TotalAmount DECIMAL(18, 2) NULL,
    PracticeID INT NOT NULL,
    IsDeleted BIT NOT NULL,
    IsActive BIT NOT NULL,
    AppointmentID INT NULL
);
GO
CREATE VIEW fqb.Invoices AS SELECT * FROM fqb.Invoices_Base;
GO
INSERT INTO fqb.Invoices_Base
    (InvoiceTransactionID, PatientID, InvoiceDate, UnpaidAmount, TotalAmount, PracticeID, IsDeleted, IsActive)
VALUES
    (1, 1, DATEADD(day, -90, SYSDATETIME()), 100.00, -50.00, 100, 0, 1),  -- fail both checks
    (2, 1, DATEADD(day, -90, SYSDATETIME()), 0.00,   100.00, 100, 0, 1),  -- pass both checks
    (3, 1, DATEADD(day, -90, SYSDATETIME()), 50.00,  NULL,   100, 0, 1),  -- indeterminate (negative-total), fail (stale)
    (4, 1, NULL,                             50.00,  200.00, 100, 0, 1),  -- pass (negative-total), indeterminate (stale)
    (5, 1, DATEADD(day, -90, SYSDATETIME()), 50.00,  -10.00, 100, 1, 1),  -- excluded: IsDeleted = 1
    (6, 1, DATEADD(day, -90, SYSDATETIME()), 50.00,  50.00,  100, 0, 0),  -- pass (negative-total); excluded from stale check: IsActive = 0
    (7, 1, DATEADD(day, -5,  SYSDATETIME()), 30.00,  50.00,  100, 0, 1);  -- pass both checks (stale: not old enough)
GO
-- Row 8: Phase 4 step 5 -- the invoice that blocks appointment 17's
-- not_exists join (appointment-completed-no-invoice, LLM-drafted).
INSERT INTO fqb.Invoices_Base
    (InvoiceTransactionID, PatientID, InvoiceDate, UnpaidAmount, TotalAmount, PracticeID, IsDeleted, IsActive, AppointmentID)
VALUES
    (8, 1, DATEADD(day, -40, SYSDATETIME()), 0.00, 100.00, 100, 0, 1, 17);
GO
-- Rows 9-18: Phase 4 step 6 (cdss.calibration) -- one invoice per row-21-30
-- appointment above, InvoiceDate offset by n days from that appointment's
-- fixed ScheduleDate, giving appointment_to_invoice_lag a lag of exactly n
-- days per row (n=1..10).
INSERT INTO fqb.Invoices_Base
    (InvoiceTransactionID, PatientID, InvoiceDate, UnpaidAmount, TotalAmount, PracticeID, IsDeleted, IsActive, AppointmentID)
SELECT 8 + n, 1, DATEADD(day, -100 + n, SYSDATETIME()), 0.00, 100.00, 200, 0, 1, 20 + n
FROM (VALUES (1), (2), (3), (4), (5), (6), (7), (8), (9), (10)) AS numbers(n);
GO

-- dbo.Patient ----------------------------------------------------------------
-- HealthCardExpiryDate/EnrollmentExpiryDate: Phase 4 step 5 additions -- real
-- semantic-catalog-v3.json columns two LLM-drafted checks reference. Left
-- NULL throughout: both of those checks turned out to be structurally
-- broken (a self-referential `dbo.Patient.ProfileID = dbo.Patient.ProfileID`
-- exists clause -- the compiler has no aliasing for a view joined to itself,
-- so every row gets the same population-wide answer, never a genuine
-- per-patient fail/pass split) -- see tests/test_fixture_suite.py.
CREATE TABLE dbo.Patient_Base (
    ProfileID INT NOT NULL PRIMARY KEY,
    FirstName NVARCHAR(50) NULL,
    FamilyName NVARCHAR(50) NULL,
    EnrollmentDate DATETIME2 NULL,
    PracticeID INT NOT NULL,
    IsDeleted BIT NOT NULL,
    IsActive BIT NULL,
    IsTestRecord BIT NULL,
    NHINumber NVARCHAR(20) NULL,
    IsHighCare BIT NULL,
    IsCarePlus BIT NULL,
    HealthCardExpiryDate DATE NULL,
    EnrollmentExpiryDate DATETIME2 NULL
);
GO
CREATE VIEW dbo.Patient AS SELECT * FROM dbo.Patient_Base;
GO
INSERT INTO dbo.Patient_Base
    (ProfileID, FirstName, FamilyName, EnrollmentDate, PracticeID, IsDeleted, IsActive, IsTestRecord, NHINumber, IsHighCare, IsCarePlus)
VALUES
    (1, 'Pat', 'One',   DATEADD(day, -400, SYSDATETIME()), 100, 0, 1,    0, NULL,      1, 0),  -- fail both checks (no recent appt of its own)
    (2, 'Pat', 'Two',   DATEADD(day, -400, SYSDATETIME()), 100, 0, 1,    0, 'ABC123',  0, 1),  -- pass both checks
    (3, 'Pat', 'Three', DATEADD(day, -400, SYSDATETIME()), 100, 0, 1,    1, NULL,      0, 0),  -- pass both (test record / not high-care)
    (4, 'Pat', 'Four',  DATEADD(day, -400, SYSDATETIME()), 100, 0, NULL, 0, NULL,      1, 0),  -- excluded from recall check: IsActive NULL
    (5, 'Pat', 'Five',  NULL,                               100, 0, 1,    0, NULL,      1, 0),  -- fail (missing-nhi); indeterminate (recall, EnrollmentDate NULL)
    (6, 'Pat', 'Six',   DATEADD(day, -400, SYSDATETIME()), 100, 1, 1,    0, NULL,      1, 0);  -- excluded from both: IsDeleted = 1
GO
-- Row 7: Phase 4 step 5 -- a high-care patient with a recent appointment
-- (dbo.Appointments row 19), giving high-risk-patient-no-follow-up /
-- no-recent-appointment-high-needs-patient / no-appointment-overdue-follow-up
-- a genuine pass case (rows 1/3/5 already give them a fail case).
INSERT INTO dbo.Patient_Base
    (ProfileID, FirstName, FamilyName, EnrollmentDate, PracticeID, IsDeleted, IsActive, IsTestRecord, NHINumber, IsHighCare, IsCarePlus)
VALUES
    (7, 'Pat', 'Seven', DATEADD(day, -400, SYSDATETIME()), 100, 0, 1, 0, 'XYZ999', 1, 0);
GO
