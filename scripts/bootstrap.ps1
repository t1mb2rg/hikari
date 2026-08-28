$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvRoot = Join-Path $RepoRoot ".venv"
$Python = Join-Path $VenvRoot "Scripts\python.exe"
$Activate = Join-Path $VenvRoot "Scripts\Activate.ps1"
$EnvExample = Join-Path $RepoRoot ".env.example"
$EnvFile = Join-Path $RepoRoot ".env"

Write-Host "Hikari environment bootstrap: $RepoRoot"

if (-not (Test-Path $Python)) {
    Write-Host "Creating repo-local .venv ..."

    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv $VenvRoot
    }
    elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv $VenvRoot
    }
    else {
        throw "Python 3 launcher was not found on PATH."
    }

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create Hikari .venv."
    }
}

if (-not (Test-Path $Python)) {
    throw "Hikari .venv does not contain python.exe: $Python"
}

Push-Location $RepoRoot
try {
    & $Python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upgrade pip in Hikari .venv."
    }

    & $Python -m pip install -e ".[dev,windows-notify]"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install Hikari into repo-local .venv."
    }

    if ((-not (Test-Path $EnvFile)) -and (Test-Path $EnvExample)) {
        Copy-Item $EnvExample $EnvFile
        Write-Host "Created .env from .env.example. Fill in HIKARI_MODEL_API_KEY locally."
    }
}
finally {
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
