#!/bin/sh
# Public exposure probe: what the Cloudflare tunnel can reach, and nothing more
# (nas-feedops-sentinel/docs/public-access.md).
#
#   1. Host          the public listeners (edge 8450, 8451) publish no host
#                    port, and nas-cloudflared (if running) publishes none
#   2. Allowed       through the listeners, exactly as the tunnel reaches them
#                    (the edge certificate verified: local CA, name localhost):
#                    the dashboard answers; the sign-in realm's discovery
#                    document answers and names the public sign-in address
#   3. Refused       the admin console, the master realm, the account console,
#                    Keycloak's metrics/health, and every other service:
#                    all 404 at the edge, whatever a tunnel rule says
#   4. Tunnel        running exactly when public access is on; if running:
#                    read-only root, all capabilities dropped, no-new-privileges,
#                    non-root user, healthy, and every route Cloudflare sent it
#                    verifies the edge (no noTLSVerify)
#
# Runs from the host with the Docker CLI; requests go from a throwaway
# container on nas-network (the app image: Python, the local CA). Hostnames
# come from PUBLIC_DASHBOARD_URL / KEYCLOAK_PUBLIC_URL in either env file;
# unset, the listeners are still checked, with localhost as the host.
set -u

REPO=$(cd "$(dirname "$0")/../.." && pwd)
export MSYS_NO_PATHCONV=1
hostpath() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else echo "$1"; fi; }

FAILED=0
pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1"; FAILED=1; }
info() { echo "  INFO  $1"; }

envval() {  # the application's env file wins over the infrastructure's, as in compose
    v=""
    for f in "$REPO/instraclustr/.env" "$REPO/nas-feedops-sentinel/.env"; do
        [ -f "$f" ] || continue
        line=$(grep -E "^$1=" "$f" | tail -1 | cut -d= -f2- | tr -d '\r"'"'")
        [ -n "$line" ] && v=$line
    done
    echo "$v"
}
DASH_URL=$(envval PUBLIC_DASHBOARD_URL); AUTH_URL=$(envval KEYCLOAK_PUBLIC_URL)
DASH_HOST=${DASH_URL#https://}; DASH_HOST=${DASH_HOST:-localhost}
AUTH_HOST=${AUTH_URL#https://}; AUTH_HOST=${AUTH_HOST:-localhost}
# The sign-in address Keycloak is serving now: the public one while public
# access is on (KEYCLOAK_BROWSER_URL, scripts/Set-PublicAccess.ps1), else local.
BROWSER_URL=$(envval KEYCLOAK_BROWSER_URL)
ISSUER="${BROWSER_URL:-https://localhost:8081}/realms/nas-sentinel"

echo "Public exposure probe: dashboard=$DASH_HOST sign-in=$AUTH_HOST (public access $([ -n "$BROWSER_URL" ] && echo on || echo off))"

# ── 1. Host ──────────────────────────────────────────────────────────────────
echo "1. Nothing public is published on this machine"
published=$(docker port nas-edge 2>/dev/null)
if printf '%s\n' "$published" | grep -qE '^(8450|8451)/'; then fail "edge publishes a public listener on the host: $published"
else pass "edge publishes only its loopback consoles (8450/8451 are tunnel-only)"; fi
if docker inspect nas-cloudflared >/dev/null 2>&1; then
    if [ -z "$(docker port nas-cloudflared 2>/dev/null)" ]; then pass "nas-cloudflared publishes no port"
    else fail "nas-cloudflared publishes a port"; fi
fi

# ── 2 and 3. Through the listeners, as the tunnel reaches them ──────────────
echo "2. Allowed, and 3. refused, through the public listeners"
out=$(docker run --rm -i --network nas-network -v "$(hostpath "$REPO/nas-feedops-sentinel/tls/trust"):/ca:ro" \
    --entrypoint python nas-feedops-sentinel:latest - "$DASH_HOST" "$AUTH_HOST" "$ISSUER" <<'PY'
import http.client, json, socket, ssl, sys
dash, auth, issuer = sys.argv[1:4]
# Verified exactly as the tunnel's routes verify it: the local authority, and
# the edge certificate's name `localhost` (Origin Server Name + CA Pool).
ctx = ssl.create_default_context(cafile="/ca/edge-ca.crt")
class Edge(http.client.HTTPSConnection):
    def connect(self):
        self.sock = ctx.wrap_socket(socket.create_connection((self.host, self.port), self.timeout),
                                    server_hostname="localhost")
def get(port, path, host):
    try:
        conn = Edge("nas-edge", port, timeout=15)
        conn.request("GET", path, headers={"Host": host})
        r = conn.getresponse()
        return r.status, r.read(65536)
    except Exception as e:           # connection refused, certificate not verified etc.
        return f"error: {e}", b""
def check(ok, msg): print(("PASS" if ok else "FAIL") + " " + msg)

s, body = get(8450, "/_stcore/health", dash)
check(s == 200, f"dashboard answers on the public listener ({s})")
s, body = get(8451, "/realms/nas-sentinel/.well-known/openid-configuration", auth)
iss = json.loads(body).get("issuer") if s == 200 else None
check(s == 200, f"sign-in realm answers on the public listener ({s})")
check(iss == issuer, f"sign-in pages are served as {issuer} (issuer: {iss})")
for port, path, why in [
    (8451, "/admin/", "admin console"),
    (8451, "/admin/master/console/", "admin console (master)"),
    (8451, "/realms/master/.well-known/openid-configuration", "master realm"),
    (8451, "/realms/nas-sentinel/account", "account console"),
    (8451, "/metrics", "Keycloak metrics"),
    (8451, "/health/ready", "Keycloak health"),
    (8451, "/", "anything else on the sign-in host"),
]:
    s, _ = get(port, path, auth)
    check(s == 404, f"refused: {why} {path} ({s})")
PY
)
printf '%s\n' "$out" | while IFS= read -r line; do
    case "$line" in PASS*) echo "  PASS  ${line#PASS }" ;; FAIL*) echo "  FAIL  ${line#FAIL }" ;; *) echo "        $line" ;; esac
