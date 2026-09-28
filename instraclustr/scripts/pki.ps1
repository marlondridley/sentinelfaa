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

# Docker Desktop shares a Windows drive into its VM only if the drive was
# there when Docker Desktop started. Mounting a folder on a drive it does not
# share is NOT an error to docker: it quietly creates an empty folder on its
# own virtual disk and mounts that -- and root-init once wrote the root key
# there instead of to the USB. So prove each mount is the real folder, with a
# marker written on the Windows side, before anything runs.
function Assert-RealFolder([string]$hostDir) {
    $marker = ".pki-mount-check-" + [guid]::NewGuid().ToString("N")
    $markerPath = Join-Path $hostDir $marker
    Set-Content -Path $markerPath -Value "mount check" -NoNewline
    try {
        docker run --rm --network none -v "${hostDir}:/check:ro" $image test -f "/check/$marker" 2>$null
        $seen = ($LASTEXITCODE -eq 0)
    } finally {
        Remove-Item -Force $markerPath -ErrorAction SilentlyContinue
    }
    if (-not $seen) {
        $drive = Split-Path -Qualifier $hostDir
        Write-Host "ERROR: Docker cannot see $hostDir -- drive $drive is not shared into Docker Desktop." -ForegroundColor Red
        Write-Host "  Docker shares a drive only if it was present when Docker Desktop started. With the" -ForegroundColor Red
        Write-Host "  drive plugged in: quit Docker Desktop, run 'wsl --shutdown', start Docker Desktop," -ForegroundColor Red
        Write-Host "  wait until it is running, and try again. Nothing was written." -ForegroundColor Red
        exit 2
    }
}

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
    $rootPath = (Resolve-Path $RootDir).Path
    Assert-RealFolder $rootPath
    $mounts += @("-v", "${rootPath}:/root-ca")
}
if ($Command -ne "root-init") {
    New-Item -ItemType Directory -Force $certs | Out-Null
    $certsPath = (Resolve-Path $certs).Path
    Assert-RealFolder $certsPath
    $mounts += @("-v", "${certsPath}:/certs")
}

$step = switch ($Command) {
    "root-init" { "/root-ca" }
    "sign"      { "/root-ca", "/certs" }
    default     { "/certs" }
}

# One explicit argument list, built by appending. Windows PowerShell 5.1
# turns a one-element array returned from `if` into a plain string, and
# splatting a string passes it one character at a time: "-it" reached docker
# as "- i t" ("invalid reference format").
$dockerArgs = @("run", "--rm")
if ($Command -in @("root-init", "sign")) {
    $dockerArgs += "-it"            # root-init and sign prompt for the passphrase here
}
$dockerArgs += @("--network", "none")
$dockerArgs += $mounts
$dockerArgs += @($image, "sh", "/pki/scripts/pki.sh", $Command)
$dockerArgs += $step
docker @dockerArgs
exit $LASTEXITCODE
