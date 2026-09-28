# Step 1 of 9: create the root CA -- once, ever.
#
# Writes root-ca.key (P-384, passphrase-encrypted) and root-ca.crt straight to
# the USB folder; nothing is written to this computer. Asks for the passphrase
# twice (at least 12 characters): put it in your password manager FIRST.
#
# Success: "root created: ...". Then copy the USB folder to the backup USB.
# If it says root-ca.key already exists, a root was made before: stop and ask.
param([string]$RootDir = "D:\sentinelfaa-root-ca")
& (Join-Path $PSScriptRoot "..\instraclustr\scripts\pki.ps1") root-init -RootDir $RootDir
exit $LASTEXITCODE
