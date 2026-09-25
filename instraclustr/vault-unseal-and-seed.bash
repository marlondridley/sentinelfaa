#!/bin/sh
# Vault bootstrap: runs on every infrastructure start. Idempotent.
#
# Unseal: Shamir 3 shares, threshold 2. No single file unseals Vault.
#   share 1  /vault-init-data/unseal/share-1   (bind mount: instraclustr/vault-init-data)
#   share 2  /vault-share-2/share-2            (named volume vault-unseal-share, mounted
#                                               only into this container)
#   share 3  /vault-recovery/share-3           (bind mount: instraclustr/vault-recovery --
#                                               MOVE IT OFF THIS MACHINE, then delete it)
# Automatic unseal uses shares 1 and 2; share 3 is the recovery copy.
#
# No root token on disk. The root token exists only in memory, at first
# initialization (or at the one-time migration from the old layout), long
# enough to create the `vault-bootstrap` AppRole -- then it is revoked. Every
# later run authenticates as vault-bootstrap, whose policy can seed this
# project's secrets and manage its sentinel-* AppRoles, and nothing else (it
# cannot unseal, seal, mount engines or mint root). For break-glass access,
# regenerate a root token from two shares: `vault operator generate-root`.
#
# States handled:
#   uninitialized          -> init 3/2, distribute shares, set up, revoke root
#   old layout (init.json) -> unseal with the old single key, rekey to 3/2,
#                             set up with the old root token, revoke it, and
#                             delete init.json
#   normal                 -> unseal with shares 1+2 if sealed, log in as
#                             vault-bootstrap, seed and reconcile
set -eu

: "${VAULT_ADDR:?Missing VAULT_ADDR}"
: "${POSTGRES_USER:?Missing POSTGRES_USER}"
: "${POSTGRES_PASSWORD:?Missing POSTGRES_PASSWORD}"
: "${POSTGRES_DB:?Missing POSTGRES_DB}"
: "${LITELLM_MASTER_KEY:?Missing LITELLM_MASTER_KEY}"
: "${APP_DB_PASSWORD:?Missing APP_DB_PASSWORD}"
APP_DB_USER="${APP_DB_USER:-sentinel_app}"

INIT_DIR="${INIT_DIR:-/vault-init-data}"
SHARE2_DIR="${SHARE2_DIR:-/vault-share-2}"
RECOVERY_DIR="${RECOVERY_DIR:-/vault-recovery}"
LEGACY_INIT="$INIT_DIR/init.json"
SHARE1="$INIT_DIR/unseal/share-1"
SHARE2="$SHARE2_DIR/share-2"
SHARE3="$RECOVERY_DIR/share-3"
BOOT_DIR="$INIT_DIR/bootstrap"

flat() { tr -d '\n\t ' < "$1"; }
json_str() { flat "$1" | grep -o "\"$2\":\"[^\"]*\"" | head -1 | sed -E 's/.*"([^"]+)"$/\1/'; }
json_arr() { flat "$1" | grep -o "\"$2\":\[[^]]*\]" | sed -E 's/^[^[]*\[//; s/\]$//' | tr ',' '\n' | tr -d '"'; }
status_field() { vault status -format=json 2>/dev/null | tr -d '\n\t ' | grep -o "\"$1\":[a-z]*" | sed 's/.*://'; }

write_secret_file() {  # write_secret_file <path> <value>  (owner-only; umask scoped to a subshell)
    mkdir -p "$(dirname "$1")"
    ( umask 077; printf '%s\n' "$2" > "$1" )
}

store_shares() {  # store_shares <file listing base64 shares, one per line>
    s1=$(sed -n 1p "$1"); s2=$(sed -n 2p "$1"); s3=$(sed -n 3p "$1")
    [ -n "$s1" ] && [ -n "$s2" ] && [ -n "$s3" ] || { echo "ERROR: expected 3 key shares" >&2; exit 1; }
    write_secret_file "$SHARE1" "$s1"
    write_secret_file "$SHARE2" "$s2"
    write_secret_file "$SHARE3" "$s3"
    write_secret_file "$RECOVERY_DIR/README.txt" "Vault recovery key share 3 of 3 (any 2 of 3 unseal Vault or regenerate a root token).
Move share-3 to storage OFF this machine (password manager, offline media), then delete it here.
Shares 1 and 2 are used for automatic unseal; share 3 is how you recover if either is lost."
    echo "key shares stored: 1 (vault-init-data), 2 (vault-unseal-share volume), 3 (vault-recovery: move it off this machine)"
}

