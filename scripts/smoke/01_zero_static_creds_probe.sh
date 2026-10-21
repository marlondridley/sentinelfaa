#!/bin/sh
# Zero static credentials probe: no application container holds a secret it
# was handed at deploy time, and nothing credential-shaped for the
# application sits on disk.
#
#   1. Environment   no password, key, Vault token or Vault route in any
#                    application container's environment -- by name, and by
#                    value shape (LiteLLM "sk-", Vault "hvs.")
#   2. Mounts        each service mounts exactly one secret source, its own
#                    Vault Agent file, read-only, from a memory-only volume;
#                    no host path holding Vault, PKI or AppRole material
#   3. In-container  the rendered file is owner-only (uid 999, 0400); no
#                    AppRole files, token files or SVID keys in the service
#   4. Sidecars      Vault Agent keeps its token in memory (no token on disk);
#                    SVIDs live on memory-only volumes
#   5. Host          no AppRole secret-id files, no recovery share, no
#                    database credential in the application's .env
# The documented exceptions -- what bootstrapping needs by design -- are
# listed as INFO, not hidden: unseal shares 1 and 2 (they unseal Vault on
# every start), vault-bootstrap's secret-id (rotated on every start), and
# instraclustr/.env (the values Vault is seeded from).
#
# Runs from the host with the Docker CLI against the live stack. Prints
# PASS/FAIL/INFO per check; exits 1 if any check fails. Never prints a value.
set -u

REPO=$(cd "$(dirname "$0")/../.." && pwd)
PROJECT="${PROBE_APP_PROJECT:-nas-feedops-sentinel}"
SERVICES="${PROBE_SERVICES:-migrate api dashboard sentinel temporal-worker}"
export MSYS_NO_PATHCONV=1

FAILED=0
pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1"; FAILED=1; }
info() { echo "  INFO  $1"; }
exists() { docker inspect "$1" >/dev/null 2>&1; }
running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = true ]; }

FORBIDDEN_ENV='^(POSTGRES_PASSWORD|APP_DB_PASSWORD|LITELLM_API_KEY|LITELLM_MASTER_KEY|VAULT_TOKEN|VAULT_ADDR|VAULT_ROLE_ID|VAULT_SECRET_ID|VAULT_APPROLE_DIR|DATABASE_URL)='
# DATABASE_URL is allowed only empty (compose sets it "" on purpose).
SECRET_SHAPED='=(sk-|hvs\.|hvb\.)'
FORBIDDEN_MOUNT='vault-init-data|vault-recovery|approle|unseal|instraclustr/certs/(spire|vault|web)|root-ca\.key'

echo "Zero static credentials probe: project=$PROJECT"

