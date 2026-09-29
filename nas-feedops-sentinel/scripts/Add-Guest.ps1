# Add-Guest.ps1 -- a login for the public site (docs/public-access.md):
# read-only by default, or with a decision role for a demo.
#
#   .\nas-feedops-sentinel\scripts\Add-Guest.ps1 -Username jdoe -Email jdoe@example.com
#   .\nas-feedops-sentinel\scripts\Add-Guest.ps1 -Username jdoe -Email jdoe@example.com -Role watch_officer
#   .\nas-feedops-sentinel\scripts\Add-Guest.ps1 -Username jdoe -Disable
#
# -Role (src/auth.py DECISION_ROLES):
#   none           (default) sees everything, decides nothing
#   auditor        the same, as a named auditor
#   watch_officer  approve, reject, monitor
#   supervisor     those, and override the AI's recommendation or severity
# The user ends with exactly that role: running it again with another role
# replaces it. Every decision is signed with the user's name in the audit
# trail, so give each person their own login, never a shared one.
#
# The password is generated here, shown ONCE, and is temporary -- Keycloak
# makes the user choose their own at first sign-in. Give it to them
# privately (not in the same email as the link).
#
# -Disable turns the login off and ends its sessions at once. (If you also
# run Cloudflare Access in front of the site -- optional, see the runbook --
# add or remove the guest's email there too.)
#
# Needs nas-keycloak running. The Keycloak admin password is read from this
# stack's .env or ../instraclustr/.env and passed by name, never on a command
# line; nothing secret is written to disk.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-zA-Z0-9._-]{3,64}$')][string]$Username,
    [string]$Email,
    [ValidateSet('none', 'auditor', 'watch_officer', 'supervisor')][string]$Role = 'none',
    [switch]$Disable
)

$appDir   = Split-Path -Parent $PSScriptRoot
$envFiles = @((Join-Path $appDir '.env'), (Join-Path (Join-Path (Split-Path -Parent $appDir) 'instraclustr') '.env'))
$operator = Join-Path $PSScriptRoot 'keycloak-operator.sh'

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

# 16 characters from an alphabet without look-alikes (0/O, 1/l/I): meets the
# realm's policy (12+, not the username or email) and survives being read aloud.
function New-TemporaryPassword {
    $alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
    $bytes = New-Object byte[] 16
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return -join ($bytes | ForEach-Object { $alphabet[$_ % $alphabet.Length] })
}

$adminPw = Get-EnvValue 'KEYCLOAK_ADMIN_PASSWORD'
if (-not $adminPw) { Fail "KEYCLOAK_ADMIN_PASSWORD is in neither $($envFiles -join ' nor ')" }
if ((docker inspect -f '{{.State.Running}}' nas-keycloak 2>$null) -ne 'true') { Fail "nas-keycloak is not running" }
if (-not $Disable -and $Email -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$') { Fail "-Email must be the guest's email address" }

docker cp $operator nas-keycloak:/tmp/keycloak-operator.sh | Out-Null
$temporary = $null
try {
    $env:KC_ADMIN_PW = $adminPw
    $env:APP_USERNAME = $Username
    if ($Disable) {
        $env:APP_DISABLE = '1'
        docker exec -e KC_ADMIN_PW -e APP_USERNAME -e APP_DISABLE nas-keycloak bash /tmp/keycloak-operator.sh
    } else {
        $temporary = New-TemporaryPassword
        $env:APP_USER_PW = $temporary
        $env:APP_EMAIL = $Email
        $env:APP_ROLES = $Role
        $env:APP_ROLES_EXACT = '1'
        $env:APP_TEMPORARY_PASSWORD = '1'
        $env:APP_LAST_NAME = if ($Role -eq 'none') { 'Guest' } else { 'Operator' }
        docker exec -e KC_ADMIN_PW -e APP_USERNAME -e APP_USER_PW -e APP_EMAIL -e APP_ROLES -e APP_ROLES_EXACT `
            -e APP_TEMPORARY_PASSWORD -e APP_LAST_NAME nas-keycloak bash /tmp/keycloak-operator.sh
    }
    $code = $LASTEXITCODE
} finally {
    Remove-Item Env:KC_ADMIN_PW, Env:APP_USERNAME, Env:APP_USER_PW, Env:APP_EMAIL, Env:APP_ROLES,
        Env:APP_TEMPORARY_PASSWORD, Env:APP_LAST_NAME, Env:APP_DISABLE -ErrorAction SilentlyContinue
    # docker cp leaves the file owned by root, so root removes it.
    docker exec -u 0 nas-keycloak rm -f /tmp/keycloak-operator.sh | Out-Null
}
if ($code -ne 0) { Fail "Keycloak did not accept the change (exit $code); see above" }

if ($Disable) {
    Write-Host ""
    Write-Host "'$Username' is off; their sessions are ended." -ForegroundColor Green
    exit 0
}
Write-Host ""
$can = @{ none = 'read-only'; auditor = 'auditor, read-only'; watch_officer = 'watch officer: approve, reject, monitor'
          supervisor = 'supervisor: approve, reject, monitor, override' }[$Role]
Write-Host "'$Username' ($Email) is ready: $can." -ForegroundColor Green
Write-Host "  Temporary password (shown once): $temporary" -ForegroundColor Yellow
Write-Host "  Give it to them privately. They choose their own at first sign-in." -ForegroundColor Yellow
Write-Host "  They sign in at the public dashboard address (docs/public-access.md)." -ForegroundColor Cyan
$temporary = $null
exit 0
