#!/bin/sh
# Rotation probe: identities and Vault logins keep working as they rotate,
# which is the failure mode a stack only reaches after it has been up for
# a while -- an SVID lives one hour, a Vault token at most one.
#
#   1. Every service   its current SVID names its own SPIFFE ID, chains to the
#                      offline root, and is inside its lifetime; its Vault
#                      Agent has authenticated and reports no failed login.
#                      Up for over an hour: it has re-authenticated since.
#   2. Rotation, live  on the api identity, the SVID lifetime is cut to 90 s
#                      (spire-server entry update), then: the SVID is
#                      re-issued, renewed again at half-life, and spiffe-helper
#                      rewrites the files each time; api's Vault Agent,
#                      restarted, logs in with the ROTATED certificate and
#                      renders; the api service stays healthy throughout.
#   3. Restore         the entry goes back to the server default (1 h), always
#                      -- on success, failure or Ctrl+C -- and the probe
#                      checks the next SVID has the full lifetime again.
#
# Runs from the host with the Docker CLI against the live stack; about four
# minutes. Prints PASS/FAIL/INFO per check; exits 1 if any check fails.
set -u

REPO=$(cd "$(dirname "$0")/../.." && pwd)
PROJECT="${PROBE_APP_PROJECT:-nas-feedops-sentinel}"
SERVICES="${PROBE_SERVICES:-api dashboard sentinel temporal-worker}"
TRUST_DOMAIN="sentinelfaa.internal"
PARENT_ID="spiffe://$TRUST_DOMAIN/agent/nas-host"
SHORT_TTL=90
IMAGE="postgres:16"                              # OpenSSL 3, already in the stack
SOCK="/tmp/spire-server/private/api.sock"
export MSYS_NO_PATHCONV=1

hostpath() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else echo "$1"; fi; }
TRUST="$(hostpath "$REPO/instraclustr/certs/trust")"

FAILED=0
pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1"; FAILED=1; }
info() { echo "  INFO  $1"; }

spire() { docker exec spire-server /opt/spire/bin/spire-server "$@" -socketPath "$SOCK"; }

# svid <service> <what>: read the service's current SVID from its memory-only
# volume (uid 999, read-only). <what>: serial | seconds-left | check
svid() {
    docker run --rm --network none --user 999:999 \
        -v "${PROJECT}_$1-svid:/s:ro" -v "$TRUST:/t:ro" "$IMAGE" sh -c '
        f=/s/svid.pem
        [ -s "$f" ] || { echo "none"; exit 0; }
        awk "BEGIN{n=0} /BEGIN CERT/{n++} {print > (\"/tmp/c\" n \".pem\")}" "$f"
        case "$1" in
          serial) openssl x509 -in /tmp/c1.pem -noout -serial | cut -d= -f2 ;;
          seconds-left)
            end=$(date -d "$(openssl x509 -in /tmp/c1.pem -noout -enddate | cut -d= -f2)" +%s)
            echo $(( end - $(date +%s) )) ;;
          check)
            cat /tmp/c[2-9].pem > /tmp/chain.pem 2>/dev/null
            uri=$(openssl x509 -in /tmp/c1.pem -noout -ext subjectAltName | grep -o "URI:[^,]*" | cut -d: -f2-)
            if openssl verify -x509_strict -CAfile /t/root-ca.crt -untrusted /tmp/chain.pem /tmp/c1.pem >/dev/null 2>&1
            then echo "chain-ok $uri"; else echo "chain-bad $uri"; fi ;;
        esac' sh "$2" 2>/dev/null
}

wait_for() {  # wait_for <seconds> <command...>: until the command succeeds
    limit=$1; shift; i=0
    while [ "$i" -lt "$limit" ]; do "$@" && return 0; sleep 3; i=$((i + 3)); done
    return 1
}

echo "Rotation probe: project=$PROJECT"
[ -f "$REPO/instraclustr/certs/trust/root-ca.crt" ] || { echo "ERROR: no instraclustr/certs/trust/root-ca.crt" >&2; exit 2; }
docker inspect spire-server >/dev/null 2>&1 || { echo "ERROR: spire-server is not running" >&2; exit 2; }

