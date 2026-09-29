# Enable-PublicAccess.ps1 -- wire this machine to the public site, once the
# Cloudflare side exists (docs/public-access.md, steps 1-4). Re-runnable.
#
#   .\nas-feedops-sentinel\scripts\Enable-PublicAccess.ps1
#
#   1. Settings   PUBLIC_DASHBOARD_URL, KEYCLOAK_PUBLIC_URL (https://host, no
#                 trailing slash) and CLOUDFLARE_TUNNEL_TOKEN, in either env
#                 file. Only their names are ever printed.
#   2. Keycloak   the dashboard client also accepts the public callback
#                 (scripts/keycloak-rbac.sh, PUBLIC_DASHBOARD_URL).
#   3. Edge       the public listeners (a no-op once they exist).
#   4. On         scripts/Set-PublicAccess.ps1 -Mode On: Keycloak's sign-in
#                 address, the dashboard's return address (the one
#                 redirect_uri line of .streamlit/secrets.toml) and the tunnel.
#                 From then on the launcher switches it: -Public on, else off.
#   5. Proof      scripts/smoke/05_public_exposure_probe.sh: the public
#                 listeners serve the dashboard and the sign-in pages, and
#                 nothing else -- not the admin console, not the master realm.
[CmdletBinding()]
param()

$appDir   = Split-Path -Parent $PSScriptRoot
$repo     = Split-Path -Parent $appDir
$infraEnv = Join-Path (Join-Path $repo 'instraclustr') '.env'
$appEnv   = Join-Path $appDir '.env'
$compose  = Join-Path $appDir 'docker-compose.yml'
$envArgs  = @('--env-file', $infraEnv, '--env-file', $appEnv)

function Fail([string]$message) { Write-Host "ERROR: $message" -ForegroundColor Red; exit 2 }
function Ok([string]$message) { Write-Host "  [+] $message" -ForegroundColor Green }

function Get-EnvValue([string]$Key) {
    $found = ''
    foreach ($file in @($infraEnv, $appEnv)) {      # the application's file wins, as in compose
        if (-not (Test-Path $file)) { continue }
        foreach ($line in [IO.File]::ReadAllLines($file)) {
            if ($line -match "^\s*$([regex]::Escape($Key))\s*=\s*(.*)$") {
                $value = $Matches[1].Trim().Trim('"').Trim("'")
                if ($value) { $found = $value }
            }
        }
    }
    return $found
}

# -- 1. Settings --------------------------------------------------------------
$dashboardUrl = Get-EnvValue 'PUBLIC_DASHBOARD_URL'
$authUrl      = Get-EnvValue 'KEYCLOAK_PUBLIC_URL'
foreach ($pair in @(@('PUBLIC_DASHBOARD_URL', $dashboardUrl), @('KEYCLOAK_PUBLIC_URL', $authUrl))) {
    if ($pair[1] -notmatch '^https://[a-z0-9.-]+[a-z0-9]$') {
        Fail "$($pair[0]) must be set in an env file as https://<hostname> (no path, no trailing slash)"
    }
}
if ($dashboardUrl -eq $authUrl) { Fail "PUBLIC_DASHBOARD_URL and KEYCLOAK_PUBLIC_URL must be different hostnames" }
if (-not (Get-EnvValue 'CLOUDFLARE_TUNNEL_TOKEN')) { Fail "CLOUDFLARE_TUNNEL_TOKEN is in neither env file" }
Ok "settings: PUBLIC_DASHBOARD_URL, KEYCLOAK_PUBLIC_URL, CLOUDFLARE_TUNNEL_TOKEN"

$wanted = "$dashboardUrl/oauth2callback"

# -- 2. Keycloak: allow the public callback ------------------------------------
$adminPw = Get-EnvValue 'KEYCLOAK_ADMIN_PASSWORD'
if (-not $adminPw) { Fail "KEYCLOAK_ADMIN_PASSWORD is in neither env file" }
if ((docker inspect -f '{{.State.Running}}' nas-keycloak 2>$null) -ne 'true') { Fail "nas-keycloak is not running: start the stack first" }
docker cp (Join-Path $PSScriptRoot 'keycloak-rbac.sh') nas-keycloak:/tmp/keycloak-rbac.sh | Out-Null
try {
    $env:KC_ADMIN_PW = $adminPw; $env:PUBLIC_DASHBOARD_URL = $dashboardUrl
    docker exec -e KC_ADMIN_PW -e PUBLIC_DASHBOARD_URL nas-keycloak bash /tmp/keycloak-rbac.sh
    $code = $LASTEXITCODE
} finally {
    Remove-Item Env:KC_ADMIN_PW, Env:PUBLIC_DASHBOARD_URL -ErrorAction SilentlyContinue
    docker exec -u 0 nas-keycloak rm -f /tmp/keycloak-rbac.sh | Out-Null   # docker cp: root-owned
}
if ($code -ne 0) { Fail "updating the realm's dashboard client failed (exit $code)" }
Ok "Keycloak accepts $wanted"

# -- 3. The edge's public listeners ---------------------------------------------
# --no-deps: the running stack only. Without it compose would also start the
# migrate job's identity sidecars, which hold the schema owner's credentials
# and run only during a migration.
docker compose -f $compose @envArgs up -d --wait --no-deps edge
if ($LASTEXITCODE -ne 0) { Fail "the edge did not come back healthy: docker logs nas-edge" }
Ok "edge (public listeners)"

# -- 4. On: sign-in addresses and the tunnel -------------------------------------
& (Join-Path $PSScriptRoot 'Set-PublicAccess.ps1') -Mode On
if ($LASTEXITCODE -ne 0) { Fail "public access could not be switched on (see above)" }

# -- 5. Proof -------------------------------------------------------------------
$git = Get-Command git -ErrorAction SilentlyContinue
$sh = if ($git) { Join-Path (Split-Path -Parent (Split-Path -Parent $git.Source)) "bin\sh.exe" }
if ($sh -and (Test-Path $sh)) {
    & $sh ((Join-Path $repo 'scripts\smoke\05_public_exposure_probe.sh') -replace '\\', '/')
    if ($LASTEXITCODE -ne 0) { Fail "the exposure probe failed: see above" }
}
Write-Host ""
Write-Host "Public access is on:" -ForegroundColor Green
Write-Host "  dashboard  $dashboardUrl" -ForegroundColor Cyan
Write-Host "  sign-in    $authUrl" -ForegroundColor Cyan
Write-Host "From now on: .\launch_nas_sentinel.ps1 -Public  (without -Public it is switched off: tunnel stopped, sign-in at https://localhost:8443)." -ForegroundColor DarkGray
Write-Host "Logins: .\nas-feedops-sentinel\scripts\Add-Guest.ps1 -Username <name> -Email <email> [-Role watch_officer|supervisor]" -ForegroundColor DarkGray
exit 0
