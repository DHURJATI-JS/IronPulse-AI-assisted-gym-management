<#
.SYNOPSIS
Build and install pgvector into a native Windows PostgreSQL (needs an Administrator PowerShell).

.DESCRIPTION
Installs Visual Studio 2022 Build Tools (C++ workload) with winget when missing, downloads the pgvector source,
builds it with nmake inside the x64 developer environment (vcvars64.bat, located with vswhere) and installs it
into PostgreSQL. setup.ps1 -DbMode native runs it for you. The Docker setup (-DbMode docker) does not need it.

.PARAMETER PgRoot
PostgreSQL installation folder (default: C:\Program Files\PostgreSQL\17).

.PARAMETER Version
pgvector release tag without the leading "v" (default 0.8.1).

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\windows\install_pgvector.ps1
.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\windows\install_pgvector.ps1 -PgRoot "C:\Program Files\PostgreSQL\16"
#>
[CmdletBinding()]
param(
    [string]$PgRoot = "",
    [string]$Version = "0.8.1"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
. (Join-Path $PSScriptRoot "common.ps1")

function Find-VcVars {
    $vswhere = ""
    if (${env:ProgramFiles(x86)}) {
        $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    }
    if ((-not $vswhere) -or (-not (Test-Path $vswhere))) { return "" }
    $path = Get-NativeOutput $vswhere @("-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath")
    if (-not $path) { return "" }
    $bat = Join-Path $path "VC\Auxiliary\Build\vcvars64.bat"
    if (Test-Path $bat) { return $bat }
    return ""
}

try {
    if (-not $PgRoot) { $PgRoot = Join-Path $env:ProgramFiles "PostgreSQL\17" }
    if (-not (Test-Path (Join-Path $PgRoot "bin\pg_config.exe"))) {
        throw "PostgreSQL was not found in $PgRoot - pass the right folder with -PgRoot."
    }
    if (Test-Path (Join-Path $PgRoot "share\extension\vector.control")) {
        Write-Ok "pgvector is already installed in $PgRoot"
        exit 0
    }
    if (-not (Test-Admin)) {
        throw "Run this script in an Administrator PowerShell (it copies files into $PgRoot)."
    }

    Write-Step "Visual Studio 2022 Build Tools (C++)"
    $vcvars = Find-VcVars
    if (-not $vcvars) {
        Write-Info "Installing the C++ build tools (several GB, 10-30 minutes) ..."
        Install-WingetPackage -Id "Microsoft.VisualStudio.2022.BuildTools" -Name "Visual Studio 2022 Build Tools" -Override "--quiet --wait --norestart --nocache --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
        $vcvars = Find-VcVars
    }
    if (-not $vcvars) {
        throw "vcvars64.bat was not found. Open 'Visual Studio Installer', add the 'Desktop development with C++' workload, then run this script again."
    }
    Write-Ok $vcvars

    Write-Step "Downloading pgvector $Version"
    $work = Join-Path $env:TEMP "ironpulse-pgvector-$Version"
    if (Test-Path $work) { Remove-Item -Recurse -Force $work }
    New-Item -ItemType Directory -Path $work | Out-Null
    $zip = Join-Path $work "pgvector.zip"
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri "https://github.com/pgvector/pgvector/archive/refs/tags/v$Version.zip" -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $work -Force
    $src = Join-Path $work "pgvector-$Version"
    if (-not (Test-Path (Join-Path $src "Makefile.win"))) {
        throw "Makefile.win was not found in the downloaded source ($src)."
    }

    Write-Step "Building and installing pgvector (nmake)"
    $buildCmd = Join-Path $work "build-pgvector.cmd"
    $lines = @(
        "@echo off",
        "call `"$vcvars`"",
        "where nmake >nul 2>nul || (echo nmake was not found after vcvars64.bat & exit /b 1)",
        "set `"PGROOT=$PgRoot`"",
        "cd /d `"$src`" || exit /b 1",
        "nmake /NOLOGO /F Makefile.win || exit /b 1",
        "nmake /NOLOGO /F Makefile.win install || exit /b 1"
    )
    Set-Content -Path $buildCmd -Value $lines -Encoding Ascii
    Invoke-Native "cmd.exe" @("/d", "/c", $buildCmd) "build pgvector"

    if (-not (Test-Path (Join-Path $PgRoot "share\extension\vector.control"))) {
        throw "the build finished but vector.control is not in $PgRoot\share\extension"
    }
    Write-Ok "pgvector $Version installed into $PgRoot - run: .venv\Scripts\python scripts\manage.py init-db"
    exit 0
} catch {
    Write-Host ""
    Write-Host "pgvector installation failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Alternative without compiling: use Docker (setup.ps1 -DbMode docker)." -ForegroundColor Yellow
    exit 1
}
