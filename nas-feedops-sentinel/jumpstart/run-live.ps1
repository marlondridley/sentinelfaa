<#
.SYNOPSIS
    Launches the FAA SWIM SCDS Jumpstart Kit against the live broker and writes
    each received message into the folder the Sentinel ingest watcher observes.

.DESCRIPTION
    This replaces the containerized jumpstart service that used to live in
    docker-compose.yml. The kit ships a prebuilt fat jar and its own launcher,
    so building an image for it added a Maven/JDK-25 step that bought nothing.

    What this script does:
      1. Reads the SWIM_* credentials from ..\.env. The password reaches the
         kit in a private, owner-only config file (-Dconfig.file) that is
         deleted when the kit exits, never as a -D argument: an argument is
         visible in the process list and is recorded by command-line process
         auditing (Windows event 4688, Sysmon event 1, EDR).
      2. Checks that a Java 25+ runtime is present -- the shipped jar is
         compiled to bytecode major 69 and will not load on anything older.
      3. Builds lib\swim-ingest-output.jar from
         src\main\java\...\IngestFolderFileOutput.java against the kit's jar
         (needs a JDK, i.e. javac, not just a JRE) when it is missing or older
         than its source.
      4. Launches the kit with that class as its output. The stock
         MessageFileOutput is NOT used: the kit acknowledges a message when its
         output returns, and that output logs a failed write and returns, so a
         message could be acknowledged with nothing on disk. IngestFolderFileOutput
         writes one durable record per message (fsync, atomic rename) and halts
         the JVM before returning if it cannot, so the broker redelivers.
      5. Restarts the kit when it exits, with a growing pause, because it now
         exits on a failed write and on a lost broker connection. Nothing is
         lost by that: what it had not acknowledged, the broker sends again.

    -Djson=true -Dheaders=true are NOT optional. The record carries the JMS
    properties (the SWIM message identity) and the payload converted to JSON;
    without them the ingest quarantines every message.

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
    [string]$OutputDir,

    # Run the kit once and return its exit code instead of restarting it.
    [switch]$NoRestart
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
$javaCmd = Get-Command java -ErrorAction SilentlyContinue
if (-not $javaCmd) {
    throw "No 'java' on PATH. The shipped jar needs a Java 25+ runtime (e.g. Temurin 25)."
}
# java -version writes to stderr. Under 'Stop', Windows PowerShell 5.1 turns
# redirected stderr into a terminating error, so the check threw even with
# Java 25 installed and the launcher's window closed before the kit started.
$ErrorActionPreference = 'Continue'
$versionOutput = & $javaCmd.Source -version 2>&1 | Out-String
$ErrorActionPreference = 'Stop'
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
# The kit's own log (messages.log, from its logging config) goes to <cwd>\log\, so the
# kit runs from the parent of OutputDir to keep it beside the records. The records
# themselves go wherever -DoutputDir says.
$launchDir = Split-Path -Parent $OutputDir

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$jar = Join-Path $PSScriptRoot 'lib\jumpstart-jar-with-dependencies.jar'
if (-not (Test-Path $jar)) {
    throw "Prebuilt jar not found at $jar."
}

