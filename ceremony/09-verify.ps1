# Step 9 of 9: prove it -- every unauthorized path to a secret is refused.
#
# Runs scripts\smoke\02_negative_access_probe.sh against the live stack: a
# container with no identity gets none; a real identity cannot reach another
# service's secrets, write, author policy or mint tokens; a missing or forged
# client certificate is refused; plain HTTP to Vault gets nothing. Also
# re-checks the certificates.
#
# Success: "RESULT: all unauthorized paths refused" and exit 0. Keep the
# output with the ceremony record (RECORD.md).
# Exit codes, not ErrorActionPreference=Stop (Windows PowerShell 5.1 stops on docker stderr).
$repo = Split-Path -Parent $PSScriptRoot
$git = Get-Command git -ErrorAction SilentlyContinue
$sh = if ($git) { Join-Path (Split-Path -Parent (Split-Path -Parent $git.Source)) "bin\sh.exe" }
if (-not $sh -or -not (Test-Path $sh)) { Write-Host "Git for Windows is required to run the probe." -ForegroundColor Red; exit 2 }

& (Join-Path $repo "instraclustr\scripts\pki.ps1") check
& $sh ((Join-Path $repo "scripts\smoke\02_negative_access_probe.sh") -replace '\\', '/')
exit $LASTEXITCODE
