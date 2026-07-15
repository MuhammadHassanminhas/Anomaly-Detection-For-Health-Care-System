# CDSS Phase 0 — Environment Report

Generated: 2026-07-15T05:05:32.477254+00:00

## Database

- Database: `INDICI_BI_Full`
- Product version: 15.0.4460.4
- Edition: Developer Edition (64-bit) (engine edition 3)
- `@@VERSION`: Microsoft SQL Server 2019 (RTM-CU32-GDR) (KB5077469) - 15.0.4460.4 (X64) 
	Feb 13 2026 17:00:40 
	Copyright (C) 2019 Microsoft Corporation
	Developer Edition (64-bit) on Windows Server 2019 Standard 10.0 <X64> (Build 17763: )


## Surface

- 2368 visible objects: 320 views, 2048 base tables, 0 other.

## Reconciliation (D-001)

- 54 export names reconciled against the live surface.
  - found_as_view: 54
- 2314 additional objects visible on the surface but not in the export list.

## Row counts + watermark candidates

| Object | Type | Row count | Status | Duration (ms) | Watermark columns |
|---|---|---|---|---|---|
| `dbo.ServiceTemplate` | view | 58 | exact | 24.175 | InsertedAt, UpdatedAt |
| `dbo.Addresses` | view | 118434 | exact | 100.794 | — |
| `dbo.HTIReferral` | view | 1 | exact | 69.507 | InsertedAt, UpdatedAt |
| `dbo.OutStandingBalance` | view | 493 | exact | 210.044 | — |
| `dbo.PatientContact` | view | 31402 | exact | 299.945 | InsertedAt, UpdatedAt |
| `dbo.PendingFinishCounsult` | view | 286 | exact | 1075.426 | — |
| `dbo.MedicineBrand` | view | 9465 | exact | 565.154 | — |
| `dbo.InboxDetail` | view | 1555063 | exact | 15401.524 | InsertedAt |
| `dbo.LabRad` | view | 670 | exact | 4804.976 | — |
| `dbo.Recalls` | view | 97434 | exact | 626.765 | InsertedAt, UpdatedAt |
| `dbo.Patient` | view | 30746 | exact | 190.049 | InsertedAt, UpdatedAt |
| `dbo.Immunisation` | view | 153134 | exact | 2195.431 | InsertedAt, UpdatedAt |
| `dbo.Appointments` | view | 620854 | exact | 7183.355 | InsertedAt, UpdatedAt |
| `dbo.Diagnosis` | view | 5849 | exact | 1598.781 | InsertedAt, UpdatedAt |
| `dbo.Payments` | view | 7562 | exact | 102.226 | InsertedAt, UpdatedAt |
| `dbo.InBox` | view | 395289 | exact | 1661.524 | InsertedAt, UpdatedAt |
| `dbo.HBA1cResult` | view | 0 | exact | 265.311 | InsertedAt |
| `dbo.AppointmentMedications` | view | 955356 | exact | 11678.32 | InsertedAt, UpdatedAt |
| `dbo.Measurements` | view | 11615 | exact | 177.142 | UpdatedAt, InsertedAt |
| `dbo.ImmunisationSchedule` | view | 448035 | exact | 2572.815 | InsertedAt, UpdatedAt |
| `dbo.AppointmentsSlots` | view | 14 | exact | 70.409 | InsertedAt, UpdatedAt |
| `dbo.TransactionHistory` | view | 3369 | exact | 856.998 | InsertedAt |
| `dbo.TimeLine` | view | — | indeterminate | 24420.265 | InsertedAt, UpdatedAt |
| `dbo.PatientAlerts` | view | 383870 | exact | 1576.381 | InsertedAt, UpdatedAt |
| `dbo.MeasurementServiceTemplateDetail` | view | 11825 | exact | 957.928 | InsertedAt, UpdatedAt |
| `dbo.PatientEthnicity` | view | 13568 | exact | 47.241 | — |
| `dbo.Provider` | view | 1141 | exact | 283.906 | InsertedAt, UpdatedAt |
| `dbo.PatientTask` | view | 174898 | exact | 2583.79 | InsertedAt, UpdatedAt |
| `dbo.PatientAccountAge` | view | 0 | exact | 52.44 | — |
| `dbo.LettersAndDocuments` | view | 0 | exact | 14.413 | InsertedAt, UpdatedAt |
| `dbo.PatientContactMethod` | view | 2667 | exact | 56.811 | — |
| `dbo.NESEnrolment` | view | 0 | exact | 7.326 | — |
| `dbo.NextOfKin` | view | 21018 | exact | 131.709 | InsertedAt, UpdatedAt |
| `dbo.Claims` | view | 93511 | exact | 134.746 | InsertedAt, UpdatedAt |
| `dbo.PortalRecievedMessages` | view | 6602 | exact | 192.354 | InsertedAt, UpdatedAt |
| `dbo.QuickConsult` | view | 620894 | exact | 2437.893 | InsertedAt, UpdatedAt |
| `dbo.ACC18` | view | 8 | exact | 66.97 | InsertedAt, UpdatedAt |
| `dbo.PatientPortalMenu` | view | 0 | exact | 21.137 | InsertedAt, UpdatedAt |
| `dbo.Accidents` | view | 37 | exact | 67.562 | InsertedAt, UpdatedAt |
| `dbo.Company` | view | 5988 | exact | 19.805 | — |
| `dbo.Roster` | view | 430727 | exact | 9860.018 | InsertedAt, UpdatedAt |
| `dbo.OutBox` | view | 285451 | exact | 3538.579 | InsertedAt, UpdatedAt |
| `dbo.Allergies` | view | 4177 | exact | 138.334 | InsertedAt, UpdatedAt |
| `dbo.CarePlan` | view | 167 | exact | 41.264 | InsertedAt, UpdatedAt |
| `dbo.Refund` | view | 594 | exact | 65.823 | InsertedAt, UpdatedAt |
| `dbo.ACCConsumableDetail` | view | 0 | exact | 36.957 | InsertedAt, UpdatedAt |
| `dbo.ReferralPatient` | view | 745 | exact | 68.17 | UpdatedAt |
| `dbo.AgedBalanceSummaryReportMonthly` | view | 572 | exact | 12.067 | InsertedAt, UpdatedAt |
| `dbo.TriageTemplate` | view | 0 | exact | 23.767 | InsertedAt, UpdatedAt |
| `dbo.AppointmentMaternity` | view | 13 | exact | 37.142 | InsertedAt, UpdatedAt |
| `dbo.NewAppointmentServices` | view | 109530 | exact | 203.291 | InsertedAt, UpdatedAt |
| `dbo.InvoiceDetail` | view | 3777 | exact | 83.995 | InsertedAt, UpdatedAt |
| `dbo.Invoices` | view | 7118 | exact | 92.479 | InsertedAt, UpdatedAt |
| `AIFinanceAssistant.VU_PracticeTotalExpenses` | view | 16 | exact | 40.234 | — |

- 11 objects have no `InsertedAt`/`UpdatedAt` watermark candidate.
  - `dbo.Addresses`
  - `dbo.OutStandingBalance`
  - `dbo.PendingFinishCounsult`
  - `dbo.MedicineBrand`
  - `dbo.LabRad`
  - `dbo.PatientEthnicity`
  - `dbo.PatientAccountAge`
  - `dbo.PatientContactMethod`
  - `dbo.NESEnrolment`
  - `dbo.Company`
  - `AIFinanceAssistant.VU_PracticeTotalExpenses`
