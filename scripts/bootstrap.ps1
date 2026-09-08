$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvRoot = Join-Path $RepoRoot ".venv"
$Python = Join-Path $VenvRoot "Scripts\python.exe"
$Activate = Join-Path $VenvRoot "Scripts\Activate.ps1"
$EnvExample = Join-Path $RepoRoot ".env.example"
$EnvFile = Join-Path $RepoRoot ".env"

Write-Host "Hikari environment bootstrap: $RepoRoot"

$UvCommand = Get-Command uv -ErrorAction SilentlyContinue
if (-not $UvCommand) {
    throw "uv was not found on PATH. Install uv before bootstrapping Hikari."
}

Push-Location $RepoRoot
$PreviousProjectEnvironment = $env:UV_PROJECT_ENVIRONMENT
try {
    $env:UV_PROJECT_ENVIRONMENT = $VenvRoot
    & $UvCommand.Source sync --locked --extra dev --extra windows-notify
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to synchronize Hikari .venv from uv.lock."
    }
    if (-not (Test-Path $Python)) {
        throw "Hikari .venv does not contain python.exe after uv sync: $Python"
    }

    if ((-not (Test-Path $EnvFile)) -and (Test-Path $EnvExample)) {
        Copy-Item $EnvExample $EnvFile
        Write-Host "Created .env from .env.example. Fill in HIKARI_MODEL_API_KEY locally."
    }
}
finally {
    $env:UV_PROJECT_ENVIRONMENT = $PreviousProjectEnvironment
    Pop-Location
}

Write-Host ""
Write-Host "Hikari environment is ready."
Write-Host "Python: $Python"
Write-Host "Activate: $Activate"
Write-Host "Env: $EnvFile"
Write-Host ""
Write-Host "Next:"
Write-Host "  & `"$Activate`""
Write-Host "  hikari-resident doctor --env-file `"$EnvFile`""
