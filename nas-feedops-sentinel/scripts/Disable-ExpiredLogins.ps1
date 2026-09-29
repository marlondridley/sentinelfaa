# Disable-ExpiredLogins.ps1 -- turn off every login past its last day
# (Add-Guest.ps1 -Days), and end its sessions. Idempotent.
#
#   .\nas-feedops-sentinel\scripts\Disable-ExpiredLogins.ps1
#
# The launcher runs it on every launch. It is the second line: the dashboard
# already refuses a signed-in login past its last day by itself, on every page
# (src/keycloak_auth.access_ended), whether or not this has run. This also
# stops such a login at Keycloak, so it cannot sign in at all.
#
# A login's last day is the admin-only user attribute sentinel_expires_on
# (YYYY-MM-DD, UTC). Logins without one (operators) are never touched.
#
# Needs nas-keycloak running. The Keycloak admin password is read from this
# stack's .env or ../instraclustr/.env and passed by name, never on a command line.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'   # docker writes progress to stderr
$appDir   = Split-Path -Parent $PSScriptRoot
$envFiles = @((Join-Path $appDir '.env'), (Join-Path (Join-Path (Split-Path -Parent $appDir) 'instraclustr') '.env'))

function Fail([string]$message) { Write-Host "ERROR: $message" -ForegroundColor Red; exit 2 }

function Get-EnvValue([string]$Key) {
    foreach ($file in $envFiles) {
        if (-not (Test-Path $file)) { continue }
        foreach ($line in [IO.File]::ReadAllLines($file)) {
            if ($line -match "^\s*$([regex]::Escape($Key))\s*=\s*(.*)$") {
                $value = $Matches[1].Trim().Trim('"').Trim("'")
                if ($value) { return $value }
            }
        }
    }
    return ''
}

$adminPw = Get-EnvValue 'KEYCLOAK_ADMIN_PASSWORD'
if (-not $adminPw) { Fail "KEYCLOAK_ADMIN_PASSWORD is in neither $($envFiles -join ' nor ')" }
if ((docker inspect -f '{{.State.Running}}' nas-keycloak 2>$null) -ne 'true') { Fail "nas-keycloak is not running" }

$today = [DateTime]::UtcNow.ToString('yyyy-MM-dd')
docker cp (Join-Path $PSScriptRoot 'keycloak-get.sh') nas-keycloak:/tmp/keycloak-get.sh | Out-Null
docker cp (Join-Path $PSScriptRoot 'keycloak-operator.sh') nas-keycloak:/tmp/keycloak-operator.sh | Out-Null
$turnedOff = @()
try {
    $env:KC_ADMIN_PW = $adminPw; $env:KC_PATH = 'users'; $env:KC_QUERY = 'max=10000'
    $json = docker exec -e KC_ADMIN_PW -e KC_PATH -e KC_QUERY nas-keycloak bash /tmp/keycloak-get.sh 2>$null | Out-String
    if ($LASTEXITCODE -ne 0) { Fail "reading the realm's logins failed (exit $LASTEXITCODE)" }
    Remove-Item Env:KC_PATH, Env:KC_QUERY -ErrorAction SilentlyContinue

    # -InputObject, not a pipe: Windows PowerShell 5.1 would hand back the
    # whole array as one item.
    foreach ($user in (ConvertFrom-Json -InputObject $json)) {
        $ends = @($user.attributes.sentinel_expires_on)[0]
        if (-not $user.enabled -or -not $ends) { continue }
        # yyyy-MM-dd compares as text; a value that is not a date counts as over.
        if ($ends -match '^\d{4}-\d{2}-\d{2}$' -and $ends -ge $today) { continue }
        $env:APP_USERNAME = $user.username; $env:APP_DISABLE = '1'
        docker exec -e KC_ADMIN_PW -e APP_USERNAME -e APP_DISABLE nas-keycloak bash /tmp/keycloak-operator.sh | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail "turning off '$($user.username)' failed (exit $LASTEXITCODE)" }
        $turnedOff += "$($user.username) (last day $ends)"
    }
} finally {
    Remove-Item Env:KC_ADMIN_PW, Env:KC_PATH, Env:KC_QUERY, Env:APP_USERNAME, Env:APP_DISABLE -ErrorAction SilentlyContinue
    docker exec -u 0 nas-keycloak rm -f /tmp/keycloak-get.sh /tmp/keycloak-operator.sh | Out-Null   # docker cp: root-owned
}

if ($turnedOff) {
    Write-Host "  [+] expired logins turned off, sessions ended: $($turnedOff -join ', ')" -ForegroundColor Green
} else {
    Write-Host "  [+] expired logins: none" -ForegroundColor Green
}
exit 0
