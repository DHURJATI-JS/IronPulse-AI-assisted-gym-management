# Shared helpers for the IronPulse Windows scripts (Windows PowerShell 5.1 and PowerShell 7).
# Dot-source it:   . (Join-Path $PSScriptRoot "common.ps1")
# Native programs do not throw on failure in PowerShell 5.1, so every call checks $LASTEXITCODE explicitly.

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Info {
    param([string]$Message)
    Write-Host "    $Message"
}

function Write-Ok {
    param([string]$Message)
    Write-Host "    [ok] $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "    [!] $Message" -ForegroundColor Yellow
}

function Test-Command {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Update-SessionPath {
    # pick up PATH changes made by installers without opening a new window
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Invoke-Native {
    # Run a program, show its output, throw a friendly error when it fails.
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$What = ""
    )
    if (-not $What) { $What = "$FilePath $($Arguments -join ' ')" }
    if (-not (Get-Command $FilePath -ErrorAction SilentlyContinue)) {
        throw "'$What' failed: $FilePath was not found."
    }
    # stderr output (progress bars, warnings) must not turn into terminating errors: judge by the exit code only
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $FilePath @Arguments | Out-Host
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) {
        throw "'$What' failed (exit code $code)."
    }
}

function Invoke-NativeQuiet {
    # Run a program silently and return its exit code (never throws).
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $FilePath @Arguments *> $null
        return $LASTEXITCODE
    } catch {
        return 1
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Get-NativeOutput {
    # Run a program and return its standard output as one trimmed string ("" on failure).
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $lines = & $FilePath @Arguments 2> $null
        if ($LASTEXITCODE -ne 0) { return "" }
        return (($lines | ForEach-Object { "$_" }) -join "`n").Trim()
    } catch {
        return ""
    } finally {
        $ErrorActionPreference = $previous
    }
}

function New-RandomSecret {
    param([int]$Length = 32)
    $chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789".ToCharArray()
    $bytes = New-Object byte[] $Length
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $builder = New-Object System.Text.StringBuilder
    foreach ($b in $bytes) {
        [void]$builder.Append($chars[$b % $chars.Length])
    }
    return $builder.ToString()
}

function Test-WingetPackage {
    param([string]$Id)
    if (-not (Test-Command "winget")) { return $false }
    $code = Invoke-NativeQuiet "winget" @("list", "--id", $Id, "-e", "--accept-source-agreements")
    return ($code -eq 0)
}

function Install-WingetPackage {
    # Install a winget package unless it is already installed. Throws a friendly error on failure.
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$Name,
        [string]$Override = ""
    )
    if (-not (Test-Command "winget")) {
        throw "winget is not available. Install 'App Installer' from the Microsoft Store (Windows 10/11), then run this script again - or install $Name manually."
    }
    if (Test-WingetPackage $Id) {
        Write-Ok "$Name is already installed"
        return
    }
    Write-Info "Installing $Name with winget ($Id) - this can take a few minutes ..."
    $wingetArgs = @("install", "--id", $Id, "-e", "--source", "winget", "--accept-package-agreements", "--accept-source-agreements", "--silent")
    if ($Override) {
        $wingetArgs += @("--override", $Override)
    }
    & winget @wingetArgs | Out-Host
    $code = $LASTEXITCODE
    # 0 = installed; 3010 = installed, restart required; -1978335189 / -1978335135 = already installed / no newer version
    $okCodes = @(0, 3010, -1978335189, -1978335135)
    if ($okCodes -notcontains $code) {
        throw "winget could not install $Name (exit code $code). Install it manually and run the script again."
    }
    if ($code -eq 3010) {
        Write-Warn "$Name asks for a restart - restart Windows if something does not work yet."
    }
    Update-SessionPath
}

function Get-ComposeArgs {
    # docker compose arguments with the project's .env and compose file
    param([Parameter(Mandatory = $true)][string]$Root)
    return @("compose", "--env-file", (Join-Path $Root ".env"), "-f", (Join-Path $Root "docker\compose.yml"))
}

function Test-DockerRunning {
    if (-not (Test-Command "docker")) { return $false }
    return ((Invoke-NativeQuiet "docker" @("info")) -eq 0)
}

function Start-DockerDesktop {
    # Start Docker Desktop if needed and wait until the engine answers (up to $TimeoutMinutes).
    param([int]$TimeoutMinutes = 5)
    if (Test-DockerRunning) { return }
    $exe = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (Test-Path $exe) {
        Write-Info "Starting Docker Desktop (the first start can take a few minutes) ..."
        Start-Process -FilePath $exe | Out-Null
    }
    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        Update-SessionPath
        if (Test-DockerRunning) {
            Write-Ok "Docker is running"
            return
        }
    }
    throw "Docker did not start within $TimeoutMinutes minutes. Open Docker Desktop once (accept the terms; WSL 2 may need a Windows restart), then run the script again."
}
