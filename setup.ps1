<#
.SYNOPSIS
IronPulse setup for Windows 10/11 (Windows PowerShell 5.1 or PowerShell 7). Safe to run again.

.DESCRIPTION
Installs what is missing with winget (Python, Tesseract OCR, Docker Desktop or PostgreSQL 17 + pgvector),
creates the virtual environment, writes .env with fresh secrets, starts PostgreSQL + Redis (Docker), the
Gemma failsafe (Ollama in Docker), initialises the database, builds the assets, generates the videos and loads
the demo data. Every step is skipped when it is already done.

.PARAMETER DbMode
docker (default): PostgreSQL 17 + pgvector and Redis in Docker Desktop.
native: PostgreSQL 17 installed on Windows + pgvector built with Visual Studio 2022 Build Tools (needs Administrator).
existing: your own PostgreSQL server (-DatabaseUrl).

.EXAMPLE
powershell -ExecutionPolicy Bypass -File setup.ps1
.EXAMPLE
powershell -ExecutionPolicy Bypass -File setup.ps1 -DbMode native -SkipVideos
.EXAMPLE
powershell -ExecutionPolicy Bypass -File setup.ps1 -DbMode existing -DatabaseUrl "postgresql://user:pass@host:5432/ironpulse"
.EXAMPLE
powershell -ExecutionPolicy Bypass -File setup.ps1 -NoAiDocker -WithVoice
#>
[CmdletBinding()]
param(
    [ValidateSet("docker", "native", "existing")]
    [string]$DbMode = "docker",
    [switch]$WithAiDocker,
    [switch]$NoAiDocker,
    [switch]$WithVoice,
    [switch]$SkipAssets,
    [switch]$SkipVideos,
    [switch]$SkipSeed,
    [switch]$SkipPip,
    [string]$PythonVersion = "3.12",
    [string]$DatabaseUrl = "",
    [string]$AdminUrl = "",
    [string]$PgSuperPassword = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Root = $PSScriptRoot
Set-Location -LiteralPath $Root
. (Join-Path $Root "scripts\windows\common.ps1")

$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
$ManagePy = Join-Path $Root "scripts\manage.py"
$script:TesseractPath = ""
$script:AdminUrlForConfigure = ""

# The Gemma failsafe in Docker is on by default: -NoAiDocker (or -WithAiDocker:$false) turns it off
$script:UseAiDocker = $true
if ($NoAiDocker) { $script:UseAiDocker = $false }
if ($PSBoundParameters.ContainsKey("WithAiDocker") -and (-not $WithAiDocker.IsPresent)) { $script:UseAiDocker = $false }
$script:UseVoice = [bool]$WithVoice

# ------------------------------------------------------------------------------------------- helpers
function Invoke-Manage {
    param([string[]]$ManageArgs, [string]$What = "")
    if (-not $What) { $What = "manage.py $($ManageArgs[0])" }
    Invoke-Native $VenvPy (@($ManagePy) + $ManageArgs) $What
}

function Get-EnvValue {
    param([string]$Key)
    if (-not (Test-Path $VenvPy)) { return "" }
    return (Get-NativeOutput $VenvPy @($ManagePy, "env", "get", $Key))
}

function Invoke-Compose {
    param([string[]]$ComposeArgs, [string]$What = "docker compose")
    Invoke-Native "docker" ((Get-ComposeArgs -Root $Root) + $ComposeArgs) $What
}

function Test-PythonOk {
    param([string]$Exe, [string[]]$PrefixArgs = @())
    $code = Invoke-NativeQuiet $Exe ($PrefixArgs + @("-c", "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"))
    return ($code -eq 0)
}

function Find-Python {
    param([string]$Version)
    if (Test-Command "py") {
        if (Test-PythonOk "py" @("-$Version")) {
            return @{ Exe = "py"; Args = @("-$Version") }
        }
    }
    $short = $Version -replace "\.", ""
    $candidates = @()
    if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python$short\python.exe") }
    if ($env:ProgramFiles) { $candidates += (Join-Path $env:ProgramFiles "Python$short\python.exe") }
    foreach ($candidate in $candidates) {
        if ((Test-Path $candidate) -and (Test-PythonOk $candidate)) {
            return @{ Exe = $candidate; Args = @() }
        }
    }
    # any other Python 3.10+ on PATH (but not the Microsoft Store placeholder in WindowsApps)
    foreach ($name in @("python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and ($cmd.Path -notlike "*WindowsApps*") -and (Test-PythonOk $cmd.Path)) {
            return @{ Exe = $cmd.Path; Args = @() }
        }
    }
    return $null
}

function Find-Tesseract {
    $candidates = @()
    if ($env:ProgramFiles) { $candidates += (Join-Path $env:ProgramFiles "Tesseract-OCR\tesseract.exe") }
    if (${env:ProgramFiles(x86)}) { $candidates += (Join-Path ${env:ProgramFiles(x86)} "Tesseract-OCR\tesseract.exe") }
    if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA "Programs\Tesseract-OCR\tesseract.exe") }
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }
    $cmd = Get-Command "tesseract" -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Path }
    return ""
}

