# Set-PublicAccess.ps1 -- switch the site between public and local-only
# (docs/public-access.md). The launcher runs it on every launch: -Public turns
# it on, a launch without -Public turns it off. Idempotent: restarts only what
# changed.
#
#   .\nas-feedops-sentinel\scripts\Set-PublicAccess.ps1 -Mode On
#   .\nas-feedops-sentinel\scripts\Set-PublicAccess.ps1 -Mode Off
#
# Three things must agree, or nobody can sign in:
#
#                      On                                   Off
#   Keycloak sign-in   KEYCLOAK_PUBLIC_URL                  https://localhost:8081
#     (.env KEYCLOAK_BROWSER_URL, compose's KC_HOSTNAME)
#   dashboard return   PUBLIC_DASHBOARD_URL/oauth2callback  https://localhost:8443/oauth2callback
#     (.streamlit/secrets.toml redirect_uri)
#   tunnel             nas-cloudflared running              stopped
#
# The mode lives in the files, so every `docker compose` run (the launcher's,
# Setup-Keycloak.ps1's, one typed by hand) starts Keycloak with the same
# sign-in address. Only setting names are printed, never values from .env.
# The Keycloak client accepts both return addresses once Enable-PublicAccess.ps1
# has run (scripts/keycloak-rbac.sh), so no Keycloak change is needed here.
[CmdletBinding()]
param([Parameter(Mandatory = $true)][ValidateSet('On', 'Off')][string]$Mode)

$ErrorActionPreference = 'Continue'   # docker writes progress to stderr
$appDir   = Split-Path -Parent $PSScriptRoot
$repo     = Split-Path -Parent $appDir
$infraEnv = Join-Path (Join-Path $repo 'instraclustr') '.env'
$appEnv   = Join-Path $appDir '.env'
$compose  = Join-Path $appDir 'docker-compose.yml'
$secrets  = Join-Path (Join-Path $appDir '.streamlit') 'secrets.toml'
$envArgs  = @('--env-file', $infraEnv, '--env-file', $appEnv)
$utf8     = New-Object System.Text.UTF8Encoding($false)

$localSignIn   = 'https://localhost:8081'
$localCallback = 'https://localhost:8443/oauth2callback'
$browserKey    = 'KEYCLOAK_BROWSER_URL'
$browserNote   = '# Set by scripts/Set-PublicAccess.ps1 while public access is on: the address browsers sign in at. Do not edit.'

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

function Test-Running([string]$Container) {
    return (docker inspect -f '{{.State.Running}}' $Container 2>$null) -eq 'true'
}

# -- What the mode needs ---------------------------------------------------------
if ($Mode -eq 'On') {
    $dashboardUrl = Get-EnvValue 'PUBLIC_DASHBOARD_URL'
    $signIn       = Get-EnvValue 'KEYCLOAK_PUBLIC_URL'
    foreach ($pair in @(@('PUBLIC_DASHBOARD_URL', $dashboardUrl), @('KEYCLOAK_PUBLIC_URL', $signIn))) {
        if ($pair[1] -notmatch '^https://[a-z0-9.-]+[a-z0-9]$') {
            Fail "$($pair[0]) must be set in an env file as https://<hostname> (docs/public-access.md)"
        }
    }
    if (-not (Get-EnvValue 'CLOUDFLARE_TUNNEL_TOKEN')) { Fail "CLOUDFLARE_TUNNEL_TOKEN is in neither env file (docs/public-access.md)" }
    $callback = "$dashboardUrl/oauth2callback"
} else {
    $signIn   = $localSignIn
    $callback = $localCallback
}

# -- 1. Keycloak's sign-in address (.env) ------------------------------------------
if (-not (Test-Path $appEnv)) { Fail "missing $appEnv" }
$lines = [Collections.Generic.List[string]]([IO.File]::ReadAllLines($appEnv))
$current = ''
foreach ($line in $lines) { if ($line -match "^\s*$browserKey\s*=\s*(.*)$") { $current = $Matches[1].Trim() } }
$wantedLine = if ($Mode -eq 'On') { "$browserKey=$signIn" } else { $null }
$envChanged = $false
if ($Mode -eq 'On' -and $current -ne $signIn) {
    $kept = @($lines | Where-Object { $_ -notmatch "^\s*$browserKey\s*=" })
    $lines = [Collections.Generic.List[string]]$kept
    if (-not ($lines -contains $browserNote)) { $lines.Add(''); $lines.Add($browserNote) }
    $lines.Add($wantedLine)
    $envChanged = $true
} elseif ($Mode -eq 'Off' -and $current) {
    $lines = [Collections.Generic.List[string]]@($lines | Where-Object { $_ -notmatch "^\s*$browserKey\s*=" })
    $envChanged = $true
}
if ($envChanged) { [IO.File]::WriteAllLines($appEnv, $lines, $utf8) }
Ok "Keycloak sign-in address: $signIn$(if ($envChanged) { ' (changed)' })"

# -- 2. The dashboard's return address (secrets.toml) -----------------------------
if (-not (Test-Path $secrets)) { Fail "missing $secrets" }
$toml = [IO.File]::ReadAllLines($secrets)
$index = -1
for ($i = 0; $i -lt $toml.Length; $i++) { if ($toml[$i] -match '^\s*redirect_uri\s*=') { $index = $i; break } }
if ($index -lt 0) { Fail "no redirect_uri in $secrets" }
$secretsChanged = $toml[$index].Trim() -ne "redirect_uri = `"$callback`""
if ($secretsChanged) {
    $toml[$index] = "redirect_uri = `"$callback`""
    [IO.File]::WriteAllLines($secrets, $toml, $utf8)
}
Ok "dashboard return address: $callback$(if ($secretsChanged) { ' (changed)' })"

# -- 3. Restart what changed ------------------------------------------------------
# --no-deps: the running stack only. Without it compose would also start the
# migrate job's identity sidecars, which hold the schema owner's credentials.
if ($envChanged -and (Test-Running 'nas-keycloak')) {
    docker compose -f $compose @envArgs up -d --wait --no-deps keycloak
    if ($LASTEXITCODE -ne 0) { Fail "keycloak did not come back healthy: docker logs nas-keycloak" }
    Ok "keycloak restarted"
}
if ($secretsChanged -and (Test-Running 'nas-dashboard')) {
    docker compose -f $compose @envArgs restart dashboard | Out-Null      # re-reads secrets.toml
    docker compose -f $compose @envArgs up -d --wait --no-deps dashboard
    if ($LASTEXITCODE -ne 0) { Fail "the dashboard did not come back healthy: docker logs nas-dashboard" }
    Ok "dashboard restarted"
}

# -- 4. The tunnel ------------------------------------------------------------------
if ($Mode -eq 'On') {
    docker compose -f $compose @envArgs --profile public up -d --wait --no-deps cloudflared
    if ($LASTEXITCODE -ne 0) { Fail "the tunnel did not connect: docker logs nas-cloudflared" }
    Ok "public access ON: $dashboardUrl (sign-in at $signIn)"
} else {
    if (Test-Running 'nas-cloudflared') {
        docker compose -f $compose @envArgs --profile public stop cloudflared | Out-Null
    }
    Ok "public access OFF: https://localhost:8443 (sign-in at $localSignIn); the tunnel is stopped"
}
exit 0
