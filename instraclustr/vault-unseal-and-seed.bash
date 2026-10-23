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
# enough to enable the KV engine and AppRole, create the vault-bootstrap
# identity, and revoke itself -- see root_setup(). Every later run
# authenticates as vault-bootstrap, which can do exactly two things: write
# the KV secrets this project seeds, and rotate its own secret-id. It cannot
# write a policy, configure an auth method or role, unseal, seal, mount
# engines or generate a root token, so it is not root-equivalent.
#
# The application's identities are not here at all. Each application service
# logs in with its own SPIFFE X.509-SVID over mutual TLS (instraclustr/
# README.md, "Workload identity"); what each may read, and the cert auth
# method itself, are Terraform's (instraclustr/terraform/), applied only in
# the root ceremony -- a root token from shares 1 and 2, one terraform run,
# revoked. So is any policy change, vault-bootstrap's own included: this
# script writes that policy once, at first init, from the same file.
#
# vault-bootstrap's secret_id_ttl / secret_id_num_uses, set in root_setup():
#   24h, num_uses=0 (unlimited uses within the ttl). Not num_uses=1: this
#   identity has TWO one-shot consumers sharing the SAME on-disk secret-id
#   every boot cycle -- this script, and litellm-bootstrap.sh, which
#   docker-compose.yml orders strictly after this one finishes and which
#   reads the very file this script just rotated. num_uses=1 would let this
#   script's own login succeed, hand litellm-bootstrap.sh a working secret-id
#   for THIS cycle, and leave nothing for THIS SCRIPT's own next invocation --
#   breaking the bootstrap chain after exactly one restart, silently. Every
#   run still rotates it (rotate_secret_id, below) right after using it,
#   which is what actually bounds how long any one secret-id value stays
#   valid (about one restart cycle) -- the ttl is slack for how long the
#   stack can go without a restart at all, not a replay window. A stack
#   down for longer than the ttl finds the secret-id expired; this script
#   then re-issues it itself (reissue_bootstrap_secret_id: a root token from
#   the shares it already holds, used for that alone, revoked at once) and
#   says so, instead of failing every start until someone intervenes. A
#   corrupted role_id is answered by Vault the same way and repaired the
#   same way; any other login failure still stops the run with Vault's error.
#
# States handled:
#   uninitialized          -> init 3/2, distribute shares, set up, revoke root
#   old layout (init.json) -> unseal with the old single key, rekey to 3/2,
#                             set up with the old root token, revoke it, and
#                             delete init.json
#   normal                 -> unseal with shares 1+2 if sealed, log in as
#                             vault-bootstrap, seed, rotate its secret-id
set -eu

: "${VAULT_ADDR:?Missing VAULT_ADDR}"
APP_DB_USER="${APP_DB_USER:-sentinel_app}"

INIT_DIR="${INIT_DIR:-/vault-init-data}"
SHARE2_DIR="${SHARE2_DIR:-/vault-share-2}"
RECOVERY_DIR="${RECOVERY_DIR:-/vault-recovery}"
LEGACY_INIT="$INIT_DIR/init.json"
SHARE1="$INIT_DIR/unseal/share-1"
SHARE2="$SHARE2_DIR/share-2"
SHARE3="$RECOVERY_DIR/share-3"
BOOT_DIR="$INIT_DIR/bootstrap"
POLICY_DIR="${POLICY_DIR:-/vault-policies}"

flat() { tr -d '\n\t ' < "$1"; }
json_str() { flat "$1" | grep -o "\"$2\":\"[^\"]*\"" | head -1 | sed -E 's/.*"([^"]+)"$/\1/'; }
json_arr() { flat "$1" | grep -o "\"$2\":\[[^]]*\]" | sed -E 's/^[^[]*\[//; s/\]$//' | tr ',' '\n' | tr -d '"'; }
status_field() { vault status -format=json 2>/dev/null | tr -d '\n\t ' | grep -o "\"$1\":[a-z]*" | sed 's/.*://'; }

