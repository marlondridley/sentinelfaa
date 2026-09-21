<#
.SYNOPSIS
    Purge processed STDDS message files from the FAA Jumpstart Kit log folder.

.DESCRIPTION
    The FAA SCDS Jumpstart Kit writes one file per JMS message into its log/
    folder at ~20 messages/sec sustained. At that rate the folder will hold
    1.7M files per day without intervention.

    This script removes files older than -MaxAgeMinutes (default 5 minutes)
    so the Sentinel polling loop has a chance to ingest them while keeping
    the on-disk footprint small.

    The Sentinel processes files newest-first and caps each pass at 500
    files. Anything older than 5 minutes has either already been processed
    (and moved to data/processed/) or is too stale to matter — safe to drop.

.PARAMETER LogPath
    Path to the Jumpstart Kit log folder. Defaults to the standard project
    layout assuming jumpstart-latest sits beside nas-feedops-sentinel.

.PARAMETER MaxAgeMinutes
    Files older than this are removed. Default: 5 minutes.

.PARAMETER Watch
    Run continuously in a 60-second loop. Use this when running in a
    dedicated PowerShell window as a background janitor.

.PARAMETER IntervalSeconds
    Sleep between watch iterations. Default: 60 seconds.

.EXAMPLE
    .\Cleanup-KitLog.ps1
    Single-pass cleanup. Returns the count of files removed.

.EXAMPLE
    .\Cleanup-KitLog.ps1 -Watch
    Run forever, cleaning every 60 seconds. Ctrl+C to stop.

.EXAMPLE
    .\Cleanup-KitLog.ps1 -MaxAgeMinutes 10 -Watch -IntervalSeconds 120
    Keep last 10 minutes of files; sweep every 2 minutes.
#>

[CmdletBinding()]
param(
    # Defaults to nas-feedops-sentinel\data\raw\swim_live\log, derived from this
    # script's own location rather than hardcoded to one machine's paths.
    #
    # This MUST stay under data/, because that is the only directory bind-
    # mounted into the sentinel container (./data:/app/data in
    # docker-compose.yml). The previous default pointed at
    # sentinelfaa\jumpstart-latest\log, which sits outside that mount -- the
    # container could not see those files at all, so nothing written there
    # was ever ingested.
    [string]$LogPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'data\raw\swim_live\log'),
    [int]$MaxAgeMinutes = 5,
    [switch]$Watch,
    [int]$IntervalSeconds = 60
)

function Invoke-OneCleanupPass {
    param([string]$Path, [int]$AgeMin)

    if (-not (Test-Path -LiteralPath $Path)) {
        Write-Warning "Kit log folder does not exist: $Path"
        return 0
    }

    $cutoff = (Get-Date).AddMinutes(-$AgeMin)
    $removed = 0

    Get-ChildItem -LiteralPath $Path -File | Where-Object {
        $_.LastWriteTime -lt $cutoff
    } | ForEach-Object {
        try {
            Remove-Item -LiteralPath $_.FullName -Force -ErrorAction Stop
            $removed++
        } catch {
            # File may be in use (Kit writing) or already gone — skip
        }
    }

    $remaining = (Get-ChildItem -LiteralPath $Path -File | Measure-Object).Count
    $ts = Get-Date -Format "HH:mm:ss"
    Write-Host "[$ts] Cleanup-KitLog: removed $removed file(s) older than $AgeMin min ; $remaining remain"
    return $removed
}

if ($Watch) {
    Write-Host "Cleanup-KitLog: WATCH MODE — sweeping $LogPath every $IntervalSeconds s, keeping last $MaxAgeMinutes min. Ctrl+C to stop."
    while ($true) {
        [void](Invoke-OneCleanupPass -Path $LogPath -AgeMin $MaxAgeMinutes)
        Start-Sleep -Seconds $IntervalSeconds
    }
} else {
    [void](Invoke-OneCleanupPass -Path $LogPath -AgeMin $MaxAgeMinutes)
}