# ── 1. Every service ─────────────────────────────────────────────────────────
echo "1. Every service"
for svc in $SERVICES; do
    got=$(svid "$svc" check)
    want="spiffe://$TRUST_DOMAIN/workload/$svc"
    case "$got" in
        "chain-ok $want") pass "$svc: SVID is $want, chained to the offline root" ;;
        none)             fail "$svc: no SVID on its volume" ;;
        *)                fail "$svc: SVID wrong or not chained to the root ($got)" ;;
    esac
    left=$(svid "$svc" seconds-left)
    if [ "${left:-0}" -gt 0 ] 2>/dev/null; then pass "$svc: SVID valid for another ${left}s"
    else fail "$svc: SVID expired or unreadable"; fi

    a="nas-$svc-vault-agent"
    started=$(docker inspect -f '{{.State.StartedAt}}' "$a" 2>/dev/null)
    if [ -z "$started" ]; then fail "$a: no such container"; continue; fi
    logs=$(docker logs --since "$started" "$a" 2>&1)
    auths=$(printf '%s\n' "$logs" | grep -c "authentication successful")
    errs=$(printf '%s\n' "$logs" | grep -c "error authenticating")
    if [ "$auths" -ge 1 ] && [ "$errs" -eq 0 ]; then pass "$a: authenticated ($auths), no failed login"
    else fail "$a: $auths successful, $errs failed logins since it started"; fi
    uptime=$(( $(date +%s) - $(date -d "$started" +%s 2>/dev/null || echo "$(date +%s)") ))
    if [ "$uptime" -gt 3700 ]; then
        if [ "$auths" -ge 2 ]; then pass "$a: re-authenticated after its first token (up ${uptime}s)"
        else fail "$a: up ${uptime}s but never re-authenticated"; fi
    else
        info "$a: up ${uptime}s; its first re-authentication is due after an hour"
    fi
done

# ── 2. Rotation, live, on the api identity ───────────────────────────────────
echo "2. Rotation on the api identity (lifetime ${SHORT_TTL}s, then restored)"
API_ID="spiffe://$TRUST_DOMAIN/workload/api"
entry_id=$(spire entry show -spiffeID "$API_ID" -output json 2>/dev/null | tr -d '\n ' | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
[ -n "$entry_id" ] || { fail "no SPIRE entry for $API_ID"; echo "RESULT: FAILED"; exit 1; }

# set_ttl <seconds>: `entry update` replaces the whole entry, so every field
# goes back exactly as spire-entries.yaml declares it (the DNS name is the CN
# Vault names the login after); 0 is the server default.
set_ttl() {
    spire entry update -entryID "$entry_id" -parentID "$PARENT_ID" -spiffeID "$API_ID" \
        -selector "docker:label:com.sentinelfaa.identity:api" -dns "api.sentinelfaa.invalid" \
        -x509SVIDTTL "$1" >/dev/null 2>&1
}
restored=0
restore() {
    [ "$restored" = 1 ] && return
    restored=1
    if set_ttl 0; then echo "  ....  api entry back to the server default lifetime"
    else echo "  FAIL  COULD NOT RESTORE the api entry: run spire-server entry update by hand"; FAILED=1; fi
}
trap 'restore' EXIT
trap 'restore; exit 130' INT TERM

probe_start=$(date -u +%Y-%m-%dT%H:%M:%SZ)
before=$(svid api serial)
if set_ttl "$SHORT_TTL"; then pass "api entry lifetime set to ${SHORT_TTL}s"
else fail "could not update the api entry"; fi

changed_from() { now=$(svid api serial); [ -n "$now" ] && [ "$now" != "none" ] && [ "$now" != "$1" ]; }
if wait_for 150 changed_from "$before"; then
    first=$(svid api serial)
    left=$(svid api seconds-left)
    pass "SVID re-issued (serial ${before} -> ${first})"
    if [ "${left:-9999}" -le "$SHORT_TTL" ] 2>/dev/null; then pass "new SVID lives ${left}s (<= ${SHORT_TTL}s)"
    else fail "new SVID lives ${left}s, not the short lifetime"; fi
    if wait_for 120 changed_from "$first"; then pass "SVID renewed again at half-life ($(svid api serial))"
    else fail "SVID was not renewed within 120s"; fi
else
    fail "SVID was not re-issued within 150s"
fi
helper_errs=$(docker logs --since "$probe_start" "nas-api-spiffe" 2>&1 | grep -ciE "permission denied|unable to dump|error")
if [ "$helper_errs" -eq 0 ]; then pass "spiffe-helper rewrote the files on every rotation, no error"
else fail "spiffe-helper logged $helper_errs error(s) while rotating"; fi
case "$(svid api check)" in
    "chain-ok $API_ID") pass "rotated SVID still chains to the offline root" ;;
    *) fail "rotated SVID does not chain to the root" ;;