for svc in $SERVICES; do
    c="nas-$svc"
    if ! exists "$c"; then fail "$c: no such container (is the stack up?)"; continue; fi
    echo "$c"

    # 1. Environment (the container's configured environment: what it was handed)
    env=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$c")
    named=$(printf '%s\n' "$env" | grep -E "$FORBIDDEN_ENV" | grep -v '^DATABASE_URL=$' | cut -d= -f1 | tr '\n' ' ')
    if [ -z "$named" ]; then pass "no credential or Vault variable in its environment"
    else fail "credential variables in its environment: $named"; fi
    shaped=$(printf '%s\n' "$env" | grep -E "$SECRET_SHAPED" | cut -d= -f1 | tr '\n' ' ')
    if [ -z "$shaped" ]; then pass "no secret-shaped value (sk-, hvs.) in its environment"
    else fail "secret-shaped values in: $shaped"; fi

    # 2. Mounts
    mounts=$(docker inspect -f '{{range .Mounts}}{{.Type}}|{{.Name}}|{{.Source}}|{{.Destination}}|{{.RW}}{{println}}{{end}}' "$c")
    bad=$(printf '%s\n' "$mounts" | grep -E "$FORBIDDEN_MOUNT" | cut -d'|' -f3,4 | tr '\n' ' ')
    if [ -z "$bad" ]; then pass "no host path with Vault, PKI or AppRole material mounted"
    else fail "mounts Vault/PKI/AppRole material: $bad"; fi
    # Type, volume name, destination and read-only: the source is Docker's
    # own path for the volume and says nothing here.
    secret_mounts=$(printf '%s\n' "$mounts" | grep -c '|/vault/secrets|')
    expected="volume|${PROJECT}_${svc}-secrets|/vault/secrets|false"
    actual=$(printf '%s\n' "$mounts" | grep '|/vault/secrets|' | cut -d'|' -f1,2,4,5)
    if [ "$secret_mounts" = 1 ] && [ "$actual" = "$expected" ]; then
        pass "one secret source: its own Vault Agent file, read-only"
    else
        fail "secret mount is not exactly ${PROJECT}_${svc}-secrets at /vault/secrets, read-only"
    fi
    for kind in secrets svid; do
        vol="${PROJECT}_${svc}-${kind}"
        if [ "$(docker volume inspect -f '{{index .Options "type"}}' "$vol" 2>/dev/null)" = tmpfs ]; then
            pass "$vol is memory-only (tmpfs)"
        else
            fail "$vol is not a memory-only volume"
        fi
    done

    # 3. Inside the running service (a finished one-shot job has nothing to inspect)
    if running "$c"; then
        mode=$(docker exec "$c" stat -c '%u %a' /vault/secrets/app-config.env 2>/dev/null)
        if [ "$mode" = "999 400" ]; then pass "rendered file is owner-only (uid 999, 0400)"
        else fail "rendered file is '$mode', not '999 400'"; fi
        found=$(docker exec "$c" sh -c 'find / -xdev \( -name role_id -o -name secret_id -o -name .vault-token -o -name svid_key.pem -o -name "*.key.pending" \) 2>/dev/null' | tr '\n' ' ')
        if [ -z "$found" ]; then pass "no AppRole file, token file or SVID key in the container"
        else fail "credential files in the container: $found"; fi
    else
        info "$c is not running (one-shot job finished): configuration checked above"
    fi

    # 4. Its Vault Agent keeps the token in memory
    a="nas-$svc-vault-agent"
    if running "$a"; then
        tokens=$(docker exec "$a" sh -c 'grep -rlE "hv[sb]\.[A-Za-z0-9_-]{20,}" /vault /run /tmp /home 2>/dev/null' | tr '\n' ' ')
        if [ -z "$tokens" ]; then pass "$a: no Vault token on disk (memory only, no sink)"
        else fail "$a: Vault token found on disk: $tokens"; fi
    elif exists "$a"; then
        info "$a is stopped (its job is done: the launcher stops migrate's sidecars)"
    else
        fail "$a: no such container"
    fi
done

# 5. The host
echo "host"
if [ -e "$REPO/instraclustr/vault-init-data/approle" ]; then fail "retired AppRole secret-id files still on disk (instraclustr/vault-init-data/approle)"
else pass "no application AppRole secret-id files on disk"; fi
if [ -e "$REPO/instraclustr/vault-recovery/share-3" ]; then fail "unseal share 3 is still on this machine (move it offline, then delete it)"
else pass "unseal share 3 is not on this machine"; fi
if [ -f "$REPO/nas-feedops-sentinel/.env" ]; then
    creds=$(grep -oE '^(POSTGRES_PASSWORD|APP_DB_PASSWORD|VAULT_TOKEN|LITELLM_API_KEY)=' "$REPO/nas-feedops-sentinel/.env" | cut -d= -f1 | tr '\n' ' ')
    if [ -z "$creds" ]; then pass "the application's .env holds no database credential or Vault token"
    else fail "the application's .env still holds: $creds"; fi
fi
if [ -e "$REPO/instraclustr/certs/trust/root-ca.key" ] || find "$REPO/instraclustr/certs" -name 'root-ca.key' 2>/dev/null | grep -q .; then
    fail "a root CA key is on this machine (it belongs on the root USB only)"
else
    pass "no root CA key on this machine"
fi
info "by design: unseal shares 1 (instraclustr/vault-init-data/unseal) and 2 (a Docker volume) unseal Vault on every start"
info "by design: vault-bootstrap's secret-id (instraclustr/vault-init-data/bootstrap) is rotated on every start"
info "by design: instraclustr/.env holds the values Vault is seeded from"

echo
if [ "$FAILED" -eq 0 ]; then echo "RESULT: no static application credential found"; else echo "RESULT: FAILED -- see above"; fi
exit "$FAILED"
