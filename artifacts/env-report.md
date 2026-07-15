# CDSS Phase 0 — Environment Report

Generated: 2026-07-15T11:41:20.635648+00:00

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

- 10 export names reconciled against the live surface.
  - found_as_view: 10
- 2358 additional objects visible on the surface but not in the export list.

## Row counts + watermark candidates

| Object | Type | Row count | Status | Duration (ms) | Watermark columns |
|---|---|---|---|---|---|
| `dbo.AppointmentMedications` | view | 955356 | exact | 395.43 | InsertedAt, UpdatedAt |
| `dbo.Disease` | view | 270481 | exact | 61.005 | — |
| `dbo.Patient` | view | 30746 | exact | 119.858 | InsertedAt, UpdatedAt |
| `fqb.Allergies` | view | 4177 | exact | 12.478 | InsertedAt, UpdatedAt |
| `fqb.Diagnosis` | view | 5849 | exact | 642.219 | InsertedAt, UpdatedAt |
| `dbo.Immunisation` | view | 153134 | exact | 284.219 | InsertedAt, UpdatedAt |
| `OLAP.Medicine` | view | 65501 | exact | 11.819 | — |
| `dbo.PatientAlerts` | view | 383870 | exact | 186.121 | InsertedAt, UpdatedAt |
| `fqb.Invoices` | view | 16442 | exact | 74.777 | InsertedAt, UpdatedAt |
| `dbo.Appointments` | view | 620854 | exact | 188.975 | InsertedAt, UpdatedAt |

- 2 objects have no `InsertedAt`/`UpdatedAt` watermark candidate.
  - `dbo.Disease`
  - `OLAP.Medicine`
