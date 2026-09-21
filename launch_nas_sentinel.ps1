# launch_nas_sentinel.ps1
# Complete local launcher for NAS FeedOps Sentinel
#
# -WithLiveFeed also starts the FAA SWIM Jumpstart Kit (jumpstart/run-live.ps1)
# in its own window, plus the log janitor that keeps its output folder from
# growing without bound. The kit is NOT containerized -- it ships a prebuilt
# Java 25 jar and runs natively; see the jumpstart comment in
# nas-feedops-sentinel/docker-compose.yml for why.
#
# It is opt-in rather than automatic because it opens a live subscription to
# the FAA broker and requires a Java 25 runtime on this machine.
[CmdletBinding()]
param(
    [switch]$WithLiveFeed,
    # Skip the Cleanup-KitLog janitor window (only meaningful with -WithLiveFeed).
    [switch]$NoJanitor
)

$ErrorActionPreference = "Stop"
$scriptDir = $PSScriptRoot

Write-Host "Starting NAS FeedOps Sentinel Launch Sequence..." -ForegroundColor Cyan

# NOTE: we intentionally do NOT parse .env files into the PowerShell process
# anymore. `docker compose --env-file <path>` already handles ${VAR}
# substitution inside the compose YAML and passes the same values into each
# container's environment -- doing it twice was redundant and only risked the
# two copies drifting out of sync.

# 1. Network Setup
$networkName = "nas-network"
$existingNetwork = docker network ls --filter "name=$networkName" --format "{{.Name}}"

if ($existingNetwork -eq $networkName) {
    Write-Host "Docker network '$networkName' already exists." -ForegroundColor Green
} else {
    Write-Host "Creating Docker network '$networkName'..." -ForegroundColor Yellow
    docker network create $networkName
}

# 2. Start Infrastructure Stack (Postgres, PgBouncer, Vault, LiteLLM)
#    Kafka/Connect/Cassandra/Elasticsearch/Kibana were never wired into this
#    application and are commented out in instraclustr/docker-compose.yml.
$infraDir = Join-Path $scriptDir "instraclustr"
$infraEnv = Join-Path $infraDir ".env"
$infraCompose = Join-Path $infraDir "docker-compose.yml"

if (-not (Test-Path $infraDir)) {
    Write-Host "CRITICAL ERROR: Infrastructure directory '$infraDir' not found." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $infraEnv)) {
    Write-Host "CRITICAL ERROR: Environment file '$infraEnv' not found!" -ForegroundColor Red
    exit 1
}

Write-Host "Bringing up infrastructure stack from '$infraDir'..." -ForegroundColor Yellow
docker compose -f $infraCompose --env-file $infraEnv up -d --build
if ($LASTEXITCODE -ne 0) {
    Write-Host "CRITICAL ERROR: Infrastructure stack failed to start. Halting." -ForegroundColor Red
    exit 1
}

# 3. Cross-Stack Pre-Flight Health Checks (PgBouncer, Vault, LiteLLM)
Write-Host "Verifying external infrastructure readiness across stack boundaries..." -ForegroundColor Yellow

$infraServices = @(
    @{ Name = "PgBouncer"; Container = "nas-pgbouncer" },
    @{ Name = "Vault";      Container = "vault" },
    @{ Name = "LiteLLM";    Container = "nas-litellm" }
)

foreach ($svc in $infraServices) {
    $ready = $false
    $containerName = $svc.Container
    $displayName = $svc.Name
    $status = ""

    for ($i = 0; $i -lt 30; $i++) {
        try {
            $rawJson = docker inspect $containerName 2>$null
            if ($rawJson) {
                $info = $rawJson | ConvertFrom-Json
                if ($info -and $info.Count -gt 0) {
                    if ($info[0].State.Health) {
                        $status = $info[0].State.Health.Status
                    } else {
                        $status = $info[0].State.Status
                    }
                }
            }
        } catch {
            $status = ""
        }

        if ($status -eq "healthy" -or $status -eq "running") {
            $ready = $true
            break
        }
        Start-Sleep -Seconds 2
    }

    if ($ready) {
        Write-Host "  [+] $displayName is healthy ($status)." -ForegroundColor Green
    } else {
        Write-Host "CRITICAL ERROR: $displayName did not report healthy. Halting application launch." -ForegroundColor Red
        exit 1
    }
}

