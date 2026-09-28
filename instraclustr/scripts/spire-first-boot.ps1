# SPIRE first boot, once per spire-agent lifetime (and again only after its
# spire-agent-data volume is deleted). See "SPIRE: first boot" in
# instraclustr/README.md.
#
#   .\instraclustr\scripts\spire-first-boot.ps1
#
# 1. starts spire-server (its CA is signed by the offline root's SPIRE
#    upstream CA, instraclustr/certs/);
# 2. issues a single-use join token bound to this host's STABLE node alias
#    (spiffe://sentinelfaa.internal/agent/nas-host -- the parent_id every
#    entry in spire-entries.yaml names);
# 3. starts spire-agent with it, through this process's environment only --
#    the token is never written to .env or anywhere else on disk;
# 4. applies spire-entries.yaml (scripts/spire-entries-apply.py) and checks
#    there is no drift left.
# The agent then keeps its node identity in the spire-agent-data volume: every
# later start, the launcher's included, needs no token.
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$infra = Split-Path -Parent $PSScriptRoot
$compose = @("-f", (Join-Path $infra "docker-compose.yml"), "--env-file", (Join-Path $infra ".env"))
$serverSocket = "/tmp/spire-server/private/api.sock"

docker compose @compose up -d --wait spire-server
if ($LASTEXITCODE -ne 0) { throw "spire-server did not become healthy: docker logs spire-server" }

$line = docker compose @compose exec -T spire-server /opt/spire/bin/spire-server token generate `
    -spiffeID spiffe://sentinelfaa.internal/agent/nas-host -socketPath $serverSocket
if ($LASTEXITCODE -ne 0 -or -not ($line -match '^Token:\s*(\S+)')) { throw "could not issue a join token" }

$env:SPIRE_JOIN_TOKEN = $Matches[1]
try {
    docker compose @compose up -d --force-recreate spire-agent
    if ($LASTEXITCODE -ne 0) { throw "spire-agent did not start: docker logs spire-agent" }
} finally {
    Remove-Item Env:\SPIRE_JOIN_TOKEN -ErrorAction SilentlyContinue
}

$attested = $false
for ($i = 0; $i -lt 30; $i++) {
    if ((docker logs spire-agent 2>&1 | Out-String) -match "Node attestation was successful|SVID loaded") { $attested = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $attested) { throw "spire-agent did not attest: docker logs spire-agent" }
Write-Host "spire-agent attested as this host." -ForegroundColor Green

$prefix = "docker compose -f $((Join-Path $infra 'docker-compose.yml') -replace '\\','/') exec -T spire-server"
python (Join-Path $PSScriptRoot "spire-entries-apply.py") (Join-Path $infra "spire-entries.yaml") --exec-prefix $prefix --yes
if ($LASTEXITCODE -ne 0) { throw "applying spire-entries.yaml failed" }
python (Join-Path $PSScriptRoot "spire-entries-apply.py") (Join-Path $infra "spire-entries.yaml") --exec-prefix $prefix --check
if ($LASTEXITCODE -ne 0) { throw "SPIRE registrations still differ from spire-entries.yaml" }
