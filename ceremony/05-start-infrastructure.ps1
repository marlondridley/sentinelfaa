# Step 5 of 9: start the infrastructure stack on the new certificates.
#
# Vault comes up TLS-only; vault-unseal unseals and seeds it. spire-agent
# EXITS on this first start -- it has no identity until step 7. Expected.
#
# Success: "Infrastructure up; Vault unsealed and seeded over TLS." Needs
# Docker Desktop.
#
# Exit codes, not ErrorActionPreference=Stop: under Windows PowerShell 5.1 a
# docker message on stderr would otherwise stop the script by itself.
$repo = Split-Path -Parent $PSScriptRoot
$infra = Join-Path $repo "instraclustr"

# The shared network and SPIRE socket volume, as the launcher creates them.
docker network inspect nas-network *> $null
if ($LASTEXITCODE -ne 0) { docker network create nas-network | Out-Null }
docker volume inspect nas-spire-agent-socket *> $null
if ($LASTEXITCODE -ne 0) { docker volume create nas-spire-agent-socket | Out-Null }

docker compose -f (Join-Path $infra "docker-compose.yml") --env-file (Join-Path $infra ".env") up -d --build
if ($LASTEXITCODE -ne 0) { Write-Host "Infrastructure did not start: see the output above." -ForegroundColor Red; exit 1 }

docker wait vault-unseal | Out-Null
$code = docker inspect vault-unseal --format "{{.State.ExitCode}}"
if ($code -ne "0") { Write-Host "vault-unseal failed (exit $code): docker logs vault-unseal" -ForegroundColor Red; exit 1 }
Write-Host "Infrastructure up; Vault unsealed and seeded over TLS." -ForegroundColor Green