# ------------------------------------------------------------------------------------------- steps
function Install-PythonIfMissing {
    Write-Step "Python $PythonVersion"
    if ((Test-Path $VenvPy) -and (Test-PythonOk $VenvPy)) {
        Write-Ok "the virtual environment (.venv) already exists"
        return
    }
    $py = Find-Python $PythonVersion
    if ($null -eq $py) {
        Install-WingetPackage -Id "Python.Python.$PythonVersion" -Name "Python $PythonVersion"
        $py = Find-Python $PythonVersion
        if ($null -eq $py) {
            throw "Python $PythonVersion was installed but is not visible in this window yet. Open a NEW PowerShell window and run setup.ps1 again."
        }
    }
    Write-Ok "$($py.Exe) $($py.Args -join ' ')"
}

function Install-Tesseract {
    Write-Step "Tesseract OCR"
    $script:TesseractPath = Find-Tesseract
    if (-not $script:TesseractPath) {
        try {
            Install-WingetPackage -Id "UB-Mannheim.TesseractOCR" -Name "Tesseract OCR"
        } catch {
            Write-Warn $_.Exception.Message
        }
        $script:TesseractPath = Find-Tesseract
    }
    if ($script:TesseractPath) {
        Write-Ok $script:TesseractPath
    } else {
        Write-Warn "Tesseract is not installed - ID cards are still read by Windows OCR and the AI vision engine."
    }
}

function Initialize-Docker {
    Write-Step "Docker Desktop"
    if (-not (Test-Command "docker")) {
        Install-WingetPackage -Id "Docker.DockerDesktop" -Name "Docker Desktop"
        Update-SessionPath
        $dockerBin = Join-Path $env:ProgramFiles "Docker\Docker\resources\bin"
        if ((-not (Test-Command "docker")) -and (Test-Path $dockerBin)) {
            $env:Path = "$env:Path;$dockerBin"
        }
    }
    if (-not (Test-Command "docker")) {
        throw "Docker Desktop is installed but 'docker' is not available yet. Restart Windows (WSL 2 usually needs it), open Docker Desktop once, then run setup.ps1 again."
    }
    Start-DockerDesktop -TimeoutMinutes 6
    if ((Invoke-NativeQuiet "docker" @("compose", "version")) -ne 0) {
        throw "'docker compose' is not available - update Docker Desktop."
    }
}

function Initialize-Venv {
    Write-Step "Virtual environment (.venv) and Python packages"
    if (-not ((Test-Path $VenvPy) -and (Test-PythonOk $VenvPy))) {
        $py = Find-Python $PythonVersion
        if ($null -eq $py) {
            throw "Python $PythonVersion was not found. Open a new PowerShell window and run setup.ps1 again."
        }
        Invoke-Native $py.Exe ($py.Args + @("-m", "venv", "--clear", (Join-Path $Root ".venv"))) "create the virtual environment"
    }
    if ($SkipPip) {
        Write-Ok "skipping pip (-SkipPip)"
        return
    }
    Invoke-Native $VenvPy @("-m", "pip", "install", "--disable-pip-version-check", "--quiet", "--upgrade", "pip") "upgrade pip"
    Invoke-Native $VenvPy @("-m", "pip", "install", "--disable-pip-version-check", "-r", (Join-Path $Root "requirements-dev.txt")) "install the Python packages"
    Write-Ok "packages installed"
}

