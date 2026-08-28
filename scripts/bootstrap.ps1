$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvRoot = Join-Path $RepoRoot ".venv"
$Python = Join-Path $VenvRoot "Scripts\python.exe"
$Activate = Join-Path $VenvRoot "Scripts\Activate.ps1"
$EnvExample = Join-Path $RepoRoot ".env.example"
$EnvFile = Join-Path $RepoRoot ".env"

Write-Host "Hikari 环境初始化：$RepoRoot"

if (-not (Test-Path $Python)) {
    Write-Host "创建 repo-local .venv ..."
    py -3 -m venv $VenvRoot
}

if (-not (Test-Path $Python)) {
    throw "未能创建 Hikari .venv：$Python"
}

Push-Location $RepoRoot
try {
    & $Python -m pip install --upgrade pip
    & $Python -m pip install -e ".[dev,windows-notify]"

    if ((-not (Test-Path $EnvFile)) -and (Test-Path $EnvExample)) {
        Copy-Item $EnvExample $EnvFile
        Write-Host "已创建 .env，请编辑其中的 HIKARI_MODEL_API_KEY。"
    }
}
finally {
    Pop-Location
}

Write-Host ""
Write-Host "Hikari 环境已就绪。"
Write-Host "Python: $Python"
Write-Host "Activate: $Activate"
Write-Host "Env: $EnvFile"
Write-Host ""
Write-Host "下一步："
Write-Host "  & `"$Activate`""
Write-Host "  hikari-resident doctor --env-file `"$EnvFile`""
