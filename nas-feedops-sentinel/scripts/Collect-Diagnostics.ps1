# Collect-Diagnostics.ps1
# Gathers container state and recent logs into one file (diagnostics.txt next to
# this repo's docker-compose.yml) so it can be read in one pass instead of
# pasting output piecemeal. Does NOT dump container environments (docker
# inspect) -- those contain passwords and tokens.
$ErrorActionPreference = 'Continue'
$appDir = Split-Path -Parent $PSScriptRoot
$out = Join-Path $appDir 'diagnostics.txt'

function Section($title, [scriptblock]$cmd) {
    "`n===== $title =====" | Out-File $out -Append -Encoding utf8
    try { & $cmd 2>&1 | Out-String -Width 300 | Out-File $out -Append -Encoding utf8 }
    catch { "ERROR: $_" | Out-File $out -Append -Encoding utf8 }
}

"Collected $(Get-Date -Format o)" | Out-File $out -Encoding utf8
Section 'docker ps -a' { docker ps -a --format "table {{.Names}}\t{{.Status}}\t{{.Image}}" }
Section 'docker volumes' { docker volume ls --format "{{.Name}}" }
Section 'docker networks' { docker network ls --format "{{.Name}}" }
Section 'docker engine' { docker version --format 'server {{.Server.Version}}' }
Section 'app image build time' { docker image inspect nas-feedops-sentinel:latest --format '{{.Created}}' }
Section 'git branch / last commit' { git -C $appDir rev-parse --abbrev-ref HEAD; git -C $appDir log --oneline -1 }
Section 'compose: temporal env var check' { Select-String -Path (Join-Path $appDir 'docker-compose.yml') -Pattern 'DYNAMIC_CONFIG' }

$containers = 'nas-temporal','nas-temporal-postgresql','nas-api','nas-temporal-worker',
              'nas-sentinel','nas-dashboard','nas-temporal-ui','nas-grafana','nas-prometheus',
              'nas-keycloak','vault','nas-litellm','nas-pgbouncer','postgres'
foreach ($c in $containers) {
    Section "logs: $c (last 60 lines)" { docker logs $c --tail 60 }
}
Write-Host "Wrote $out" -ForegroundColor Green
