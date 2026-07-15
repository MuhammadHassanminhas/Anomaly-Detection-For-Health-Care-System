# CDSS Phase 0 — Environment Report

Generated: 2026-07-15T05:43:37.927050+00:00

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
| `dbo.ServiceTemplate` | view | 58 | exact | 239.879 | InsertedAt, UpdatedAt |
| `dbo.Addresses` | view | 118434 | exact | 92.521 | — |
| `dbo.HTIReferral` | view | 1 | exact | 43.517 | InsertedAt, UpdatedAt |
| `dbo.OutStandingBalance` | view | 493 | exact | 165.645 | — |
| `dbo.PatientContact` | view | 31402 | exact | 108.053 | InsertedAt, UpdatedAt |
| `dbo.PendingFinishCounsult` | view | 286 | exact | 34.203 | — |
| `dbo.MedicineBrand` | view | 9465 | exact | 71.305 | — |
| `dbo.InboxDetail` | view | 1555063 | exact | 3711.703 | InsertedAt |
| `dbo.LabRad` | view | 670 | exact | 309.913 | — |
| `dbo.Recalls` | view | 97434 | exact | 98.204 | InsertedAt, UpdatedAt |
| `dbo.Patient` | view | 30746 | exact | 188.278 | InsertedAt, UpdatedAt |
| `dbo.Immunisation` | view | 153134 | exact | 306.676 | InsertedAt, UpdatedAt |
| `dbo.Appointments` | view | 620854 | exact | 224.884 | InsertedAt, UpdatedAt |
| `dbo.Diagnosis` | view | 5849 | exact | 647.693 | InsertedAt, UpdatedAt |
| `dbo.Payments` | view | 7562 | exact | 83.869 | InsertedAt, UpdatedAt |
| `dbo.InBox` | view | 395289 | exact | 558.866 | InsertedAt, UpdatedAt |
| `dbo.HBA1cResult` | view | 0 | exact | 269.02 | InsertedAt |
| `dbo.AppointmentMedications` | view | 955356 | exact | 8507.463 | InsertedAt, UpdatedAt |
| `dbo.Measurements` | view | 11615 | exact | 43.122 | UpdatedAt, InsertedAt |
| `dbo.ImmunisationSchedule` | view | 448035 | exact | 524.763 | InsertedAt, UpdatedAt |
| `dbo.AppointmentsSlots` | view | 14 | exact | 20.561 | InsertedAt, UpdatedAt |
| `dbo.TransactionHistory` | view | 3369 | exact | 589.539 | InsertedAt |
| `dbo.TimeLine` | view | 2469619 | exact | 5500.097 | InsertedAt, UpdatedAt |
| `dbo.PatientAlerts` | view | 383870 | exact | 212.788 | InsertedAt, UpdatedAt |
| `dbo.MeasurementServiceTemplateDetail` | view | 11825 | exact | 797.879 | InsertedAt, UpdatedAt |
| `dbo.PatientEthnicity` | view | 13568 | exact | 34.568 | — |
| `dbo.Provider` | view | 1141 | exact | 230.156 | InsertedAt, UpdatedAt |
| `dbo.PatientTask` | view | 174898 | exact | 401.032 | InsertedAt, UpdatedAt |
| `dbo.PatientAccountAge` | view | 0 | exact | 10.565 | — |
| `dbo.LettersAndDocuments` | view | 0 | exact | 14.328 | InsertedAt, UpdatedAt |
| `dbo.PatientContactMethod` | view | 2667 | exact | 24.147 | — |
| `dbo.NESEnrolment` | view | 0 | exact | 6.732 | — |
| `dbo.NextOfKin` | view | 21018 | exact | 130.71 | InsertedAt, UpdatedAt |
| `dbo.Claims` | view | 93511 | exact | 117.928 | InsertedAt, UpdatedAt |
| `dbo.PortalRecievedMessages` | view | 6602 | exact | 68.976 | InsertedAt, UpdatedAt |
| `dbo.QuickConsult` | view | 620894 | exact | 242.42 | InsertedAt, UpdatedAt |
| `dbo.ACC18` | view | 8 | exact | 16.91 | InsertedAt, UpdatedAt |
| `dbo.PatientPortalMenu` | view | 0 | exact | 12.452 | InsertedAt, UpdatedAt |
| `dbo.Accidents` | view | 37 | exact | 30.066 | InsertedAt, UpdatedAt |
| `dbo.Company` | view | 5988 | exact | 8.44 | — |
| `dbo.Roster` | view | 430727 | exact | 372.897 | InsertedAt, UpdatedAt |
| `dbo.OutBox` | view | 285451 | exact | 1616.971 | InsertedAt, UpdatedAt |
| `dbo.Allergies` | view | 4177 | exact | 27.727 | InsertedAt, UpdatedAt |
| `dbo.CarePlan` | view | 167 | exact | 16.913 | InsertedAt, UpdatedAt |
| `dbo.Refund` | view | 594 | exact | 47.929 | InsertedAt, UpdatedAt |
| `dbo.ACCConsumableDetail` | view | 0 | exact | 14.048 | InsertedAt, UpdatedAt |
| `dbo.ReferralPatient` | view | 745 | exact | 21.975 | UpdatedAt |
| `dbo.AgedBalanceSummaryReportMonthly` | view | 572 | exact | 13.877 | InsertedAt, UpdatedAt |
| `dbo.TriageTemplate` | view | 0 | exact | 21.567 | InsertedAt, UpdatedAt |
| `dbo.AppointmentMaternity` | view | 13 | exact | 27.486 | InsertedAt, UpdatedAt |
| `dbo.NewAppointmentServices` | view | 109530 | exact | 158.433 | InsertedAt, UpdatedAt |
| `dbo.InvoiceDetail` | view | 3777 | exact | 85.687 | InsertedAt, UpdatedAt |
| `dbo.Invoices` | view | 7118 | exact | 85.613 | InsertedAt, UpdatedAt |
| `AIFinanceAssistant.VU_PracticeTotalExpenses` | view | 16 | exact | 6.956 | — |

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
