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
#
# -Public turns public access on (nas-feedops-sentinel/docs/public-access.md):
# the Cloudflare tunnel runs and everyone, operators on this machine too,
# signs in through the public addresses. Without -Public it is turned off:
# the tunnel is stopped and sign-in is back on https://localhost:8443. The
# site is public only on the launches that ask for it.
[CmdletBinding()]
param(
    [switch]$WithLiveFeed,
    # Skip the Cleanup-KitLog janitor window (only meaningful with -WithLiveFeed).
    [switch]$NoJanitor,
    [switch]$Public
)

$ErrorActionPreference = "Stop"
$scriptDir = $PSScriptRoot

Write-Host "Starting NAS FeedOps Sentinel Launch Sequence..." -ForegroundColor Cyan

# NOTE: we intentionally do NOT parse .env files into the PowerShell process
# anymore. `docker compose --env-file <path>` already handles ${VAR}
# substitution inside the compose YAML and passes the same values into each
# container's environment -- doing it twice was redundant and only risked the
# two copies drifting out of sync.

# 0. .env is the source of truth for the stacks it configures.
# docker compose lets a variable already in this process's environment
# override the same key in --env-file. A stale Windows user variable did
# exactly that: a provider key variable held an old placeholder, so the proxy kept
# getting it even after .env had the real key. For this process only (nothing
# persistent is changed), remove any variable an env file also defines.
function Clear-ShadowingEnv([string]$envFile) {
    if (-not (Test-Path $envFile)) { return }
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
            $name = $Matches[1]
            if (Test-Path "Env:\$name") {
                Write-Host "  [!] Ignoring $name from the environment; using $envFile" -ForegroundColor Yellow
                Remove-Item "Env:\$name"
            }
        }
    }
}
Clear-ShadowingEnv (Join-Path $scriptDir "instraclustr\.env")
Clear-ShadowingEnv (Join-Path $scriptDir "nas-feedops-sentinel\.env")

# 1. Network Setup
$networkName = "nas-network"
$existingNetwork = docker network ls --filter "name=$networkName" --format "{{.Name}}"

if ($existingNetwork -eq $networkName) {
    Write-Host "Docker network '$networkName' already exists." -ForegroundColor Green
} else {
    Write-Host "Creating Docker network '$networkName'..." -ForegroundColor Yellow
    docker network create $networkName
}

# External, like the network above: the socket spire-agent serves and every
# application container's spiffe-helper sidecar reads (see
# instraclustr/spire-entries.yaml and nas-feedops-sentinel/docker-compose.yml)
# has to be one real volume shared across both compose projects.
$spireSocketVolume = "nas-spire-agent-socket"
$existingSpireVolume = docker volume ls --filter "name=$spireSocketVolume" --format "{{.Name}}"
if ($existingSpireVolume -eq $spireSocketVolume) {
    Write-Host "Docker volume '$spireSocketVolume' already exists." -ForegroundColor Green
} else {
    Write-Host "Creating Docker volume '$spireSocketVolume'..." -ForegroundColor Yellow
    docker volume create $spireSocketVolume
}