# --- Build the ingest output ----------------------------------------------
# Compiled here rather than shipped: it is our source, and the jar is not tracked.
$outputSource = Join-Path $PSScriptRoot 'src\main\java\com\harris\cinnato\outputs\IngestFolderFileOutput.java'
$outputJar    = Join-Path $PSScriptRoot 'lib\swim-ingest-output.jar'
if (-not (Test-Path $outputSource)) {
    throw "Cannot find $outputSource."
}
if (-not (Test-Path $outputJar) -or (Get-Item $outputSource).LastWriteTimeUtc -gt (Get-Item $outputJar).LastWriteTimeUtc) {
    $javac  = Get-Command javac -ErrorAction SilentlyContinue
    $jarCmd = Get-Command jar -ErrorAction SilentlyContinue
    if (-not $javac -or -not $jarCmd) {
        throw "Building the ingest output needs a JDK ('javac' and 'jar' on PATH), not just a JRE. Install Temurin 25 (JDK)."
    }
    $build = Join-Path ([IO.Path]::GetTempPath()) ("swim-ingest-build-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $build | Out-Null
    try {
        # javac and jar report on stderr; see the java -version note above.
        $ErrorActionPreference = 'Continue'
        & $javac.Source -Xlint:all -cp $jar -d $build $outputSource
        $compiled = $LASTEXITCODE
        if ($compiled -eq 0) {
            & $jarCmd.Source cf $outputJar -C $build .
            $compiled = $LASTEXITCODE
        }
        $ErrorActionPreference = 'Stop'
        if ($compiled -ne 0) { throw "Building $outputJar failed (exit code $compiled)." }
    } finally {
        $ErrorActionPreference = 'Stop'
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $build
    }
    Write-Host "Built $outputJar" -ForegroundColor DarkGray
}
$classpath = $jar + [IO.Path]::PathSeparator + $outputJar

Write-Host ""
Write-Host "Broker    : $($settings['SWIM_PROVIDER_URL'])"
Write-Host "Queue     : $($settings['SWIM_QUEUE'])"
Write-Host "VPN       : $($settings['SWIM_VPN'])"
Write-Host "User      : $($settings['SWIM_USERNAME'])"
Write-Host "Writing to: $OutputDir"
Write-Host ""
Write-Host "Ctrl+C to stop. The sentinel container picks these up automatically." -ForegroundColor DarkGray
Write-Host ""

# The kit reads its settings with Typesafe Config, and -Dconfig.file names the file
# it loads as its application config, so the password goes there instead of on the
# command line. The file is created empty, restricted to this user, and only then
# given the secret; it is removed when the kit exits.
$secretConf = Join-Path ([IO.Path]::GetTempPath()) ("swim-kit-" + [guid]::NewGuid().ToString('N') + ".conf")

$exitCode = 0
Push-Location $launchDir
try {
    New-Item -ItemType File -Path $secretConf | Out-Null
    & icacls $secretConf /inheritance:r /grant:r "$($env:USERNAME):(F)" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not restrict $secretConf to the current user; refusing to write the SWIM password to it." }
    # A HOCON quoted string: backslash and double quote are the only characters to escape.
    $escaped = $settings['SWIM_PASSWORD'].Replace('\', '\\').Replace('"', '\"')
    [IO.File]::WriteAllText($secretConf, "password = `"$escaped`"`n", (New-Object Text.UTF8Encoding($false)))

    $delay = 2
    while ($true) {
        $started = Get-Date
        & java `
            "-Dconfig.file=$secretConf" `
            "-DproviderUrl=$($settings['SWIM_PROVIDER_URL'])" `
            "-Dqueue=$($settings['SWIM_QUEUE'])" `
            "-DconnectionFactory=$($settings['SWIM_CONNECTION_FACTORY'])" `
            "-Dusername=$($settings['SWIM_USERNAME'])" `
            "-Dvpn=$($settings['SWIM_VPN'])" `
            "-Doutput=com.harris.cinnato.outputs.IngestFolderFileOutput" `
            "-DoutputDir=$OutputDir" `
            "-Djson=true" `
            "-Dheaders=true" `
            "-Dmetrics=true" `
            -cp $classpath com.harris.cinnato.Consumer
        $exitCode = $LASTEXITCODE
        if ($NoRestart) { break }

        $ranFor = [int]((Get-Date) - $started).TotalSeconds
        if ($ranFor -gt 300) { $delay = 2 }     # it was healthy for a while: start the backoff over
        Write-Warning "The kit stopped (exit code $exitCode) after $ranFor s. Restarting in $delay s; the broker redelivers whatever it had not acknowledged."
        Start-Sleep -Seconds $delay
        $delay = [Math]::Min($delay * 2, 60)
    }
} finally {
    Remove-Item -Force -ErrorAction SilentlyContinue $secretConf
    Pop-Location
}
exit $exitCode