write_secret_file() {  # write_secret_file <path> <value> [mode]  (default 600, owner-only)
    # A shell redirect's O_CREAT only applies permission bits when the file does
    # not already exist -- overwriting one with the wrong mode (a file this same
    # helper wrote before this fix existed, or one from a compromised umask)
    # would silently keep that mode forever. rm -f first so the redirect always
    # creates the file fresh (under a restrictive umask, then chmod to the
    # requested mode explicitly -- simpler and more certain than computing a
    # umask for an arbitrary target mode).
    mode="${3:-600}"
    mkdir -p "$(dirname "$1")"
    rm -f "$1"
    ( umask 077; printf '%s\n' "$2" > "$1" )
    chmod "$mode" "$1"
}

# rotate_secret_id <role> <dir>: mint a fresh secret-id for <role> into
# <dir>/secret_id (owner-only), destroying every existing accessor first.
# vault-bootstrap rotates its own right after logging in with the one it just
# used, so the file on disk is always a not-yet-used secret-id, and the one
# that was just read is never valid again regardless of its num_uses/ttl.
# Its readers -- this script and litellm-bootstrap.sh -- both run as root in
# the hashicorp/vault image, so 600 is right.
rotate_secret_id() {
    # set -e does not fire on a command that fails inside a $(...) used as
    # someone else's argument, so a denied/failed `vault` call here would
    # otherwise write an EMPTY secret file and let the script report success.
    # Capture the value and check the exit status explicitly instead.
    role="$1"; dir="$2"
    role_id=$(vault read -field=role_id "auth/approle/role/$role/role-id") \
        || { echo "ERROR: could not read role-id for $role" >&2; exit 1; }
    write_secret_file "$dir/role_id" "$role_id"
    # Accessors are UUIDs; with none issued yet `vault list` prints "{}".
    for accessor in $(vault list -format=json "auth/approle/role/$role/secret-id" 2>/dev/null \
                      | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'); do
        vault write -f "auth/approle/role/$role/secret-id-accessor/destroy" secret_id_accessor="$accessor" >/dev/null \
            || { echo "ERROR: could not destroy a stale secret-id accessor for $role" >&2; exit 1; }
    done
    secret_id=$(vault write -f -field=secret_id "auth/approle/role/$role/secret-id") \
        || { echo "ERROR: could not mint a secret-id for $role" >&2; exit 1; }
    write_secret_file "$dir/secret_id" "$secret_id"
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
# The bootstrap identity only: everything that WRITES A POLICY or configures a
# role lives here, under root, never in the every-run section below, which runs
# as vault-bootstrap. The application's identities, policies and auth method
# are Terraform's (instraclustr/terraform/), applied in the root ceremony.
root_setup() {
    vault secrets list -format=json | grep -q '"secret/":' || vault secrets enable -path=secret kv-v2
    vault auth list -format=json | grep -q '"approle/":' || vault auth enable approle

    # Policy text lives in instraclustr/vault-policies/, the one copy both this
    # first-init path and Terraform (instraclustr/terraform/, which owns it
    # from the first ceremony on and catches drift) write it from.
    vault policy write vault-bootstrap "$POLICY_DIR/vault-bootstrap.hcl" >/dev/null
    vault write auth/approle/role/vault-bootstrap token_policies=vault-bootstrap \
        token_ttl=15m token_max_ttl=30m secret_id_ttl=24h secret_id_num_uses=0 >/dev/null

    # Its very first secret-id: the every-run section below logs in with it,
    # and only ever rotates one that already exists.
    rotate_secret_id vault-bootstrap "$BOOT_DIR"
    echo "vault-bootstrap AppRole ready"
}

revoke_root() {
    vault token revoke -self >/dev/null
    unset VAULT_TOKEN
    echo "root token revoked; none is stored (regenerate from 2 shares if ever needed)"
}

# json_field <key>: a string field from JSON on stdin (no temp file).
json_field() { tr -d '\n\t ' | grep -o "\"$1\":\"[^\"]*\"" | head -1 | sed -E 's/.*"([^"]+)"$/\1/'; }

# reissue_bootstrap_secret_id: vault-bootstrap's secret-id lives 24h and is
# re-issued only by a successful run of this script, so a stack that has not
# started for a day cannot log in as vault-bootstrap at all ("invalid role or
# secret ID") -- which is what the first start after the SPIFFE cutover hit.
# This container already holds unseal shares 1 and 2, i.e. it can already
# generate a root token; recovery uses exactly that, for exactly one thing --
# a fresh vault-bootstrap secret-id -- and revokes it at once. No new power.
reissue_bootstrap_secret_id() {
    vault operator generate-root -cancel >/dev/null 2>&1 || true
    gr_init=$(vault operator generate-root -init -format=json) \
        || { echo "ERROR: could not start generate-root" >&2; exit 1; }
    gr_nonce=$(printf '%s' "$gr_init" | json_field nonce)
    gr_otp=$(printf '%s' "$gr_init" | json_field otp)
    # Shares on stdin ("-"), never argv.
    vault operator generate-root -nonce="$gr_nonce" -format=json - < "$SHARE1" >/dev/null
    gr_encoded=$(vault operator generate-root -nonce="$gr_nonce" -format=json - < "$SHARE2" | json_field encoded_token)
    [ -n "$gr_encoded" ] || { vault operator generate-root -cancel >/dev/null 2>&1 || true
                              echo "ERROR: shares 1 and 2 did not complete generate-root" >&2; exit 1; }
    VAULT_TOKEN=$(vault operator generate-root -decode="$gr_encoded" -otp="$gr_otp"); export VAULT_TOKEN
    unset gr_init gr_nonce gr_otp gr_encoded
    rotate_secret_id vault-bootstrap "$BOOT_DIR"
    revoke_root
}

# bootstrap_login: prints a vault-bootstrap token; Vault's error in /tmp/login.err.
bootstrap_login() {
    vault write -field=token auth/approle/login \
        role_id="$(cat "$BOOT_DIR/role_id")" secret_id="$(cat "$BOOT_DIR/secret_id")" 2>/tmp/login.err
}

# ── The gateway's secrets: Vault is their source of truth ─────────────────────
# secret/litellm holds the provider key, the master key and the litellm
# database role's password; the gateway and its database jobs read them
# through litellm-vault-agent (vault-agent/litellm.hcl). Per key:
#   Vault has a value        -> kept; never overwritten from .env
#   Vault has none, env has  -> copied in: the one-time migration for a stack
#                               whose Postgres role and virtual keys already
#                               use the old .env values
#   neither                  -> generated (master key, database password), or
#                               left empty with a warning (ANTHROPIC_API_KEY,
#                               an external credential a person supplies)
# Rotating one is a person's job, with root: see instraclustr/.env.example.

# litellm_secret <key>: Vault's value for <key> at secret/litellm, empty when it
# has none. Any other failure (denied, unreachable) returns 1, so a key is
# never generated over a value that could not be read.
litellm_secret() {
    vault kv get -field="$1" secret/litellm 2>/tmp/kv.err && return 0
    grep -qE "No value found at|not present in secret" /tmp/kv.err && return 0
    cat /tmp/kv.err >&2
    return 1
}

random_hex() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# litellm_value <key> <value from the environment>: prints the value to store.
# Says what it did on stderr (stdout is the value), and never prints a value.
litellm_value() {
    current=$(litellm_secret "$1") || { echo "ERROR: could not read $1 at secret/litellm" >&2; return 1; }
    if [ -n "$current" ]; then
        echo "secret/litellm $1: in Vault, kept" >&2
        printf '%s' "$current"
    elif [ -n "$2" ]; then
        echo "secret/litellm $1: copied from the environment (one-time migration; now delete it from .env)" >&2
        printf '%s' "$2"
    elif [ "$1" = ANTHROPIC_API_KEY ]; then
        echo "WARNING: secret/litellm $1: none in Vault or the environment, left empty: the gateway cannot reach the model and the Critic reports itself unavailable" >&2
    elif [ "$1" = LITELLM_MASTER_KEY ]; then
        echo "secret/litellm $1: generated" >&2
        printf 'sk-%s' "$(random_hex)"
    else
        echo "secret/litellm $1: generated" >&2
        random_hex
    fi
}

seed_litellm() {
    anthropic=$(litellm_value ANTHROPIC_API_KEY "${ANTHROPIC_API_KEY:-}") || exit 1
    master=$(litellm_value LITELLM_MASTER_KEY "${LITELLM_MASTER_KEY:-}") || exit 1
    db_password=$(litellm_value LITELLM_DB_PASSWORD "${LITELLM_DB_PASSWORD:-}") || exit 1
    # All three in one write: a kv put replaces the whole secret.
    vault kv put secret/litellm \
        ANTHROPIC_API_KEY="$anthropic" LITELLM_MASTER_KEY="$master" LITELLM_DB_PASSWORD="$db_password" >/dev/null
    unset anthropic master db_password current
    rm -f /tmp/kv.err
}

# ── Main ──────────────────────────────────────────────────────────────────────
# Guarded so a script can dot-source this file for its functions
# (write_secret_file, rotate_secret_id, root_setup, revoke_root, seed_litellm)
# without running the init/unseal/every-run state machine below.
if [ "${VAULT_BOOTSTRAP_SOURCED:-0}" != "1" ]; then

: "${POSTGRES_USER:?Missing POSTGRES_USER}"
: "${POSTGRES_PASSWORD:?Missing POSTGRES_PASSWORD}"
: "${POSTGRES_DB:?Missing POSTGRES_DB}"
: "${APP_DB_PASSWORD:?Missing APP_DB_PASSWORD}"

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
if ! VAULT_TOKEN=$(bootstrap_login); then
    if grep -q "invalid role or secret ID" /tmp/login.err; then
        echo "=== vault-bootstrap's secret-id has expired (no successful start for over 24h):"
        echo "    re-issuing it with a root token from shares 1 and 2, revoked at once ==="
        reissue_bootstrap_secret_id
        VAULT_TOKEN=$(bootstrap_login) || { cat /tmp/login.err >&2; exit 1; }
    else
        cat /tmp/login.err >&2
        exit 1
    fi
fi
rm -f /tmp/login.err
export VAULT_TOKEN
echo "=== Authenticated as vault-bootstrap ==="

# Rotate vault-bootstrap's own secret-id immediately: the one just used above is
# never valid again after this, so a stolen copy of it is worthless from here on,
# and the file on disk is always the one due for the NEXT run, not this one.
rotate_secret_id vault-bootstrap "$BOOT_DIR"

# ── Seed secrets (idempotent) ─────────────────────────────────────────────────
vault kv put secret/postgres/credentials \
    username="${POSTGRES_USER}" password="${POSTGRES_PASSWORD}" database="${POSTGRES_DB}" >/dev/null
vault kv put secret/postgres/app \
    username="${APP_DB_USER}" password="${APP_DB_PASSWORD}" database="${POSTGRES_DB}" >/dev/null
seed_litellm
# secret/sentinel/config (LITELLM_API_KEY) is written by litellm-bootstrap.sh:
# a scoped virtual key, not the master key.
echo "secrets seeded"

# ── Retired: the application's AppRole secret-id files ───────────────────────
# Services log in with their SPIFFE identities now. The sentinel-* roles are
# deleted in the root ceremony (terraform/ceremony.sh), which invalidates these
# files; they are removed here so no credential-shaped file lingers on disk.
if [ -d "$INIT_DIR/approle" ]; then
    rm -rf "$INIT_DIR/approle"
    echo "removed the retired $INIT_DIR/approle secret-id files"
fi

echo "=== Vault bootstrap complete ==="

fi  # VAULT_BOOTSTRAP_SOURCED
