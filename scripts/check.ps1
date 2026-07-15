#requires -version 5.1
<#
.SYNOPSIS
    CI gate: lint (ruff) + typecheck (mypy strict) + tests (pytest). Exits non-zero on first failure.
#>
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

Write-Host "==> uv sync" -ForegroundColor Cyan
uv sync --all-groups
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> ruff check" -ForegroundColor Cyan
uv run ruff check src tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> ruff format --check" -ForegroundColor Cyan
uv run ruff format --check src tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> mypy --strict" -ForegroundColor Cyan
uv run mypy
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> pytest" -ForegroundColor Cyan
uv run pytest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> all checks passed" -ForegroundColor Green
exit 0
