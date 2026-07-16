#requires -version 5.1
<#
.SYNOPSIS
    Provisions (or tears down) the disposable SQL Server fixture used by
    tests/execution (Phase 2 step 5). Synthetic data only -- see
    scripts/fixture_db_setup.sql. Always fully recreates: drop, apply
    schema+data, exit 0 -- idempotent, safe to re-run.

.NOTES
    Uses SQL Server Express LocalDB, not Docker (D-009 amendment, 2026-07-16):
    this dev machine has no virtualization access, so the spec's Docker
    mssql/server:2022 option is unusable here; LocalDB is the spec's own
    named fallback. Requires the "MSSQLLocalDB" instance (ships with SQL
    Server Express / the SSDT tools) and sqlcmd on PATH.
#>
param(
    [switch]$Recreate,
    [switch]$Down
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$Instance = "(localdb)\MSSQLLocalDB"
$DatabaseName = "cdss_fixture"

Write-Host "==> sqllocaldb start MSSQLLocalDB" -ForegroundColor Cyan
sqllocaldb start MSSQLLocalDB
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($Down) {
    Write-Host "==> dropping $DatabaseName (-Down)" -ForegroundColor Cyan
    $dropSql = "IF DB_ID('$DatabaseName') IS NOT NULL BEGIN ALTER DATABASE $DatabaseName SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE $DatabaseName; END"
    sqlcmd -S $Instance -Q $dropSql -b
    exit $LASTEXITCODE
}

Write-Host "==> applying scripts/fixture_db_setup.sql" -ForegroundColor Cyan
sqlcmd -S $Instance -i (Join-Path $PSScriptRoot "fixture_db_setup.sql") -b
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> fixture DB ready: $Instance / database $DatabaseName" -ForegroundColor Green