# 4. Build and Launch Application Stack
$appDir = Join-Path $scriptDir "nas-feedops-sentinel"
$appEnv = Join-Path $appDir ".env"
$appCompose = Join-Path $appDir "docker-compose.yml"

if (-not (Test-Path $appDir)) {
    Write-Host "CRITICAL ERROR: Application directory '$appDir' not found!" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $appEnv)) {
    Write-Host "CRITICAL ERROR: Environment file '$appEnv' not found!" -ForegroundColor Red
    exit 1
}

Write-Host "Building and launching application stack from '$appDir'..." -ForegroundColor Yellow
docker compose -f $appCompose --env-file $appEnv up --build -d
if ($LASTEXITCODE -ne 0) {
    Write-Host "CRITICAL ERROR: Application stack failed to build or start. Halting." -ForegroundColor Red
    exit 1
}

# 5. Optional: live FAA SWIM feed (native, not containerized)
$feedStarted = $false
if ($WithLiveFeed) {
    $runLive = Join-Path $appDir 'jumpstart\run-live.ps1'
    $janitor = Join-Path $appDir 'scripts\Cleanup-KitLog.ps1'

    if (-not (Test-Path $runLive)) {
        Write-Host "WARNING: $runLive not found; skipping live feed." -ForegroundColor Yellow
    } else {
        Write-Host "Starting FAA SWIM live feed in a separate window..." -ForegroundColor Yellow
        # Its own window on purpose: the kit runs in the foreground until
        # Ctrl+C, so running it inline would block this launcher forever.
        Start-Process -FilePath 'powershell.exe' `
            -ArgumentList '-NoExit', '-ExecutionPolicy', 'Bypass', '-File', "`"$runLive`"" `
            -WorkingDirectory (Split-Path -Parent $runLive)
        $feedStarted = $true

        if (-not $NoJanitor -and (Test-Path $janitor)) {
            # MessageFileOutput writes one file per message with no rotation.
            # At the observed sustained rate that is ~1.7M files/day, so the
            # janitor is not optional housekeeping -- it is what keeps the
            # ingest folder scannable.
            Write-Host "Starting Kit log janitor in a separate window..." -ForegroundColor Yellow
            Start-Process -FilePath 'powershell.exe' `
                -ArgumentList '-NoExit', '-ExecutionPolicy', 'Bypass', '-File', "`"$janitor`"", '-Watch' `
                -WorkingDirectory (Split-Path -Parent $janitor)
        }
    }
}

Write-Host ""
Write-Host "Launch sequence complete!" -ForegroundColor Green
Write-Host "  - Streamlit Dashboard : http://localhost:8501" -ForegroundColor Cyan
Write-Host "  - LiteLLM Admin UI   : http://localhost:4000/ui" -ForegroundColor Cyan
Write-Host "  - Prometheus Metrics : http://localhost:9090" -ForegroundColor Cyan
Write-Host "  - Grafana Dashboard  : http://localhost:3000" -ForegroundColor Cyan
Write-Host "  - Temporal UI        : http://localhost:8088" -ForegroundColor Cyan
Write-Host "  - API (FastAPI)      : http://localhost:8000/health" -ForegroundColor Cyan
Write-Host ""
if ($feedStarted) {
    Write-Host "  Live SWIM feed running in its own window." -ForegroundColor Green
    Write-Host "  Messages land in nas-feedops-sentinel\data\raw\swim_live\log\" -ForegroundColor DarkGray
    Write-Host "  Anything the ingester cannot read goes to data\failed\ with a reason." -ForegroundColor DarkGray
} else {
    Write-Host "  No live feed started. Re-run with -WithLiveFeed to connect to FAA SWIM," -ForegroundColor DarkGray
    Write-Host "  or start it alone: nas-feedops-sentinel\jumpstart\run-live.ps1" -ForegroundColor DarkGray
}
