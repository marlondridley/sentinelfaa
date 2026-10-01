# Restart-TlsConsumers.ps1
# Restarts each running service that reads ./tls only when it starts, if the
# certificates there were written after it started.
#
# edge-certs (src/devcert.py) renews the server certificates, and replaces the
# local authority when it must (one that is not limited to this stack's names,
# docs/runbooks.md). It rewrites the files in place, but nginx, Keycloak, the
# dashboard and the tunnel load them only at startup, and `docker compose up`
# leaves a running container alone even though edge-certs has just run. After
# a renewal they would keep serving the old certificate; after the authority
# is replaced, what they serve no longer verifies against what they trust, and
# sign-in stops working.
#
# The services are the ones whose volumes mount ./tls, read from the compose
# file itself, so a new one is covered without a list to keep up. Only running
# containers are restarted: nothing stopped is started (the migrate job's
# sidecars stay down). Safe to re-run: a service that started after its
# certificates were written is left alone. -WhatIf shows what would restart.
#
# Called by launch_nas_sentinel.ps1 and Setup-Keycloak.ps1 after edge-certs.
[CmdletBinding(SupportsShouldProcess)]
param([Parameter(Mandatory)][string]$ComposeFile)

$ErrorActionPreference = 'Stop'
$tlsDir = Join-Path (Split-Path -Parent $ComposeFile) 'tls'
$certs = @(Get-ChildItem -Path $tlsDir -Recurse -File -Filter '*.crt' -ErrorAction SilentlyContinue)
if (-not $certs) { return }
$written = ($certs | Sort-Object LastWriteTimeUtc | Select-Object -Last 1).LastWriteTimeUtc

# No interpolation: the service list needs no secret, so none is read.
$config = (docker compose -f $ComposeFile --profile '*' config --no-interpolate --format json) -join "`n" | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "docker compose could not read $ComposeFile (exit $LASTEXITCODE)." }
$consumers = foreach ($service in $config.services.PSObject.Properties) {
    $mountsTls = @($service.Value.volumes | Where-Object {
        $_.type -eq 'bind' -and (($_.source -replace '\\', '/') -split '/') -contains 'tls'
    })
    if ($mountsTls) { $service.Name }
}

$restarted = @()
foreach ($name in $consumers) {
    $ids = @(docker ps -q --filter "label=com.docker.compose.project=$($config.name)" `
                         --filter "label=com.docker.compose.service=$name")
    foreach ($id in $ids) {
        # RFC 3339 with nanoseconds; .NET parses at most seven fractional digits.
        $startedText = (docker inspect -f '{{.State.StartedAt}}' $id) -replace '(\.\d{7})\d+', '$1'
        $started = [DateTimeOffset]::Parse($startedText, [Globalization.CultureInfo]::InvariantCulture).UtcDateTime
        if ($started -lt $written -and $PSCmdlet.ShouldProcess($name, 'restart: its certificates were written after it started')) {
            docker restart $id | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "docker could not restart $name (exit $LASTEXITCODE)." }
            $restarted += $name
        }
    }
}
if ($restarted) {
    Write-Host "  [+] Restarted $($restarted -join ', '): their certificates were issued after they started." -ForegroundColor Green
}
