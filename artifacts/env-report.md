# CDSS Phase 0 — Environment Report

Generated: 2026-07-16T09:59:07.119639+00:00

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

- 4 export names reconciled against the live surface.
  - found_as_view: 4
- 2364 additional objects visible on the surface but not in the export list.

## Row counts + watermark candidates

| Object | Type | Row count | Status | Duration (ms) | Watermark columns |
|---|---|---|---|---|---|
| `dbo.Disease` | view | 270481 | exact | 52.696 | — |
| `dbo.Patient` | view | 30746 | exact | 153.67 | InsertedAt, UpdatedAt |
| `fqb.Invoices` | view | 16442 | exact | 103.695 | InsertedAt, UpdatedAt |
| `dbo.Appointments` | view | 620854 | exact | 228.463 | InsertedAt, UpdatedAt |

- 1 objects have no `InsertedAt`/`UpdatedAt` watermark candidate.
  - `dbo.Disease`
