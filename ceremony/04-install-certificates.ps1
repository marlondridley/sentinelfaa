# Step 4 of 9: install the signed certificates, then check them.
#
# Each certificate is checked against its key, the root and the policy before
# anything is replaced; Vault's key is handed to Vault's user. Then `check`
# reports every certificate.
#
# Success: two "installed:" lines, then two "OK" lines. Needs Docker Desktop.
$pki = Join-Path $PSScriptRoot "..\instraclustr\scripts\pki.ps1"
& $pki install
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $pki check
exit $LASTEXITCODE