wait_for_vault() {
    i=0
    until vault status -format=json 2>/dev/null | grep -q '"initialized"'; do
        i=$((i + 1)); [ "$i" -ge 30 ] && { echo "ERROR: Vault did not respond in 60 s" >&2; exit 1; }
        sleep 2
    done
}

unseal_with() {  # unseal_with <key>...
    for key in "$@"; do
        [ "$(status_field sealed)" = "true" ] || return 0
        vault operator unseal "$key" >/dev/null
    done
    [ "$(status_field sealed)" = "false" ] || { echo "ERROR: Vault is still sealed" >&2; exit 1; }
}

# ── Setup that needs root (first init, or migration). Root is in VAULT_TOKEN. ─
root_setup() {
    vault secrets list -format=json | grep -q '"secret/":' || vault secrets enable -path=secret kv-v2
    vault auth list -format=json | grep -q '"approle/":' || vault auth enable approle

    vault policy write vault-bootstrap - <<'POLICY'
# The bootstrap jobs (this script, litellm-bootstrap.sh). Seed this project's
# secrets and manage its sentinel-* AppRoles; nothing else. No sys/seal,
# sys/mounts writes, sys/generate-root, or other auth methods.
path "secret/data/*"                       { capabilities = ["create", "update", "read"] }
path "secret/metadata/*"                   { capabilities = ["read", "list"] }
path "sys/policies/acl/sentinel-*"         { capabilities = ["create", "update", "read"] }
path "auth/approle/role/sentinel-*"        { capabilities = ["create", "update", "read", "list"] }
POLICY
    vault write auth/approle/role/vault-bootstrap token_policies=vault-bootstrap \
        token_ttl=15m token_max_ttl=30m secret_id_ttl=0 secret_id_num_uses=0 >/dev/null
    write_secret_file "$BOOT_DIR/role_id" "$(vault read -field=role_id auth/approle/role/vault-bootstrap/role-id)"
    write_secret_file "$BOOT_DIR/secret_id" "$(vault write -f -field=secret_id auth/approle/role/vault-bootstrap/secret-id)"
    echo "vault-bootstrap AppRole ready"
}

revoke_root() {
    vault token revoke -self >/dev/null
    unset VAULT_TOKEN
    echo "root token revoked; none is stored (regenerate from 2 shares if ever needed)"
}

# ── Main ──────────────────────────────────────────────────────────────────────
wait_for_vault

if [ "$(status_field initialized)" != "true" ]; then
    echo "=== Uninitialized: initializing with 3 key shares, threshold 2 ==="
    vault operator init -key-shares=3 -key-threshold=2 -format=json > /tmp/init.json
    json_arr /tmp/init.json unseal_keys_b64 > /tmp/shares
    store_shares /tmp/shares
    VAULT_TOKEN=$(json_str /tmp/init.json root_token); export VAULT_TOKEN
    rm -f /tmp/init.json /tmp/shares
    unseal_with "$(cat "$SHARE1")" "$(cat "$SHARE2")"
    root_setup
    revoke_root

