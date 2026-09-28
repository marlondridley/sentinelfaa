#!/bin/sh
# Negative access probe: every unauthorized path to a secret is refused, and
# refused cryptographically -- by attestation, certificate verification or
# Vault policy -- not by an address being hard to guess.
#
#   1. Un-attested container       a Workload API caller with no identity
#                                  label, or an unregistered one, is issued no
#                                  SVID at all
#   2. Cross-service escalation    a real SVID (api) cannot read another
#                                  class's secrets, write any, author policy,
#                                  mint a broader token, or log in as another
#                                  service's Vault role
#   3. No / forged client cert     Vault's cert login refuses a request with no
#                                  certificate, and a self-made certificate
#                                  claiming a real SPIFFE ID
#   4. Plain HTTP transport        Vault answers no plaintext request, and a
#                                  client that does not trust the offline root
#                                  cannot complete TLS
# Each denial is paired with a positive control (the same caller doing what it
# IS allowed to), so a pass means "refused", not "broken".
#
# Runs from the host with the Docker CLI, against any stack. Defaults target
# the live one; the smoke harness overrides:
#   PROBE_NETWORK        nas-network
#   PROBE_SOCKET_VOLUME  nas-spire-agent-socket        (spire-agent's Workload API)
#   PROBE_TRUST_DIR      instraclustr/certs/trust      (the offline root, public)
#   PROBE_VAULT_ADDR     https://vault:8200
# Probe 2 claims the api identity with the same label api's own sidecar
# carries -- which anyone with Docker API access can do (instraclustr/README.md,
# "Threat model"); the probe proves what that identity still cannot do.
#
# Prints PASS/FAIL per check; exits 1 if any check fails. Never prints a
# secret value.
set -u

REPO=$(cd "$(dirname "$0")/../.." && pwd)
NETWORK="${PROBE_NETWORK:-nas-network}"
SOCKET_VOLUME="${PROBE_SOCKET_VOLUME:-nas-spire-agent-socket}"
TRUST_DIR="${PROBE_TRUST_DIR:-$REPO/instraclustr/certs/trust}"
VAULT_ADDR_IN="${PROBE_VAULT_ADDR:-https://vault:8200}"
VAULT_IMAGE="hashicorp/vault:1.15.2@sha256:6b4e5dadf082cb3587930ced09b52851c5ae9f8d1c7df31ed39f0085a3029f63"
HELPER_IMAGE="ghcr.io/spiffe/spiffe-helper:0.10.0@sha256:95f5000aa1e0aadbfde20d66f1ad25733034162825aa51cc922ac78365bb8ee8"
OPENSSL_IMAGE="postgres:16"          # already in the stack; ships OpenSSL 3
TAG="sentinel-probe-$$"

export MSYS_NO_PATHCONV=1            # Git Bash on Windows: leave container paths alone
hostpath() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else echo "$1"; fi; }
TRUST_MOUNT="$(hostpath "$TRUST_DIR"):/trust:ro"
HELPER_CONF="$(hostpath "$REPO/instraclustr/vault-agent"):/etc/spiffe-helper:ro"

FAILED=0
pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1"; FAILED=1; }
# expect <description> <grep -E pattern> <output>: the output shows the refusal.
expect() { if printf '%s' "$3" | grep -qE "$2"; then pass "$1"; else fail "$1"; printf '%s\n' "$3" | sed 's/^/        | /' | head -8; fi; }

cleanup() {
    docker rm -f "$TAG-nolabel" "$TAG-unregistered" "$TAG-api" >/dev/null 2>&1
    docker volume rm "$TAG-svid" "$TAG-forged" >/dev/null 2>&1
}
trap cleanup EXIT INT TERM

[ -f "$TRUST_DIR/root-ca.crt" ] || { echo "ERROR: no $TRUST_DIR/root-ca.crt" >&2; exit 2; }
docker network inspect "$NETWORK" >/dev/null 2>&1 || { echo "ERROR: no Docker network $NETWORK" >&2; exit 2; }
docker volume inspect "$SOCKET_VOLUME" >/dev/null 2>&1 || { echo "ERROR: no volume $SOCKET_VOLUME" >&2; exit 2; }

# A memory-only volume owned by the application uid, like the real sidecars'.
docker volume create --driver local --opt type=tmpfs --opt device=tmpfs \
    --opt o=uid=999,gid=999,mode=0700 "$TAG-svid" >/dev/null

