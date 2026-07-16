#requires -version 5.1
<#
.SYNOPSIS
    Phase 1 step 8. Wraps `python -m cdss.profile`: runs steps 2-7
    end-to-end for every in-scope view, then step 4's cross-view pair
    analysis, writing artifacts/catalog/semantic-catalog-v<N>.json
    (schema-validated) + artifacts/catalog/profiling-report.md. Idempotent
    -- each successful run bumps to a new catalog version. Interim failures
    resume cleanly via per-view checkpointing (artifacts/catalog/
    .profile-checkpoint.json) -- no manual steps.
#>
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

uv run python -m cdss.profile
exit $LASTEXITCODE
