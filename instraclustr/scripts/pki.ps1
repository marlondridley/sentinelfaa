# Windows wrapper for pki.sh -- see instraclustr/certs/README.md.
#
#   .\instraclustr\scripts\pki.ps1 root-init -RootDir D:\sentinelfaa-root-ca   # once
#   .\instraclustr\scripts\pki.ps1 request
#   .\instraclustr\scripts\pki.ps1 sign -RootDir D:\sentinelfaa-root-ca        # asks for the passphrase
#   .\instraclustr\scripts\pki.ps1 install
#   .\instraclustr\scripts\pki.ps1 check                                       # 0 ok, 1 due, 2 broken
#
# Where each step runs:
#   root-init, sign   natively, with Git for Windows' sh and OpenSSL 3, reading
#                     and writing the root's USB folder in place. Docker Desktop
#                     does not share removable drives into its VM, and mounting
#                     a folder on an unshared drive is not an error to docker:
#                     it silently mounts an empty folder on its own disk -- the
#                     first root-init wrote the root key there, not to the USB.
#   request, install, check
#                     in a throwaway container (postgres:16, OpenSSL 3) with no
#                     network, on instraclustr/certs; install runs as root there
#                     so it can hand vault.key to Vault's user (uid 100).
#
# The root passphrase is read here with a masked prompt (root-init asks twice
# and needs 12+ characters), handed to pki.sh through this process's
# environment for the one command, and removed right after.
# Rehearsals: set PKI_ROOT_PASS (env:NAME or file:PATH) to skip the prompt.
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
$pkiSh = Join-Path $PSScriptRoot "pki.sh"
$policy = Join-Path $infra "certs\policy"
$certs = if ($CertsDir) { $CertsDir } else { Join-Path $infra "certs" }
$image = "postgres:16"
$minPassphrase = 12

function Fail([string]$message) {
    Write-Host "ERROR: $message" -ForegroundColor Red
    exit 2
}

# A path pki.sh (under Git's sh) reads the same way: forward slashes.
function To-ShPath([string]$path) { return ((Resolve-Path $path).Path -replace '\\', '/') }

# Native programs may write to stderr; under Windows PowerShell 5.1 with
# ErrorActionPreference=Stop, a redirected stderr line is a terminating error.
function Invoke-Quiet([scriptblock]$block) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $block *> $null; return $LASTEXITCODE } finally { $ErrorActionPreference = $previous }
}

function Read-Secret([string]$prompt) {
    $secure = Read-Host -Prompt $prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

# ── root-init, sign: natively ────────────────────────────────────────────────
if ($Command -in @("root-init", "sign")) {
    if (-not $RootDir) { Fail "-RootDir (the root's USB folder) is required for $Command" }
    if ($Command -eq "sign" -and -not (Test-Path (Join-Path $RootDir "root-ca.key"))) {
        Fail "no root-ca.key in $RootDir"
    }
    $git = Get-Command git -ErrorAction SilentlyContinue
    $sh = if ($git) { Join-Path (Split-Path -Parent (Split-Path -Parent $git.Source)) "bin\sh.exe" }
    if (-not $sh -or -not (Test-Path $sh)) { Fail "Git for Windows is required (its sh and OpenSSL 3 run the root steps)" }
    $version = & $sh -c "openssl version" 2>$null
    if ($version -notmatch '^OpenSSL 3\.') { Fail "Git for Windows' OpenSSL 3 is required; found: $version" }

    New-Item -ItemType Directory -Force $RootDir | Out-Null
    $shArgs = @($Command, (To-ShPath $RootDir))
    if ($Command -eq "sign") {
        New-Item -ItemType Directory -Force $certs | Out-Null
        $shArgs += (To-ShPath $certs)
    }

    $ownPassphrase = -not $env:PKI_ROOT_PASS
    if ($ownPassphrase) {
        if ($Command -eq "root-init") {
            if (Test-Path (Join-Path $RootDir "root-ca.key")) {
                # Let pki.sh say which case this is (finished root, or an unfinished one).
                $passphrase = ""
            } else {
                $passphrase = Read-Secret "Root key passphrase"
                if ($passphrase.Length -lt $minPassphrase) { Fail "use at least $minPassphrase characters; nothing was created" }
                if ((Read-Secret "Again, to confirm") -ne $passphrase) { Fail "the two entries differ; nothing was created" }
            }
        } else {
            $passphrase = Read-Secret "Root key passphrase"
        }
        $env:PKI_CEREMONY_PASS = $passphrase
        $env:PKI_ROOT_PASS = "env:PKI_CEREMONY_PASS"
        $passphrase = $null
    }
    try {
        & $sh ($pkiSh -replace '\\', '/') @shArgs
        $code = $LASTEXITCODE
    } finally {
        if ($ownPassphrase) { Remove-Item Env:\PKI_CEREMONY_PASS, Env:\PKI_ROOT_PASS -ErrorAction SilentlyContinue }
    }
    exit $code
}

# ── request, install, check: in a container ──────────────────────────────────
if ((Invoke-Quiet { docker info }) -ne 0) {
    Fail "Docker is not running (or still starting): wait for 'Engine running' and try again"
}

# Prove the certs folder is really mounted (see the header): a marker written
# on the Windows side must be visible in the container.
New-Item -ItemType Directory -Force $certs | Out-Null
$certsPath = (Resolve-Path $certs).Path
$marker = ".pki-mount-check-" + [guid]::NewGuid().ToString("N")
Set-Content -Path (Join-Path $certsPath $marker) -Value "mount check" -NoNewline
try {
    $seen = (Invoke-Quiet { docker run --rm --network none -v "${certsPath}:/check:ro" $image test -f "/check/$marker" }) -eq 0
} finally {
    Remove-Item -Force (Join-Path $certsPath $marker) -ErrorAction SilentlyContinue
}
if (-not $seen) {
    Fail "Docker cannot see $certsPath (drive $(Split-Path -Qualifier $certsPath) is not shared into Docker Desktop). Nothing was written."
}

# One explicit argument list, built by appending: Windows PowerShell 5.1
# unrolls a one-element array returned from `if` into a string, and
# splatting a string passes it one character at a time.
$dockerArgs = @("run", "--rm", "--network", "none",
    "-v", "${pkiSh}:/pki/scripts/pki.sh:ro",
    "-v", "${policy}:/pki/certs/policy:ro",
    "-v", "${certsPath}:/certs",
    $image, "sh", "/pki/scripts/pki.sh", $Command, "/certs")
docker @dockerArgs
exit $LASTEXITCODE