elif [ -f "$LEGACY_INIT" ]; then
    echo "=== Old layout (init.json with one key and a root token): migrating ==="
    LEGACY_KEY=$(json_arr "$LEGACY_INIT" unseal_keys_b64 | head -1)
    VAULT_TOKEN=$(json_str "$LEGACY_INIT" root_token); export VAULT_TOKEN
    [ -n "$LEGACY_KEY" ] && [ -n "$VAULT_TOKEN" ] || { echo "ERROR: cannot read $LEGACY_INIT" >&2; exit 1; }
    unseal_with "$LEGACY_KEY"
    if [ ! -f "$SHARE1" ]; then
        vault operator rekey -cancel >/dev/null 2>&1 || true
        vault operator rekey -init -key-shares=3 -key-threshold=2 -format=json > /tmp/rekey-init.json
        NONCE=$(json_str /tmp/rekey-init.json nonce)
        # Straight to the bind mount: if anything fails after this point the
        # new shares are still recoverable from this file.
        vault operator rekey -nonce="$NONCE" -format=json "$LEGACY_KEY" > "$INIT_DIR/rekey-output.json"
        json_arr "$INIT_DIR/rekey-output.json" keys_base64 > /tmp/shares
        store_shares /tmp/shares
        rm -f /tmp/shares /tmp/rekey-init.json "$INIT_DIR/rekey-output.json"
        echo "rekeyed: 1 share -> 3 shares, threshold 2"
    fi
    root_setup
    revoke_root
    rm -f "$LEGACY_INIT"
    echo "removed $LEGACY_INIT (old unseal key is invalid after the rekey)"

else
    [ -f "$SHARE1" ] && [ -f "$SHARE2" ] || { echo "ERROR: unseal shares 1/2 missing; unseal by hand with 2 shares" >&2; exit 1; }
    unseal_with "$(cat "$SHARE1")" "$(cat "$SHARE2")"
fi

# ── Every run: authenticate as vault-bootstrap ───────────────────────────────
VAULT_TOKEN=$(vault write -field=token auth/approle/login \
    role_id="$(cat "$BOOT_DIR/role_id")" secret_id="$(cat "$BOOT_DIR/secret_id")")
export VAULT_TOKEN
echo "=== Authenticated as vault-bootstrap ==="

# ── Seed secrets (idempotent) ─────────────────────────────────────────────────
vault kv put secret/postgres/credentials \
    username="${POSTGRES_USER}" password="${POSTGRES_PASSWORD}" database="${POSTGRES_DB}" >/dev/null
vault kv put secret/postgres/app \
    username="${APP_DB_USER}" password="${APP_DB_PASSWORD}" database="${POSTGRES_DB}" >/dev/null
vault kv put secret/litellm \
    DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-}" GEMINI_API_KEY="${GEMINI_API_KEY:-}" \
    LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY}" >/dev/null
# secret/sentinel/config (LITELLM_API_KEY) is written by litellm-bootstrap.sh:
# a scoped virtual key, not the master key.
echo "secrets seeded"

# ── Service AppRoles: least privilege, secret IDs rotated every start ────────
vault policy write sentinel-app - <<'POLICY'
# Application services: their database identity and LiteLLM key, nothing else.
path "secret/data/postgres/app"    { capabilities = ["read"] }
path "secret/data/sentinel/config" { capabilities = ["read"] }
POLICY

vault policy write sentinel-migrate - <<'POLICY'
# Schema job: the owner identity plus what it grants to the application role.
path "secret/data/postgres/credentials" { capabilities = ["read"] }
path "secret/data/postgres/app"         { capabilities = ["read"] }
path "secret/data/sentinel/config"      { capabilities = ["read"] }
POLICY

for role in sentinel-app sentinel-migrate; do
    vault write "auth/approle/role/$role" token_policies="$role" \
        token_ttl=1h token_max_ttl=4h secret_id_ttl=0 secret_id_num_uses=0 >/dev/null
    dir="$INIT_DIR/approle/$role"
    mkdir -p "$dir"
    vault read -field=role_id "auth/approle/role/$role/role-id" > "$dir/role_id"
    # Accessors are UUIDs; with none issued yet `vault list` prints "{}".
    for accessor in $(vault list -format=json "auth/approle/role/$role/secret-id" 2>/dev/null \
                      | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'); do
        vault write -f "auth/approle/role/$role/secret-id-accessor/destroy" secret_id_accessor="$accessor" >/dev/null
    done
    vault write -f -field=secret_id "auth/approle/role/$role/secret-id" > "$dir/secret_id"
    echo "approle $role: policy written, secret_id rotated"
done

echo "=== Vault bootstrap complete ==="
