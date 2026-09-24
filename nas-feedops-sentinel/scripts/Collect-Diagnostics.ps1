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

$kcProbe = @'
import json, urllib.request, urllib.error
url = 'http://keycloak:8080/realms/nas-sentinel/.well-known/openid-configuration'
try:
    with urllib.request.urlopen(url, timeout=10) as r:
        d = json.load(r)
        print('HTTP', r.status, url)
        for k in ('issuer', 'authorization_endpoint', 'token_endpoint'):
            print(' ', k, '=', d.get(k))
except urllib.error.HTTPError as e:
    print('HTTP', e.code, url, '--', e.reason)
except Exception as e:
    print('FAILED', url, '--', type(e).__name__, e)
try:
    urllib.request.urlopen('http://keycloak:8080/realms/master', timeout=10)
    print('keycloak itself reachable (the master realm answers)')
except Exception as e:
    print('keycloak itself NOT reachable --', type(e).__name__, e)
'@
Section 'keycloak discovery, fetched from inside nas-dashboard' { docker exec nas-dashboard python -c $kcProbe }

$sqlEvents = 'PGPASSWORD=$POSTGRES_PASSWORD psql -U $POSTGRES_USER -d $POSTGRES_DB -c ' +
             [char]39 + 'select service_name, count(*) as events, max(event_time_utc) as newest_event, max(received_time_utc) as newest_received from nas_events group by 1 order by 1' + [char]39
$sqlRest = 'PGPASSWORD=$POSTGRES_PASSWORD psql -U $POSTGRES_USER -d $POSTGRES_DB -c ' +
           [char]39 + 'select (select count(*) from nas_events) as events, (select count(*) from alert_events) as alerts, (select count(*) from staged_actions) as staged_actions, (select count(*) from operational_tickets) as tickets, (select count(*) from ingest_log) as files_ingested' + [char]39
Section 'database: events by feed' { docker exec postgres sh -c $sqlEvents }
Section 'database: totals' { docker exec postgres sh -c $sqlRest }
Section 'ingest folder on disk' { Get-ChildItem -Recurse -File (Join-Path $appDir 'data') -ErrorAction SilentlyContinue | Group-Object { $_.Directory.FullName.Replace($appDir,'') } | Select-Object Count, Name }

$containers = 'nas-temporal','nas-temporal-postgresql','nas-api','nas-temporal-worker',
              'nas-sentinel','nas-dashboard','nas-temporal-ui','nas-grafana','nas-prometheus',
              'nas-keycloak','vault','nas-litellm','nas-pgbouncer','postgres'
foreach ($c in $containers) {
    Section "logs: $c (last 60 lines)" { docker logs $c --tail 60 }
}
Write-Host "Wrote $out" -ForegroundColor Green