function Read-SecretText {
    param([string]$Prompt)
    $secure = Read-Host $Prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Install-NativePostgres {
    Write-Step "PostgreSQL 17 + pgvector (native)"
    $pgRoot = Join-Path $env:ProgramFiles "PostgreSQL\17"
    $psql = Join-Path $pgRoot "bin\psql.exe"
    if (-not (Test-Path $psql)) {
        $superPassword = $PgSuperPassword
        if (-not $superPassword) { $superPassword = New-RandomSecret 24 }
        $override = "--mode unattended --unattendedmodeui none --superpassword $superPassword --serverport 5432 --disable-components stackbuilder"
        Install-WingetPackage -Id "PostgreSQL.PostgreSQL.17" -Name "PostgreSQL 17" -Override $override
        if (-not (Test-Path $psql)) {
            throw "PostgreSQL 17 was not found in $pgRoot after the installation."
        }
        $script:AdminUrlForConfigure = "postgresql+asyncpg://postgres:" + [Uri]::EscapeDataString($superPassword) + "@localhost:5432/postgres"
        # save it right away so a later failure never loses the generated superuser password
        Invoke-Manage @("env", "set", "DATABASE_ADMIN_URL=$($script:AdminUrlForConfigure)") "save DATABASE_ADMIN_URL"
    } elseif ($AdminUrl) {
        $script:AdminUrlForConfigure = $AdminUrl
    } elseif (Get-EnvValue "DATABASE_ADMIN_URL") {
        $script:AdminUrlForConfigure = Get-EnvValue "DATABASE_ADMIN_URL"
    } else {
        $superPassword = $PgSuperPassword
        if (-not $superPassword) {
            $superPassword = Read-SecretText "Password of the PostgreSQL superuser 'postgres' (chosen when PostgreSQL was installed)"
        }
        $script:AdminUrlForConfigure = "postgresql+asyncpg://postgres:" + [Uri]::EscapeDataString($superPassword) + "@localhost:5432/postgres"
    }
    Write-Ok "PostgreSQL 17 in $pgRoot"

    $control = Join-Path $pgRoot "share\extension\vector.control"
    if (-not (Test-Path $control)) {
        $installer = Join-Path $Root "scripts\windows\install_pgvector.ps1"
        if (Test-Admin) {
            & $installer -PgRoot $pgRoot
        } else {
            Write-Info "Building pgvector needs Administrator rights - please accept the Windows (UAC) prompt ..."
            $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$installer`"", "-PgRoot", "`"$pgRoot`"")
            $proc = Start-Process -FilePath "powershell.exe" -Verb RunAs -Wait -PassThru -ArgumentList $argList
            if ($proc.ExitCode -ne 0) {
                throw "The pgvector build failed (exit code $($proc.ExitCode)). Run scripts\windows\install_pgvector.ps1 in an Administrator PowerShell to see the details, or use -DbMode docker."
            }
        }
        if (-not (Test-Path $control)) {
            throw "pgvector is still missing in $pgRoot. Run scripts\windows\install_pgvector.ps1 as Administrator, or use -DbMode docker."
        }
    }
    Write-Ok "pgvector is installed"
}

function Invoke-Setup {
    Write-Host "IronPulse setup for Windows - database mode: $DbMode" -ForegroundColor White
    if ($env:OS -ne "Windows_NT") {
        throw "setup.ps1 is for Windows. On macOS or Linux run ./setup.sh instead."
    }
    if (-not (Test-Command "winget")) {
        Write-Warn "winget was not found - missing tools cannot be installed automatically. Install 'App Installer' from the Microsoft Store if a step fails."
    }

    Install-PythonIfMissing
    Install-Tesseract

    if ($DbMode -eq "docker") {
        Initialize-Docker
    } elseif ($script:UseAiDocker -or $script:UseVoice) {
        try {
            Initialize-Docker
        } catch {
            Write-Warn "$($_.Exception.Message)"
            Write-Warn "Continuing without the Docker extras (Gemma failsafe / Kokoro voice) - run setup.ps1 again later to add them."
            $script:UseAiDocker = $false
            $script:UseVoice = $false
        }
    }

    Initialize-Venv

    Write-Step "Configuration (.env)"
    Invoke-Native $VenvPy @((Join-Path $Root "scripts\make_env.py")) "create .env with fresh secrets"
    $configureArgs = @("configure", "--db", $DbMode)
    if ($script:TesseractPath) { $configureArgs += @("--tesseract", $script:TesseractPath) }
    if ($DbMode -eq "native") {
        Install-NativePostgres
        $configureArgs += @("--admin-url", $script:AdminUrlForConfigure)
    } elseif ($DbMode -eq "existing") {
        $url = $DatabaseUrl
        if (-not $url) {
            $url = Read-Host "PostgreSQL URL of your server (postgresql://user:password@host:5432/ironpulse)"
        }
        if (-not $url) { throw "-DbMode existing needs -DatabaseUrl" }
        $configureArgs += @("--database-url", $url)
        if ($AdminUrl) { $configureArgs += @("--admin-url", $AdminUrl) }
    }
    Invoke-Manage $configureArgs "write the connection settings to .env"

    if ($DbMode -eq "docker") {
        Write-Step "PostgreSQL 17 + pgvector and Redis in Docker"
        Invoke-Compose @("up", "-d", "db", "redis") "start the database and Redis containers"
    }
    if ($script:UseAiDocker) {
        Write-Step "Gemma failsafe in Docker (Ollama)"
        Invoke-Compose @("up", "-d", "ollama", "ollama-pull") "start Ollama"
        Write-Info "The Gemma model downloads in the background (~3.3 GB). Follow it with:"
        Write-Info "  docker compose --env-file .env -f docker\compose.yml logs -f ollama-pull"
    }
    if ($script:UseVoice) {
        Write-Step "Kokoro voice in Docker"
        Invoke-Compose @("--profile", "voice", "up", "-d", "kokoro") "start Kokoro"
    }

    Write-Step "Database: role, databases, extensions and tables"
    Invoke-Manage @("init-db", "--with-test-db", "--wait", "120") "initialise the database"

    if (-not $SkipAssets) {
        Write-Step "Front-end assets (Tailwind CSS + JS bundle)"
        try {
            Invoke-Native $VenvPy @((Join-Path $Root "scripts\build_assets.py")) "build the front-end assets"
        } catch {
            Write-Warn "$($_.Exception.Message) - the prebuilt files in app\static\dist are used."
        }
    }
    if (-not $SkipVideos) {
        Write-Step "Workout videos"
        try {
            Invoke-Manage @("gen-videos") "generate the workout videos"
        } catch {
            Write-Warn "Videos were not generated (optional). Retry later: .venv\Scripts\python scripts\manage.py gen-videos"
        }
    }
    if (-not $SkipSeed) {
        Write-Step "Demo data"
        try {
            Invoke-Manage @("seed") "load the demo data"
        } catch {
            Write-Warn "The demo data was not loaded (optional). Retry later: .venv\Scripts\python scripts\manage.py seed"
        }
    }

    $port = Get-EnvValue "PORT"
    if (-not $port) { $port = "10000" }
    $gateway = Get-EnvValue "AI_GATEWAY_BASE_URL"
    Write-Host ""
    Write-Host "IronPulse is ready." -ForegroundColor Green
    Write-Host ""
    Write-Host "  Start the app:   powershell -ExecutionPolicy Bypass -File run.ps1      (while developing: add -Reload)"
    Write-Host "  Then open:       http://localhost:$port"
    Write-Host "  On your phone:   powershell -ExecutionPolicy Bypass -File run.ps1 -Lan   (HTTPS on this PC's Wi-Fi address)"
    Write-Host "  Health check:    .venv\Scripts\python scripts\manage.py check"
    Write-Host "  Demo accounts:   var\demo_credentials.txt (written by the demo seed)"
    Write-Host "  AI:              Gemma gateway $gateway -> Gemma in Docker -> Claude"
    Write-Host "                   .venv\Scripts\python scripts\manage.py ai-status    |    ai-chat ""Hello Gemma"""
    if ($DbMode -ne "docker") {
        Write-Host "  Redis (optional): sessions are kept in PostgreSQL. For Redis without Docker use Memurai"
        Write-Host "                   (winget install Memurai.MemuraiDeveloper) or Redis inside WSL, then set REDIS_URL in .env."
    }
    Write-Host ""
    Write-Host "  Disclaimer: student-grade project. The owner, the developer and the AI are NOT responsible for anything;" -ForegroundColor Yellow
    Write-Host "  nothing here is medical, legal or financial advice." -ForegroundColor Yellow
}

try {
    Invoke-Setup
    exit 0
} catch {
    Write-Host ""
    Write-Host "Setup stopped: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Fix the problem above and run setup.ps1 again - finished steps are skipped." -ForegroundColor Yellow
    exit 1
}
