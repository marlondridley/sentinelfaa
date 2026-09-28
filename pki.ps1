# Workload-identity PKI ceremony, from the repository root. Forwards every
# argument to instraclustr\scripts\pki.ps1 -- see instraclustr\certs\README.md.
#
#   .\pki.ps1 root-init -RootDir D:\sentinelfaa-root-ca   # OFFLINE, once
#   .\pki.ps1 request
#   .\pki.ps1 sign -RootDir D:\sentinelfaa-root-ca
#   .\pki.ps1 install
#   .\pki.ps1 check
& (Join-Path $PSScriptRoot "instraclustr\scripts\pki.ps1") @args
exit $LASTEXITCODE
