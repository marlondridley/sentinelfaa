<#
.SYNOPSIS
    Limit who can read this checkout's key material to you, SYSTEM and Administrators.

.DESCRIPTION
    The CA keys, the Vault unseal share and bootstrap secret-id, the .env files, secrets.toml and the Terraform
    state sit in folders that inherit their permissions from the folder the repository was cloned into. On a
    machine where that folder is readable by another group (a sandbox account for a coding assistant, another
    local user), every file below it is too. A private key is only private if nothing else can read it.

    This script shows, for each of those paths, who can read it now, and by default changes nothing. Run it
    again with -Apply to remove the inherited access and leave only you, SYSTEM and Administrators. Docker
    Desktop reads bind mounts as you, so the stack is unaffected. If a tool that runs as another account does
    need a file, grant it back on purpose.

    It does not read any file's contents.

.PARAMETER Apply
    Change the permissions. Without it the script only reports.

.EXAMPLE
    .\instraclustr\scripts\Protect-KeyMaterial.ps1            # report only
    .\instraclustr\scripts\Protect-KeyMaterial.ps1 -Apply     # restrict
#>
[CmdletBinding()]
param([switch]$Apply)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$me = "$env:USERDOMAIN\$env:USERNAME"

$paths = @(
    'nas-feedops-sentinel\tls',
    'nas-feedops-sentinel\.env',
    'nas-feedops-sentinel\.streamlit\secrets.toml',
    'instraclustr\.env',
    'instraclustr\vault-init-data',
    'instraclustr\certs',
    'instraclustr\terraform'
)

# Who is expected to be able to read them. A prefix match, so BUILTIN\Administrators and NT AUTHORITY\SYSTEM
# and you count; anything else is what this script is for.
function Test-Expected([string]$identity) {
    return ($identity -ieq $me) -or ($identity -like 'NT AUTHORITY\SYSTEM') -or ($identity -like 'BUILTIN\Administrators')
}

$exposed = 0
foreach ($relative in $paths) {
    $path = Join-Path $root $relative
    if (-not (Test-Path -LiteralPath $path)) { continue }
    $others = @((Get-Acl -LiteralPath $path).Access |
        Where-Object { $_.AccessControlType -eq 'Allow' -and -not (Test-Expected $_.IdentityReference.Value) } |
        ForEach-Object { $_.IdentityReference.Value } | Sort-Object -Unique)
    if ($others.Count -eq 0) {
        Write-Host ("  ok       {0}" -f $relative)
        continue
    }
    $exposed++
    Write-Host ("  exposed  {0}`n           also readable by: {1}" -f $relative, ($others -join ', ')) -ForegroundColor Yellow
    if ($Apply) {
        $isDir = (Get-Item -LiteralPath $path -Force).PSIsContainer
        $flags = if ($isDir) { '(OI)(CI)F' } else { 'F' }
        & icacls $path /inheritance:r /grant:r "${me}:$flags" "NT AUTHORITY\SYSTEM:$flags" "BUILTIN\Administrators:$flags" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "icacls failed on $relative (exit $LASTEXITCODE)" }
        Write-Host "           restricted to $me, SYSTEM and Administrators" -ForegroundColor Green
    }
}

if ($exposed -eq 0) {
    Write-Host 'Nothing else can read the key material.'
} elseif (-not $Apply) {
    Write-Host ("{0} path(s) readable by another account. Nothing was changed; run with -Apply to restrict them." -f $exposed)
}
