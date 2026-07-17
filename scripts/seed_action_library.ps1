#requires -version 5.1
<#
.SYNOPSIS
    Phase 4 step 1. Wraps `python -m cdss.action_library`: idempotent upsert
    of the curated action_library rows into the app DB (CDSS_APP_DB_URL).
    Safe to re-run on every deploy.
#>
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

uv run python -m cdss.action_library
exit $LASTEXITCODE
