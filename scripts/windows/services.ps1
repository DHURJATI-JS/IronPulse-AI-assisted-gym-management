<#
.SYNOPSIS
Start, stop or inspect the IronPulse Docker services on Windows (PostgreSQL, Redis, Gemma failsafe, Kokoro voice).

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\windows\services.ps1 start
.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\windows\services.ps1 start -Ai -Voice
.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\windows\services.ps1 start -Ai -NoDatabase
.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\windows\services.ps1 status
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "status", "logs", "down")]
    [string]$Action = "status",
    [switch]$Ai,
    [switch]$Voice,
    [switch]$NoDatabase
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$compose = Get-ComposeArgs -Root $Root

try {
    if (-not (Test-Command "docker")) {
        throw "Docker is not installed - run setup.ps1 (it installs Docker Desktop) or install it from https://www.docker.com/"
    }
    if (-not (Test-Path (Join-Path $Root ".env"))) {
        throw "No .env yet - run setup.ps1 first."
    }
    if ($Action -eq "start") {
        Start-DockerDesktop -TimeoutMinutes 5
        $services = @()
        if (-not $NoDatabase) { $services += @("db", "redis") }
        if ($Ai) { $services += @("ollama", "ollama-pull") }
        if ($services.Count -gt 0) {
            Invoke-Native "docker" ($compose + @("up", "-d") + $services) "docker compose up"
        }
        if ($Voice) {
            Invoke-Native "docker" ($compose + @("--profile", "voice", "up", "-d", "kokoro")) "start Kokoro"
        }
    } elseif ($Action -eq "stop") {
        Invoke-Native "docker" ($compose + @("--profile", "voice", "stop")) "docker compose stop"
    } elseif ($Action -eq "status") {
        Invoke-Native "docker" ($compose + @("--profile", "voice", "ps")) "docker compose ps"
    } elseif ($Action -eq "logs") {
        Invoke-Native "docker" ($compose + @("--profile", "voice", "logs", "--tail", "100", "-f")) "docker compose logs"
    } elseif ($Action -eq "down") {
        Invoke-Native "docker" ($compose + @("--profile", "voice", "down")) "docker compose down"
        Write-Info "Containers removed; the data volumes are kept (docker compose ... down -v would delete them)."
    }
    exit 0
} catch {
    Write-Host "services.ps1: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