# 1b. Workload-identity PKI: Vault serves TLS only, and SPIRE signs under the
#     offline root, so nothing can start without these certificates. They come
#     from the root ceremony (instraclustr/certs/README.md), never from here.
$pkiScript = Join-Path $scriptDir "instraclustr\scripts\pki.ps1"
& $pkiScript check
switch ($LASTEXITCODE) {
    0 { Write-Host "  [+] Workload-identity certificates are current." -ForegroundColor Green }
    1 { Write-Host "  [!] A workload-identity certificate is due for renewal (above). Plan the ceremony: instraclustr\certs\README.md" -ForegroundColor Yellow }
    default {
        Write-Host "CRITICAL ERROR: workload-identity certificates missing or invalid (above)." -ForegroundColor Red
        Write-Host "  Run the root ceremony first: instraclustr\certs\README.md" -ForegroundColor Red
        exit 1
    }
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

# 3b. Bootstrap jobs must have succeeded: Vault unseal and seeding, LiteLLM's
#     database, and the application's scoped LiteLLM key. A failure here would
#     otherwise surface later as application services that cannot start.
foreach ($job in @("vault-unseal", "litellm-db-init", "litellm-bootstrap")) {
    $code = docker inspect $job --format '{{.State.ExitCode}}' 2>$null
    $state = docker inspect $job --format '{{.State.Status}}' 2>$null
    if ($state -ne "exited" -or $code -ne "0") {
        Write-Host "CRITICAL ERROR: bootstrap job $job did not complete (state=$state exit=$code). See: docker logs $job" -ForegroundColor Red
        exit 1
    }
    Write-Host "  [+] $job completed." -ForegroundColor Green
}

# 3c. Workload identity: every application service gets its secrets through
#     an SVID from spire-agent. An agent that has never joined (first boot) or
#     lost its data volume exits at once, and every service would then wait on
#     its Vault Agent sidecar forever.
$agentState = docker inspect spire-agent --format '{{.State.Status}}' 2>$null
if ($agentState -ne "running") {
    Write-Host "CRITICAL ERROR: spire-agent is not running (state=$agentState)." -ForegroundColor Red
    Write-Host "  First boot, or its data volume was reset: instraclustr\scripts\spire-first-boot.ps1" -ForegroundColor Red
    Write-Host "  Then Vault's cert auth, if never applied: the root ceremony in instraclustr\README.md" -ForegroundColor Red
    exit 1
}
Write-Host "  [+] spire-agent is running." -ForegroundColor Green

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
# Both env files, infrastructure first: a secret the application stack's
# own services need (API key, Grafana, Keycloak, Temporal) may live in either,
# and a value in the application's file wins. Compose substitutes only the
# variables the application's compose file names -- the infrastructure's
# other secrets reach no application container. (Database passwords reach
# the application only through Vault Agent, never an env file.)
$appEnvFiles = @("--env-file", $infraEnv, "--env-file", $appEnv)
docker compose -f $appCompose @appEnvFiles up --build -d
if ($LASTEXITCODE -ne 0) {
    Write-Host "CRITICAL ERROR: Application stack failed to build or start. Halting." -ForegroundColor Red
    Write-Host "  A service waiting on secrets: docker logs nas-<service>-vault-agent (and nas-<service>-spiffe)" -ForegroundColor Red
    exit 1
}

# The migrate job has finished (`up` waits for it before starting anything
# else). Its sidecars hold the schema owner's Vault token and identity; a
# one-shot job's credentials should not outlive the job.
docker compose -f $appCompose @appEnvFiles stop migrate-vault-agent migrate-spiffe | Out-Null
Write-Host "  [+] migrate finished; its identity sidecars stopped." -ForegroundColor Green

# 4b. Public access: on for this launch only if asked for. Set-PublicAccess
# switches Keycloak's sign-in address, the dashboard's return address and the
# tunnel together (restarting only what changed), so sign-in works in both
# modes: publicly with -Public, at https://localhost:8443 without it.
$publicMode = if ($Public) { 'On' } else { 'Off' }
& (Join-Path $appDir 'scripts\Set-PublicAccess.ps1') -Mode $publicMode
if ($LASTEXITCODE -ne 0) {
    Write-Host "CRITICAL ERROR: public access could not be switched $($publicMode.ToLower()) (see above; nas-feedops-sentinel\docs\public-access.md)." -ForegroundColor Red
    exit 1
}

# 4c. Logins past their last day (Add-Guest.ps1 -Days) are turned off. Not a
# reason to halt: the dashboard refuses them on every page by itself.
& (Join-Path $appDir 'scripts\Disable-ExpiredLogins.ps1')
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [!] Expired logins could not be checked (see above); the dashboard still refuses them." -ForegroundColor Yellow
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
Write-Host "Launch sequence complete!  (HTTPS via the edge proxy; self-signed cert for localhost)" -ForegroundColor Green
if ($Public) {
    Write-Host "  - Dashboard          : the public address above (public access is on; sign-in goes through it)" -ForegroundColor Cyan
} else {
    Write-Host "  - Dashboard          : https://localhost:8443" -ForegroundColor Cyan
}
Write-Host "  - API                : https://localhost:8444/health" -ForegroundColor Cyan
Write-Host "  - Grafana            : https://localhost:8445" -ForegroundColor Cyan
Write-Host "  - Temporal UI        : https://localhost:8446" -ForegroundColor Cyan
Write-Host "  - Prometheus (alerts): https://localhost:8447/alerts" -ForegroundColor Cyan
Write-Host "  - Alertmanager       : https://localhost:8448" -ForegroundColor Cyan
Write-Host "  - Keycloak admin     : https://localhost:8081/admin" -ForegroundColor DarkGray
Write-Host "  - LiteLLM admin UI   : http://localhost:4000/ui" -ForegroundColor DarkGray
Write-Host ""
if ($feedStarted) {
    Write-Host "  Live SWIM feed running in its own window." -ForegroundColor Green
    Write-Host "  Messages land in nas-feedops-sentinel\data\raw\swim_live\log\" -ForegroundColor DarkGray
    Write-Host "  Anything the ingester cannot read goes to data\failed\ with a reason." -ForegroundColor DarkGray
} else {
    Write-Host "  No live feed started. Re-run with -WithLiveFeed to connect to FAA SWIM," -ForegroundColor DarkGray
    Write-Host "  or start it alone: nas-feedops-sentinel\jumpstart\run-live.ps1" -ForegroundColor DarkGray
}
