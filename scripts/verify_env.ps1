#requires -version 5.1
<#
.SYNOPSIS
    Phase 0 step 8. Wraps `python -m cdss.verify_env`: live connection smoke
    test (step 4), surface enumeration (step 5), D-001 reconciliation
    (step 6), row-count/watermark capture (step 7), writing
    artifacts/env-report.json and artifacts/env-report.md. Idempotent —
    re-running overwrites both artifacts.
#>
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

uv run python -m cdss.verify_env
exit $LASTEXITCODE
