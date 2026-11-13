<#
.SYNOPSIS
Start IronPulse on Windows (after setup.ps1).

.DESCRIPTION
Activates the virtual environment, makes sure the Docker database and Redis are running (Docker setup mode), starts
the local AI fallback (Gemma in Ollama, in the background - never delays the start; -NoAiDocker skips it) and starts
the app with python -m app (host and port from .env). -Workers N runs several uvicorn worker processes. -Lan serves
your phone and other devices on this network over HTTPS (scripts\lan.py: LAN address, <name>.local, port from .env;
install var\tls\ironpulse-lan-ca.crt on the phone once). -DryRun prints what would be started and run.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File run.ps1
.EXAMPLE
powershell -ExecutionPolicy Bypass -File run.ps1 -Reload
.EXAMPLE
powershell -ExecutionPolicy Bypass -File run.ps1 -Workers 4
.EXAMPLE
powershell -ExecutionPolicy Bypass -File run.ps1 -Lan
.EXAMPLE
powershell -ExecutionPolicy Bypass -File run.ps1 -BindHost 0.0.0.0 -Port 10000
#>
[CmdletBinding()]
param(
    [switch]$Reload,
    [int]$Workers = 1,
    [string]$BindHost = "",
    [int]$Port = 0,
    [switch]$NoServices,
    [switch]$Lan,
    [switch]$NoAiDocker,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location -LiteralPath $Root
. (Join-Path $Root "scripts\windows\common.ps1")

$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
$ManagePy = Join-Path $Root "scripts\manage.py"
$LanPy = Join-Path $Root "scripts\lan.py"

try {
    if (-not (Test-Path $VenvPy)) {
        throw "No virtual environment yet - run the setup first:  powershell -ExecutionPolicy Bypass -File setup.ps1"
    }
    if (-not (Test-Path (Join-Path $Root ".env"))) {
        throw "No .env yet - run setup.ps1 first (or: .venv\Scripts\python scripts\make_env.py)."
    }
    if ($Workers -lt 1) { throw "-Workers must be 1 or more." }
    if ($Reload -and ($Workers -gt 1)) { throw "-Reload cannot be combined with -Workers." }
    if ($Lan -and $Reload) { throw "-Lan cannot be combined with -Reload (LAN mode never auto-reloads)." }
    if ($Lan -and $BindHost) { throw "-Lan binds every interface itself (0.0.0.0) - leave out -BindHost." }

    $activate = Join-Path $Root ".venv\Scripts\Activate.ps1"
    if (Test-Path $activate) { . $activate }

    if (-not $NoServices) {
        $mode = Get-NativeOutput $VenvPy @($ManagePy, "env", "get", "IRONPULSE_DB_MODE")
        if ($mode -eq "docker") {
            if ($DryRun) {
                Write-Info "would run: docker compose up -d db redis"
            } elseif (Test-Command "docker") {
                Start-DockerDesktop -TimeoutMinutes 3
                Invoke-Native "docker" ((Get-ComposeArgs -Root $Root) + @("up", "-d", "db", "redis")) "start the database and Redis containers"
            } else {
                Write-Warn "Docker is not installed, but this setup keeps the database in Docker - run setup.ps1 again."
            }
        }
    }

    # the local AI fallback: decided in Python (scripts\ai_fallback.py), slow work in the background, one line out
    if (-not $NoAiDocker) {
        $fallbackArgs = @($ManagePy, "ai-fallback")
        if ($DryRun) { $fallbackArgs += "--dry-run" }
        & $VenvPy @fallbackArgs
    }

    if ($Lan) {
        $lanArgs = @($LanPy, "--workers", "$Workers")
        if ($Port -gt 0) { $lanArgs += @("--port", "$Port") }
        if ($DryRun) { $lanArgs += "--dry-run" }
        & $VenvPy @lanArgs
        exit $LASTEXITCODE
    }

    if ($BindHost) { $env:HOST = $BindHost }
    if ($Port -gt 0) { $env:PORT = "$Port" }

    if ($Workers -gt 1) {
        if ($DryRun) { Write-Info "would run: python scripts\manage.py run --workers $Workers"; exit 0 }
        & $VenvPy $ManagePy run --workers $Workers
    } else {
        if ($Reload) { $env:RELOAD = "true" }
        if ($DryRun) { Write-Info "would run: python -m app"; exit 0 }
        & $VenvPy -m app
    }
    exit $LASTEXITCODE
} catch {
    Write-Host "IronPulse could not start: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
