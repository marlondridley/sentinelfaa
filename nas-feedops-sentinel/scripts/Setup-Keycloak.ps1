# Setup-Keycloak.ps1
# Creates (or repairs) the Keycloak realm the dashboard signs in against:
#   realm  nas-sentinel
#   client nas-feedops-sentinel  (confidential; secret taken from
#                                 .streamlit\secrets.toml so the two match)
#   user   one operator login you choose now
# Safe to re-run: existing objects are updated, not duplicated.
#
# Secrets are read from .env / secrets.toml and handed to the container as
# environment variables -- never printed, never on a command line.
$ErrorActionPreference = 'Stop'
$appDir   = Split-Path -Parent $PSScriptRoot
$envFile  = Join-Path $appDir '.env'
$secrets  = Join-Path $appDir '.streamlit\secrets.toml'
$realmSh  = Join-Path $PSScriptRoot 'keycloak-realm.sh'

function Read-EnvValue($path, $key) {
    foreach ($line in Get-Content $path) {
        if ($line -match "^\s*$key\s*=\s*(.*)$") { return $Matches[1].Trim().Trim('"').Trim("'") }
    }
    return $null
}

$adminPw = Read-EnvValue $envFile 'KEYCLOAK_ADMIN_PASSWORD'
if (-not $adminPw) { throw "KEYCLOAK_ADMIN_PASSWORD not found in $envFile" }

$clientSecret = $null; $inKeycloak = $false
foreach ($line in Get-Content $secrets) {
    if ($line -match '^\s*\[auth\.keycloak\]') { $inKeycloak = $true; continue }
    if ($line -match '^\s*\[') { $inKeycloak = $false }
    if ($inKeycloak -and $line -match '^\s*client_secret\s*=\s*"?([^"]*)"?') { $clientSecret = $Matches[1].Trim() }
}
if (-not $clientSecret) { throw "client_secret not found under [auth.keycloak] in $secrets" }

Write-Host "Waiting for Keycloak on http://localhost:8081 ..." -ForegroundColor Yellow
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 'http://localhost:8081/realms/master' | Out-Null; $ready = $true; break }
    catch { Start-Sleep -Seconds 2 }
}
if (-not $ready) { throw "Keycloak did not answer on http://localhost:8081 within 2 minutes. Is nas-keycloak running?" }

$defaultUser = $env:USERNAME
$user  = Read-Host "Dashboard login username [$defaultUser]"; if (-not $user) { $user = $defaultUser }
$email = Read-Host "Email for $user"
if ($email -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$') { throw "That does not look like an email address." }
$pw1 = Read-Host "Password for $user" -AsSecureString
$pw2 = Read-Host "Repeat password" -AsSecureString
$plain = { param($s) [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)) }
$p1 = & $plain $pw1; $p2 = & $plain $pw2
if ($p1 -ne $p2) { throw "Passwords did not match." }
if ($p1.Length -lt 8) { throw "Use at least 8 characters." }

docker cp $realmSh nas-keycloak:/tmp/keycloak-realm.sh | Out-Null
try {
    $env:KC_ADMIN_PW = $adminPw; $env:CLIENT_SECRET = $clientSecret
    $env:APP_USERNAME = $user;   $env:APP_USER_PW = $p1; $env:APP_EMAIL = $email
    docker exec -e KC_ADMIN_PW -e CLIENT_SECRET -e APP_USERNAME -e APP_USER_PW -e APP_EMAIL nas-keycloak bash /tmp/keycloak-realm.sh
    if ($LASTEXITCODE -ne 0) { throw "Realm setup failed inside the container (exit $LASTEXITCODE)." }
}
finally {
    Remove-Item Env:KC_ADMIN_PW, Env:CLIENT_SECRET, Env:APP_USER_PW, Env:APP_USERNAME, Env:APP_EMAIL -ErrorAction SilentlyContinue
    docker exec nas-keycloak rm -f /tmp/keycloak-realm.sh 2>$null | Out-Null
}

$disc = Invoke-RestMethod 'http://localhost:8081/realms/nas-sentinel/.well-known/openid-configuration'
Write-Host ""
Write-Host "Realm is live. What the browser will be sent to:" -ForegroundColor Green
Write-Host "  issuer                 : $($disc.issuer)"
Write-Host "  authorization_endpoint : $($disc.authorization_endpoint)"
Write-Host ""
Write-Host "Open http://localhost:8501 in a NEW tab and sign in as '$user'." -ForegroundColor Cyan