helper() {  # helper <name> [label]: a spiffe-helper exactly like a sidecar's
    label_args=""
    [ -n "${2:-}" ] && label_args="--label com.sentinelfaa.identity=$2"
    # shellcheck disable=SC2086
    docker run -d --name "$1" --network "$NETWORK" --user 999:999 $label_args \
        -v "$SOCKET_VOLUME:/tmp/spire-agent/public:ro" -v "$HELPER_CONF" \
        -v "$TAG-svid:/run/spiffe" "$HELPER_IMAGE" -config /etc/spiffe-helper/spiffe-helper.conf >/dev/null
}
issued() { docker logs "$1" 2>&1 | grep -q "X.509 certificates updated"; }
wait_logs() {  # wait_logs <container> <pattern> <seconds>
    i=0; while [ "$i" -lt "$3" ]; do docker logs "$1" 2>&1 | grep -qE "$2" && return 0; sleep 1; i=$((i + 1)); done; return 1
}

vault_as() {  # vault_as <script>: sh in the Vault CLI image, SVID volume mounted, uid 999
    docker run --rm --network "$NETWORK" --user 999:999 -v "$TRUST_MOUNT" -v "$TAG-svid:/run/spiffe:ro" \
        -e VAULT_ADDR="$VAULT_ADDR_IN" -e VAULT_CACERT=/trust/root-ca.crt -e HOME=/tmp \
        --entrypoint sh "$VAULT_IMAGE" -c "$1" 2>&1
}

echo "Negative access probe: network=$NETWORK vault=$VAULT_ADDR_IN"

# -- 1. Un-attested container --------------------------------------------------
echo "1. Un-attested container"
helper "$TAG-nolabel"
helper "$TAG-unregistered" "rogue-not-in-spire-entries"
wait_logs "$TAG-nolabel" "no identity issued|certificates updated" 20
wait_logs "$TAG-unregistered" "no identity issued|certificates updated" 20
for c in nolabel unregistered; do
    if issued "$TAG-$c"; then
        fail "$c: SPIRE issued an SVID to a container it should not know"
    elif docker logs "$TAG-$c" 2>&1 | grep -q "no identity issued"; then
        pass "$c: spire-agent refused (\"no identity issued\")"
    else
        fail "$c: no refusal seen from spire-agent within 20 s"
        docker logs "$TAG-$c" 2>&1 | tail -3 | sed 's/^/        | /'
    fi
done
docker rm -f "$TAG-nolabel" "$TAG-unregistered" >/dev/null 2>&1

# -- 2. Cross-service privilege escalation ------------------------------------
echo "2. Cross-service privilege escalation (as api's own SPIFFE identity)"
helper "$TAG-api" api
if ! wait_logs "$TAG-api" "certificates updated" 30; then
    fail "control: api identity was not issued (is spire-entries.yaml applied?)"
