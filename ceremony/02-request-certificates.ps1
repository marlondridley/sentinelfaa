# Step 2 of 9: request the two certificates the root signs.
#
# Generates, on this computer, a fresh key for the SPIRE upstream CA and one
# for Vault's TLS certificate (instraclustr\certs\spire, \vault -- they never
# leave this computer), and a CSR for each. The USB is not needed.
#
# Success: two "request:" lines. Needs Docker Desktop running.
& (Join-Path $PSScriptRoot "..\instraclustr\scripts\pki.ps1") request
exit $LASTEXITCODE
