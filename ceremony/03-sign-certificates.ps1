# Step 3 of 9: sign both requests with the root. USB plugged in.
#
# Asks for the root passphrase once and checks it opens the root key before
# signing anything. Applies the committed policy (instraclustr\certs\policy),
# never what a request asks for. Installs the root's public certificate into
# instraclustr\certs\trust the first time.
#
# Success: two "signed:" lines. Then unplug the USB.
param([string]$RootDir = "D:\sentinelfaa-root-ca")
& (Join-Path $PSScriptRoot "..\instraclustr\scripts\pki.ps1") sign -RootDir $RootDir
exit $LASTEXITCODE