else
    out=$(vault_as '
        T=$(vault login -method=cert -client-cert=/run/spiffe/svid.pem -client-key=/run/spiffe/svid_key.pem -token-only) || exit 1
        export VAULT_TOKEN=$T
        echo "== identity"; vault token lookup -format=json | tr -d "\n " | grep -oE "\"(cert_name|common_name)\":\"[^\"]*\"|\"policies\":\[[^]]*\]"
        echo "== control: read own class secret"; vault kv get -field=username secret/postgres/app >/dev/null && echo CONTROL_READ_OK
        echo "== read migrate-class secret"; vault kv get secret/postgres/credentials
        echo "== write a secret"; vault kv put secret/sentinel/config LITELLM_API_KEY=probe
        echo "== author a policy"; echo "path \"*\" { capabilities = [\"sudo\"] }" | vault policy write probe-escalation -
        echo "== mint a broader token"; vault token create -policy=sentinel-migrate
        echo "== enable an auth method"; vault auth enable -path=probe userpass
        echo "== end"')
    # Vault matched the SVID to the api role alone, and the identity alias its
    # audit log records is the SVID's verified CN.
    expect "control: api's SVID matched the api role" '"cert_name":"api"' "$out"
    expect "control: audit identity is api.sentinelfaa.invalid" '"common_name":"api.sentinelfaa.invalid"' "$out"
    expect "control: api reads its own class's secret" 'CONTROL_READ_OK' "$out"
    expect "api policies are exactly default + sentinel-app" \
        '"policies":\["default","sentinel-app"\]' "$out"
    for step in "read migrate-class secret" "write a secret" "author a policy" \
                "mint a broader token" "enable an auth method"; do
        section=$(printf '%s\n' "$out" | sed -n "/^== $step\$/,/^== /p")
        expect "denied: $step" 'permission denied|Code: 403' "$section"
    done
    as_migrate=$(vault_as 'vault login -method=cert -client-cert=/run/spiffe/svid.pem -client-key=/run/spiffe/svid_key.pem name=migrate')
    expect "denied: api's SVID logging in as the migrate role" \
        'invalid certificate|no chain matching|permission denied|Code: 400|Code: 403' "$as_migrate"
fi
docker rm -f "$TAG-api" >/dev/null 2>&1

# -- 3. Unauthenticated / missing or forged client certificate -----------------
echo "3. Unauthenticated: no client certificate, forged client certificate"
out=$(vault_as 'vault write auth/cert/login name=api')
expect "denied: cert login with no client certificate" 'client certificate must be supplied|Code: 400' "$out"

# A self-made CA and a leaf claiming migrate's exact SPIFFE ID and CN: all the
# right names, the wrong root.
docker volume create "$TAG-forged" >/dev/null
docker run --rm -v "$TAG-forged:/f" "$OPENSSL_IMAGE" sh -c '
    set -e; cd /f
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout ca.key -out ca.crt \
        -subj "/O=sentinelfaa/CN=sentinelfaa Root CA" -days 1 2>/dev/null
    openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout leaf.key -out leaf.csr \
        -subj "/CN=migrate.sentinelfaa.invalid" 2>/dev/null
    printf "subjectAltName=URI:spiffe://sentinelfaa.internal/workload/migrate,DNS:migrate.sentinelfaa.invalid\n" > ext
    openssl x509 -req -in leaf.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 1 -extfile ext -out leaf.crt 2>/dev/null
    cat leaf.crt ca.crt > chain.crt; chmod 644 *'
out=$(docker run --rm --network "$NETWORK" -v "$TRUST_MOUNT" -v "$TAG-forged:/f:ro" \
    -e VAULT_ADDR="$VAULT_ADDR_IN" -e VAULT_CACERT=/trust/root-ca.crt -e HOME=/tmp \
    "$VAULT_IMAGE" vault login -method=cert -client-cert=/f/chain.crt -client-key=/f/leaf.key 2>&1)
expect "denied: forged certificate claiming spiffe://.../workload/migrate" \
    'invalid certificate|no chain matching|Code: 400|Code: 403' "$out"

# -- 4. Plain HTTP transport ------------------------------------------------
echo "4. Plain HTTP transport"
http_host=$(printf '%s' "$VAULT_ADDR_IN" | sed -E 's#^https://##')
out=$(docker run --rm --network "$NETWORK" --entrypoint sh "$VAULT_IMAGE" -c \
    "wget -S -O - http://$http_host/v1/sys/health 2>&1; echo WGET_EXIT=\$?")
expect "refused: plaintext HTTP gets no Vault response" 'HTTP/1.[01] 400|400 Bad Request' "$out"
if printf '%s' "$out" | grep -q '"initialized"'; then fail "plaintext HTTP returned Vault data"; else pass "no Vault data over plaintext HTTP"; fi
out=$(docker run --rm --network "$NETWORK" -e VAULT_ADDR="$VAULT_ADDR_IN" "$VAULT_IMAGE" vault status 2>&1)
expect "refused: TLS without trusting the offline root" 'certificate signed by unknown authority|x509' "$out"
out=$(docker run --rm --network "$NETWORK" -v "$TRUST_MOUNT" -e VAULT_ADDR="$VAULT_ADDR_IN" \
    -e VAULT_CACERT=/trust/root-ca.crt "$VAULT_IMAGE" vault status -format=json 2>&1)
expect "control: TLS trusting the offline root reaches Vault" '"initialized": *true' "$out"

echo
if [ "$FAILED" -eq 0 ]; then echo "RESULT: all unauthorized paths refused"; else echo "RESULT: FAILED -- see above"; fi
exit "$FAILED"
