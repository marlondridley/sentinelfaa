# Get-SignIns.ps1 -- who signed in to the dashboard, when, and from where.
# Read-only: Keycloak's sign-in events, which scripts/keycloak-rbac.sh turns on
# and keeps for 90 days.
#
#   .\nas-feedops-sentinel\scripts\Get-SignIns.ps1                        the last 7 days
#   .\nas-feedops-sentinel\scripts\Get-SignIns.ps1 -Days 30 -Username jdoe
#
# Sign-ins, failed attempts (wrong password, locked out, turned off) and
# sign-outs, newest first, then a summary. The address is the visitor's as
# Cloudflare saw it: the edge passes CF-Connecting-IP to Keycloak.
#
# Needs nas-keycloak running. The Keycloak admin password is read from this
# stack's .env or ../instraclustr/.env and passed by name, never on a command
# line; nothing is written anywhere.
[CmdletBinding()]
param(
    [ValidateRange(1, 90)][int]$Days = 7,
    [string]$Username
)

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

# One read-only admin query (scripts/keycloak-get.sh), as objects.
function Invoke-KeycloakGet([string]$Path, [string]$Query) {
    try {
        $env:KC_ADMIN_PW = $script:adminPw; $env:KC_PATH = $Path; $env:KC_QUERY = $Query
        $json = docker exec -e KC_ADMIN_PW -e KC_PATH -e KC_QUERY nas-keycloak bash /tmp/keycloak-get.sh 2>$null | Out-String
        if ($LASTEXITCODE -ne 0) { Fail "reading $Path from Keycloak failed (exit $LASTEXITCODE)" }
    } finally {
        Remove-Item Env:KC_ADMIN_PW, Env:KC_PATH, Env:KC_QUERY -ErrorAction SilentlyContinue
    }
    # -InputObject, not a pipe: Windows PowerShell 5.1 would hand back the
    # whole array as one item. Returned items are enumerated by the caller.
    return (ConvertFrom-Json -InputObject $json)
}

$script:adminPw = Get-EnvValue 'KEYCLOAK_ADMIN_PASSWORD'
if (-not $script:adminPw) { Fail "KEYCLOAK_ADMIN_PASSWORD is in neither $($envFiles -join ' nor ')" }
if ((docker inspect -f '{{.State.Running}}' nas-keycloak 2>$null) -ne 'true') { Fail "nas-keycloak is not running" }

$from = [DateTime]::UtcNow.Date.AddDays(-$Days).ToString('yyyy-MM-dd')
docker cp (Join-Path $PSScriptRoot 'keycloak-get.sh') nas-keycloak:/tmp/keycloak-get.sh | Out-Null
try {
    $events = Invoke-KeycloakGet 'events' "dateFrom=$from max=5000"
    $names = @{}
    foreach ($user in (Invoke-KeycloakGet 'users' 'briefRepresentation=true max=10000')) { $names[$user.id] = $user.username }
} finally {
    docker exec -u 0 nas-keycloak rm -f /tmp/keycloak-get.sh | Out-Null   # docker cp: root-owned
}

$reasons = @{
    invalid_user_credentials   = 'wrong password'
    user_not_found             = 'no such login'
    user_disabled              = 'login turned off'
    user_temporarily_disabled  = 'locked out (too many failures)'
    invalid_code               = 'sign-in link reused or expired'
    expired_code               = 'sign-in took too long'
    invalid_redirect_uri       = 'refused: return address not registered'
}
$rows = foreach ($e in $events) {
    $what = switch ($e.type) {
        'LOGIN'           { 'signed in' }
        'LOGIN_ERROR'     { 'FAILED: ' + $(if ($reasons[$e.error]) { $reasons[$e.error] } else { $e.error }) }
        'LOGOUT'          { 'signed out' }
        'UPDATE_PASSWORD' { 'set their password' }
        default           { $null }
    }
    if (-not $what) { continue }
    $who = if ($e.userId -and $names[$e.userId]) { $names[$e.userId] } elseif ($e.details.username) { $e.details.username } else { '(unknown)' }
    if ($Username -and $who -ne $Username) { continue }
    [pscustomobject]@{
        Time    = [DateTimeOffset]::FromUnixTimeMilliseconds([int64]$e.time).LocalDateTime.ToString('yyyy-MM-dd HH:mm')
        Login   = $who
        Event   = $what
        Address = $e.ipAddress
    }
}

$scope = if ($Username) { "'$Username'" } else { 'all logins' }
Write-Host "Sign-in activity, $scope, since $from (UTC):" -ForegroundColor Cyan
if (-not $rows) { Write-Host "  none recorded"; exit 0 }
$rows | Format-Table -AutoSize | Out-String -Width 200 | Write-Host
$signIns = @($rows | Where-Object Event -eq 'signed in')
$failures = @($rows | Where-Object { $_.Event -like 'FAILED*' })
$people = @($signIns.Login | Sort-Object -Unique).Count
$s = { param($n, $word, $plural) if ($n -eq 1) { "$n $word" } else { "$n $plural" } }
Write-Host ("{0} by {1}; {2}." -f (& $s $signIns.Count 'sign-in' 'sign-ins'), (& $s $people 'person' 'people'),
    (& $s $failures.Count 'failed attempt' 'failed attempts')) -ForegroundColor Green
exit 0