esac

docker restart nas-api-vault-agent >/dev/null
agent_healthy() { [ "$(docker inspect -f '{{.State.Health.Status}}' nas-api-vault-agent 2>/dev/null)" = healthy ]; }
restarted=$(docker inspect -f '{{.State.StartedAt}}' nas-api-vault-agent)
if wait_for 90 agent_healthy; then
    alogs=$(docker logs --since "$restarted" nas-api-vault-agent 2>&1)
    if printf '%s\n' "$alogs" | grep -q "authentication successful" \
       && ! printf '%s\n' "$alogs" | grep -q "error authenticating"; then
        pass "api Vault Agent logged in with the rotated SVID"
    else
        fail "api Vault Agent did not log in cleanly after the rotation"
    fi
    # The template engine logs "rendered" only when the content changes, and
    # api's secrets did not -- so ask Vault instead: its audit log (stdout)
    # must show api's two secrets read, as cert-api, since the restart.
    reads_ok=1
    for path in secret/data/postgres/app secret/data/sentinel/config; do
        wait_for 30 sh -c "docker logs --since '$restarted' vault 2>&1 | grep '\"type\":\"response\"' | grep '\"path\":\"$path\"' | grep -q '\"display_name\":\"cert-api\"'" \
            || reads_ok=0
    done
    if [ "$reads_ok" = 1 ]; then pass "and fetched api's secrets with it (Vault audit: cert-api read both)"
    else fail "Vault's audit log shows no read of api's secrets by cert-api since the restart"; fi
    if printf '%s\n' "$alogs" | grep -q "\[ERROR\]"; then fail "api Vault Agent logged errors after the rotation"
    else pass "api Vault Agent: no errors after the rotation"; fi
else
    fail "api Vault Agent not healthy within 90s of its restart"
fi
if [ "$(docker inspect -f '{{.State.Health.Status}}' nas-api 2>/dev/null)" = healthy ]; then pass "the api service stayed healthy"
else fail "the api service is not healthy"; fi

# ── 3. Restore ────────────────────────────────────────────────────────────────
echo "3. Restore"
restore
ttl=$(spire entry show -spiffeID "$API_ID" -output json 2>/dev/null | tr -d '\n ' | grep -o '"x509_svid_ttl":[0-9]*' | cut -d: -f2)
if [ "${ttl:-x}" = 0 ]; then pass "api entry is on the server default lifetime again"
else fail "api entry lifetime is '${ttl}', not the default"; fi
long_again() { l=$(svid api seconds-left); [ "${l:-0}" -gt 3000 ] 2>/dev/null; }
if wait_for 150 long_again; then pass "api holds a full-lifetime SVID again ($(svid api seconds-left)s)"
else fail "api did not get a full-lifetime SVID back within 150s"; fi

echo
if [ "$FAILED" -eq 0 ]; then echo "RESULT: rotation keeps every identity and login working"; else echo "RESULT: FAILED -- see above"; fi
exit "$FAILED"
