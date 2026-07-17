#requires -version 5.1
<#
.SYNOPSIS
    Phase 4 step 4. Wraps `python -m cdss.authoring.llm_draft`: drafts
    workflow/care-gap checks from the latest semantic-catalog-vN.json via
    OpenAI (D-004), validates each against the DSL schema + semantic catalog
    (F2), and persists survivors as checks(source='llm', status='draft').
    Idempotent by slug -- safe to re-run.
#>
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

uv run python -m cdss.authoring.llm_draft
exit $LASTEXITCODE
