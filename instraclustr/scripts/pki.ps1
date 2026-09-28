# Windows wrapper for pki.sh: runs each ceremony step in a throwaway
# container (postgres:16, already in the stack, ships OpenSSL 3) with no
# network, mounting only what that step needs. See instraclustr/certs/README.md.
#
#   .\instraclustr\scripts\pki.ps1 root-init -RootDir E:\sentinelfaa-root   # OFFLINE, once
#   .\instraclustr\scripts\pki.ps1 request
#   .\instraclustr\scripts\pki.ps1 sign -RootDir E:\sentinelfaa-root        # asks for the passphrase
#   .\instraclustr\scripts\pki.ps1 install
#   .\instraclustr\scripts\pki.ps1 check                                    # 0 ok, 1 due, 2 broken
#
# RootDir is the root's USB folder. It is mounted only for root-init and
# sign, and the root key is read from it in place -- never copied to this disk.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("root-init", "request", "sign", "install", "check")]
    [string]$Command,
    [string]$RootDir,
    # Defaults to instraclustr/certs; another directory for rehearsals.
    [string]$CertsDir
)

$ErrorActionPreference = "Stop"
$infra = Split-Path -Parent $PSScriptRoot
$policy = Join-Path $infra "certs\policy"
$certs = if ($CertsDir) { $CertsDir } else { Join-Path $infra "certs" }
$image = "postgres:16"

$mounts = @(
    "-v", "$(Join-Path $PSScriptRoot 'pki.sh'):/pki/scripts/pki.sh:ro",
    "-v", "${policy}:/pki/certs/policy:ro"
)
if ($Command -in @("root-init", "sign")) {
    if (-not $RootDir) { throw "-RootDir (the root's USB folder) is required for $Command" }
    if ($Command -eq "sign" -and -not (Test-Path (Join-Path $RootDir "root-ca.key"))) {
        throw "No root-ca.key in $RootDir"
    }
    New-Item -ItemType Directory -Force $RootDir | Out-Null
    $mounts += @("-v", "$((Resolve-Path $RootDir).Path):/root-ca")
}
if ($Command -ne "root-init") {
    New-Item -ItemType Directory -Force $certs | Out-Null
    $mounts += @("-v", "$((Resolve-Path $certs).Path):/certs")
}

$args_ = switch ($Command) {
    "root-init" { @("root-init", "/root-ca") }
    "sign"      { @("sign", "/root-ca", "/certs") }
    default     { @($Command, "/certs") }
}

# -it: root-init and sign prompt for the passphrase on this terminal.
$tty = if ($Command -in @("root-init", "sign")) { @("-it") } else { @() }
docker run --rm @tty --network none @mounts $image sh /pki/scripts/pki.sh @args_
exit $LASTEXITCODE