done
printf '%s\n' "$out" | grep -q '^FAIL' && FAILED=1
printf '%s\n' "$out" | grep -q '^PASS' || { fail "the probe container produced no result (is nas-network up?)"; }

# ── 4. The tunnel's own container ────────────────────────────────────────────
echo "4. The tunnel container"
running=$(docker inspect -f '{{.State.Running}}' nas-cloudflared 2>/dev/null)
if [ -n "$BROWSER_URL" ] && [ "$running" != true ]; then
    fail "public access is on (sign-in at $BROWSER_URL) but the tunnel is not running: nobody can reach it"
elif [ -z "$BROWSER_URL" ] && [ "$running" = true ]; then
    fail "the tunnel is running while public access is off: visitors reach a sign-in they cannot use"
fi
if [ "$running" = true ]; then
    [ "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' nas-cloudflared)" = true ] && pass "read-only root filesystem" || fail "writable root filesystem"
    docker inspect -f '{{.HostConfig.CapDrop}}' nas-cloudflared | grep -q ALL && pass "all capabilities dropped" || fail "capabilities not dropped"
    docker inspect -f '{{.HostConfig.SecurityOpt}}' nas-cloudflared | grep -q no-new-privileges && pass "no-new-privileges" || fail "no-new-privileges not set"
    [ "$(docker inspect -f '{{.Config.User}}' nas-cloudflared)" = "65532:65532" ] && pass "runs as a non-root user" || fail "not the non-root user"
    health=$(docker inspect -f '{{.State.Health.Status}}' nas-cloudflared 2>/dev/null)
    [ "$health" = healthy ] && pass "tunnel healthy (connected to Cloudflare)" || fail "tunnel not healthy ($health)"
    # The routes live in Cloudflare; the tunnel logs each configuration it receives.
    cfg=$(docker logs nas-cloudflared 2>&1 | grep 'Updated to new configuration' | tail -1)
    routes=$(printf '%s' "$cfg" | grep -o 'hostname' | wc -l)
    pools=$(printf '%s' "$cfg" | grep -o 'caPool[^,}]*edge-ca\.crt' | wc -l)
    if [ -z "$cfg" ]; then
        info "the tunnel has logged no route configuration yet"
    elif printf '%s' "$cfg" | grep -q 'noTLSVerify[^,}]*true'; then
        fail "a route skips verifying the edge: turn off TLS -> Disable TLS certificate verification"
    elif [ "$routes" -gt 0 ] && [ "$pools" -ge "$routes" ]; then
        pass "every route verifies the edge ($pools of $routes with the local CA pool)"
    else
        fail "a route does not verify the edge: set TLS -> CA Pool /etc/cloudflared/trust/edge-ca.crt ($pools of $routes)"
    fi
else
    info "nas-cloudflared is not running: public access is off (launch with -Public)"
fi

echo
if [ "$FAILED" -eq 0 ]; then echo "RESULT: only the dashboard and the sign-in pages are reachable through the tunnel"; else echo "RESULT: FAILED -- see above"; fi
exit "$FAILED"
