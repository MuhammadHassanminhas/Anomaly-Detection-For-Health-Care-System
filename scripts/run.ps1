#requires -version 5.1
<#
.SYNOPSIS
    Phase 3 step 8. Wraps `python -m cdss.run`: loads every active/enabled
    check, executes each (with preflight schema-drift detection) against the
    real source DB, materializes findings, surfaces pervasive indeterminacy,
    and writes artifacts/runs/run-<id>-report.md. Idempotent by design at the
    materialization layer (same run id, same rows -> zero new writes); a
    second full invocation's cross-run idempotency depends on watermark
    advancement, which only applies to views with a real watermark column.
#>
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

uv run python -m cdss.run
exit $LASTEXITCODE
