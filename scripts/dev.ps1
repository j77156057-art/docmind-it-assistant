[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "status")]
    [string]$Command = "start",

    [switch]$Open,

    [switch]$UseConfiguredDatabase
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VirtualEnvironment = Join-Path $ProjectRoot ".venv"
$Python = Join-Path $VirtualEnvironment "Scripts\python.exe"
$RunDirectory = Join-Path $ProjectRoot "data\run"
$LogDirectory = Join-Path $ProjectRoot "data\logs"

$Services = @(
    [pscustomobject]@{
        Name = "query"
        Label = "Query service"
        App = "app:app"
        Port = 8020
        Url = "http://127.0.0.1:8020/"
        Health = "http://127.0.0.1:8020/health/ready"
    },
    [pscustomobject]@{
        Name = "admin"
        Label = "Admin console"
        App = "admin_app:app"
        Port = 8021
        Url = "http://127.0.0.1:8021/"
        Health = "http://127.0.0.1:8021/health/ready"
    }
)

function Write-Step([string]$Message) {
    Write-Host "[DocMind] $Message" -ForegroundColor Cyan
}

function Get-PidFile($Service) {
    return Join-Path $RunDirectory "$($Service.Name).pid"
}

function Write-PidFile($Service, $Process) {
    $State = [pscustomobject]@{
        Pid = $Process.Id
        StartTimeUtc = $Process.StartTime.ToUniversalTime().ToString("O")
        App = $Service.App
        Port = $Service.Port
    }
    $State | ConvertTo-Json -Compress |
        Set-Content -LiteralPath (Get-PidFile $Service) -Encoding ascii
}

function Get-ManagedProcess($Service) {
    $PidFile = Get-PidFile $Service
    if (-not (Test-Path -LiteralPath $PidFile)) {
        return $null
    }

    try {
        $State = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json
        if ($State.App -ne $Service.App -or [int]$State.Port -ne $Service.Port) {
            throw "service identity mismatch"
        }
        $Process = Get-Process -Id ([int]$State.Pid) -ErrorAction Stop
        $ActualStart = $Process.StartTime.ToUniversalTime()
        $ExpectedStart = ([datetime]$State.StartTimeUtc).ToUniversalTime()
        if ($ActualStart.Ticks -ne $ExpectedStart.Ticks) {
            throw "process start time mismatch"
        }
        return $Process
    }
    catch {
        Write-Warning "$($Service.Label) has a stale or invalid PID file; no process was stopped ($($_.Exception.Message))."
        return $null
    }
}

function Get-ListeningProcessId([int]$Port) {
    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $connection) {
        return $null
    }
    return [int]$connection.OwningProcess
}

function Test-Health([string]$Url) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Wait-ForHealth($Service, [int]$Attempts = 30) {
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        if (Test-Health $Service.Health) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Ensure-Environment {
    if (-not (Test-Path -LiteralPath $Python)) {
        Write-Step "Creating the project virtual environment"
        $BootstrapPython = Get-Command python -ErrorAction SilentlyContinue
        if ($null -eq $BootstrapPython) {
            throw "Python was not found. Install Python 3.11 or newer first."
        }
        & $BootstrapPython.Source -m venv $VirtualEnvironment
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to create the virtual environment."
        }
    }

    & $Python -c "import alembic, fastapi, openpyxl, pptx, reportlab, uvicorn" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Step "Installing project dependencies"
        & $Python -m pip install -r (Join-Path $ProjectRoot "requirements-dev.txt")
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to install project dependencies."
        }
    }

    $EnvironmentFile = Join-Path $ProjectRoot ".env"
    if (-not (Test-Path -LiteralPath $EnvironmentFile)) {
        Copy-Item -LiteralPath (Join-Path $ProjectRoot ".env.example") -Destination $EnvironmentFile
        Write-Step "Created .env from .env.example"
    }

    if (-not $UseConfiguredDatabase) {
        $DatabasePath = (Join-Path $ProjectRoot "data\queries.db") -replace "\\", "/"
        $env:IT_DATABASE_URL = "sqlite:///$DatabasePath"
        $env:IT_ENVIRONMENT = "development"
        Write-Step "Using the project-local SQLite database"
    }
}

function Start-DocMind {
    Ensure-Environment
    New-Item -ItemType Directory -Force -Path $RunDirectory, $LogDirectory | Out-Null

    Write-Step "Applying database migrations"
    Push-Location $ProjectRoot
    try {
        & $Python -m alembic upgrade head
        if ($LASTEXITCODE -ne 0) {
            throw "Database migration failed."
        }
    }
    finally {
        Pop-Location
    }

    foreach ($Service in $Services) {
        $ExistingProcessId = Get-ListeningProcessId $Service.Port
        if ($null -ne $ExistingProcessId) {
            Write-Host "$($Service.Label) is already running: $($Service.Url) (PID $ExistingProcessId)"
            continue
        }

        $StandardOutput = Join-Path $LogDirectory "$($Service.Name).out.log"
        $StandardError = Join-Path $LogDirectory "$($Service.Name).err.log"
        $Arguments = @(
            "-B", "-m", "uvicorn", $Service.App,
            "--host", "127.0.0.1", "--port", [string]$Service.Port,
            "--no-access-log"
        )
        $Process = Start-Process -FilePath $Python -ArgumentList $Arguments `
            -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $StandardOutput -RedirectStandardError $StandardError
        Write-Step "Starting $($Service.Label)"
    }

    $Failed = @()
    foreach ($Service in $Services) {
        if (Wait-ForHealth $Service) {
            $ListeningProcessId = Get-ListeningProcessId $Service.Port
            $ListeningProcess = Get-Process -Id $ListeningProcessId -ErrorAction Stop
            Write-PidFile $Service $ListeningProcess
            Write-Host "$($Service.Label): $($Service.Url)" -ForegroundColor Green
        }
        else {
            $Failed += $Service
            Write-Warning "$($Service.Label) is not ready. See data\logs\$($Service.Name).err.log"
        }
    }

    if ($Failed.Count -gt 0) {
        throw "One or more services failed to start."
    }

    if ($Open) {
        Start-Process $Services[0].Url
        Start-Process $Services[1].Url
    }
}

function Stop-DocMind {
    foreach ($Service in $Services) {
        $PidFile = Get-PidFile $Service
        if (-not (Test-Path -LiteralPath $PidFile)) {
            $ExistingProcessId = Get-ListeningProcessId $Service.Port
            if ($null -ne $ExistingProcessId) {
                Write-Warning "$($Service.Label) is running but was not started by this script. Stop it in its original terminal with Ctrl+C."
            }
            else {
                Write-Host "$($Service.Label) is not running"
            }
            continue
        }

        $Process = Get-ManagedProcess $Service
        if ($null -ne $Process) {
            Stop-Process -Id $Process.Id
            Wait-Process -Id $Process.Id -Timeout 10 -ErrorAction SilentlyContinue
            Write-Step "Stopped $($Service.Label)"
        }
        Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue
    }
}

function Show-Status {
    foreach ($Service in $Services) {
        $ProcessId = Get-ListeningProcessId $Service.Port
        $Ready = $null -ne $ProcessId -and (Test-Health $Service.Health)
        [pscustomobject]@{
            Service = $Service.Label
            Status = if ($Ready) { "ready" } elseif ($null -ne $ProcessId) { "starting or unhealthy" } else { "stopped" }
            Port = $Service.Port
            PID = $ProcessId
            Url = $Service.Url
        }
    }
}

switch ($Command) {
    "start" { Start-DocMind }
    "stop" { Stop-DocMind }
    "status" { Show-Status | Format-Table -AutoSize }
}
