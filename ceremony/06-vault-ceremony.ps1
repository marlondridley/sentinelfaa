# Step 6 of 9: configure Vault's workload identity (the Terraform root ceremony).
#
# A root token is generated from unseal shares 1 and 2, used for this one
# Terraform run, and revoked. First a plan -- read it -- then the apply, which
# shows the plan again and waits for you to type yes.
#
# First time you should see: the cert auth method, six roles (one per
# service), the policies, the audit device, and on an existing Vault the
# retired sentinel-app / sentinel-migrate AppRole roles to be removed.
# Success: "Apply complete!" then "Root token revoked".
# Exit codes, not ErrorActionPreference=Stop (Windows PowerShell 5.1 stops on docker stderr).
$infra = Join-Path (Split-Path -Parent $PSScriptRoot) "instraclustr"
$compose = @("-f", (Join-Path $infra "docker-compose.yml"), "--env-file", (Join-Path $infra ".env"), "--profile", "ceremony")

docker compose @compose build vault-ceremony
if ($LASTEXITCODE -ne 0) { Write-Host "Building the ceremony image failed: see above. Nothing was changed." -ForegroundColor Red; exit 1 }
docker compose @compose run --rm vault-ceremony plan
# plan exits 2 when there are changes -- expected the first time.
if ($LASTEXITCODE -notin @(0, 2)) { Write-Host "The plan failed: see above. Nothing was changed." -ForegroundColor Red; exit 1 }

docker compose @compose run --rm vault-ceremony apply
exit $LASTEXITCODE
