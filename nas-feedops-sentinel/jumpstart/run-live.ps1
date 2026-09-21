<#
.SYNOPSIS
    Launches the FAA SWIM SCDS Jumpstart Kit against the live broker and writes
    each received message into the folder the Sentinel ingest watcher observes.

.DESCRIPTION
    This replaces the containerized jumpstart service that used to live in
    docker-compose.yml. The kit ships a prebuilt fat jar and its own launcher,
    so building an image for it added a Maven/JDK-25 step that bought nothing.

    What this script does:
      1. Reads the SWIM_* credentials from ..\.env so they never appear on a
         command line, in PowerShell history, or in a process list argument
         that other local users can read.
      2. Checks that a Java 25+ runtime is present -- the shipped jar is
         compiled to bytecode major 69 and will not load on anything older.
      3. Creates the output directory and launches the kit from its parent,
         because MessageFileOutput hardcodes its destination to <cwd>\log\.

    -Djson=true -Dheaders=true are NOT optional. src/swim_file_ingest.py's
    _parse_jms_message_file() parses exactly that shape: zero or more
    "Property name = <k>; value = <v>" header lines, then one JSON object.
    Drop either flag and every file lands in data/failed/.

.EXAMPLE
    cd jumpstart
    .\run-live.ps1

.EXAMPLE
    # Watch what is arriving, in another terminal:
    Get-ChildItem ..\data\raw\swim_live\log | Measure-Object
#>
[CmdletBinding()]
param(
    # Defaults to ..\data\raw\swim_live\log, which is what SWIM_RAW_FOLDER
    # in .env points the watcher at. Change both together or not at all.
    [string]$OutputDir
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$envFile  = Join-Path $repoRoot '.env'

if (-not (Test-Path $envFile)) {
    throw "Cannot find $envFile. Run this from the jumpstart\ directory of the repo."
}

# --- Load SWIM_* from .env -------------------------------------------------
$settings = @{}
foreach ($line in Get-Content $envFile) {
    $trimmed = $line.Trim()
    if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
    $idx = $trimmed.IndexOf('=')
    if ($idx -lt 1) { continue }
    $key = $trimmed.Substring(0, $idx).Trim()
    $val = $trimmed.Substring($idx + 1).Trim()
    # Strip one layer of surrounding quotes if present.
    if ($val.Length -ge 2 -and
        (($val.StartsWith('"') -and $val.EndsWith('"')) -or
         ($val.StartsWith("'") -and $val.EndsWith("'")))) {
        $val = $val.Substring(1, $val.Length - 2)
    }
    $settings[$key] = $val
}

$required = @(
    'SWIM_PROVIDER_URL',
    'SWIM_QUEUE',
    'SWIM_CONNECTION_FACTORY',
    'SWIM_USERNAME',
    'SWIM_PASSWORD',
    'SWIM_VPN'
)
$missing = $required | Where-Object { -not $settings.ContainsKey($_) -or $settings[$_] -eq '' }
if ($missing) {
    throw "Missing or empty in .env: $($missing -join ', ')"
}

# --- Verify the Java runtime ----------------------------------------------
# The jar is bytecode major 69 (Java 25). An older JRE fails with
# UnsupportedClassVersionError, which is a confusing error to hit at 2am.
try {
    $versionOutput = & java -version 2>&1 | Out-String
} catch {
    throw "No 'java' on PATH. The shipped jar needs a Java 25+ runtime (e.g. Temurin 25)."
}
if ($versionOutput -match 'version "(\d+)') {
    $major = [int]$Matches[1]
    if ($major -lt 25) {
        throw "Java $major found, but the Jumpstart jar requires Java 25 or newer. Install Temurin 25."
    }
    Write-Host "Java $major detected." -ForegroundColor DarkGray
} else {
    Write-Warning "Could not parse the Java version; continuing. If you see UnsupportedClassVersionError, install Java 25."
}

# --- Resolve and prepare the output directory ------------------------------
if (-not $OutputDir) {
    $OutputDir = Join-Path $repoRoot 'data\raw\swim_live\log'
}
$launchDir = Split-Path -Parent $OutputDir   # MessageFileOutput writes to <cwd>\log\
$leaf      = Split-Path -Leaf $OutputDir

if ($leaf -ne 'log') {
    Write-Warning "MessageFileOutput always writes to a folder literally named 'log' under the working directory. '$leaf' will be ignored; output will go to $launchDir\log."
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$jar = Join-Path $PSScriptRoot 'lib\jumpstart-jar-with-dependencies.jar'
if (-not (Test-Path $jar)) {
    throw "Prebuilt jar not found at $jar."
}

Write-Host ""
Write-Host "Broker    : $($settings['SWIM_PROVIDER_URL'])"
Write-Host "Queue     : $($settings['SWIM_QUEUE'])"
Write-Host "VPN       : $($settings['SWIM_VPN'])"
Write-Host "User      : $($settings['SWIM_USERNAME'])"
Write-Host "Writing to: $OutputDir"
Write-Host ""
Write-Host "Ctrl+C to stop. The sentinel container picks these up automatically." -ForegroundColor DarkGray
Write-Host ""

# Launch from the parent so the hardcoded .\log\ resolves to $OutputDir.
Push-Location $launchDir
try {
    & java `
        "-DproviderUrl=$($settings['SWIM_PROVIDER_URL'])" `
        "-Dqueue=$($settings['SWIM_QUEUE'])" `
        "-DconnectionFactory=$($settings['SWIM_CONNECTION_FACTORY'])" `
        "-Dusername=$($settings['SWIM_USERNAME'])" `
        "-Dpassword=$($settings['SWIM_PASSWORD'])" `
        "-Dvpn=$($settings['SWIM_VPN'])" `
        "-Doutput=com.harris.cinnato.outputs.MessageFileOutput" `
        "-Djson=true" `
        "-Dheaders=true" `
        "-Dmetrics=true" `
        -jar $jar
} finally {
    Pop-Location
}
