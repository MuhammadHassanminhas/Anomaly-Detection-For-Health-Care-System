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
    Provider NVARCHAR(50) NULL
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
CREATE TABLE fqb.Invoices_Base (
    InvoiceTransactionID INT NOT NULL PRIMARY KEY,
    PatientID INT NOT NULL,
    InvoiceDate DATETIME2 NULL,
    UnpaidAmount DECIMAL(18, 2) NULL,
    TotalAmount DECIMAL(18, 2) NULL,
    PracticeID INT NOT NULL,
    IsDeleted BIT NOT NULL,
    IsActive BIT NOT NULL
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

-- dbo.Patient ----------------------------------------------------------------
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
    IsCarePlus BIT NULL
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
