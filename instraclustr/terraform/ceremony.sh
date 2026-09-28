#!/bin/sh
# The root ceremony: the only way anything in instraclustr/terraform/ reaches
# the real Vault.
#
#   docker compose -f instraclustr/docker-compose.yml --env-file instraclustr/.env \
#       --profile ceremony run --rm vault-ceremony plan     # read-only: what would change
#   ... run --rm vault-ceremony apply                      # asks "yes" before changing
#
# 1. Generates a root token from unseal shares 1 and 2 (the same two
#    vault-unseal uses; share 3 stays off this machine). No root token is
#    stored anywhere, before or after -- see vault-unseal-and-seed.bash.
# 2. Runs exactly one terraform plan or apply with it.
# 3. Revokes it on the way out, whatever happened in between.
#
# plan exits like `terraform plan -detailed-exitcode`: 0 no changes (no
# drift), 2 changes pending, 1 error.
#
# Anyone who can read shares 1 and 2 can do this -- as they can unseal Vault.
# That is the existing trust boundary (the Docker host), not a new one; the
# ceremony makes the root token's lifetime one command long.
set -eu

MODE="${1:-}"
case "$MODE" in
    plan|apply) ;;
    *) echo "usage: ceremony.sh plan|apply" >&2; exit 2 ;;
esac
: "${VAULT_ADDR:?Missing VAULT_ADDR}"
: "${VAULT_CACERT:?Missing VAULT_CACERT -- Vault is TLS only}"
SHARE1="${SHARE1:-/vault-init-data/unseal/share-1}"
SHARE2="${SHARE2:-/vault-share-2/share-2}"
[ -r "$SHARE1" ] && [ -r "$SHARE2" ] || { echo "ERROR: unseal shares 1 and 2 are not readable" >&2; exit 1; }

json_str() { tr -d '\n\t ' | grep -o "\"$1\":\"[^\"]*\"" | head -1 | sed -E 's/.*"([^"]+)"$/\1/'; }

if [ "$(vault status -format=json 2>/dev/null | tr -d '\n\t ' | grep -o '"sealed":[a-z]*' | cut -d: -f2)" != "false" ]; then
    echo "ERROR: Vault is not reachable and unsealed at $VAULT_ADDR" >&2
    exit 1
fi

echo "=== Generating a root token from unseal shares 1 and 2 ==="
vault operator generate-root -cancel >/dev/null 2>&1 || true
INIT=$(vault operator generate-root -init -format=json)
NONCE=$(echo "$INIT" | json_str nonce)
OTP=$(echo "$INIT" | json_str otp)
[ -n "$NONCE" ] && [ -n "$OTP" ] || { echo "ERROR: generate-root did not start" >&2; exit 1; }
# Shares on stdin ("-"), not argv: argv is visible in /proc to anything in
# this container's PID namespace.
vault operator generate-root -nonce="$NONCE" -format=json - < "$SHARE1" >/dev/null
ENCODED=$(vault operator generate-root -nonce="$NONCE" -format=json - < "$SHARE2" | json_str encoded_token)
[ -n "$ENCODED" ] || { vault operator generate-root -cancel >/dev/null 2>&1 || true; echo "ERROR: shares 1 and 2 did not complete generate-root" >&2; exit 1; }
VAULT_TOKEN=$(vault operator generate-root -decode="$ENCODED" -otp="$OTP")
export VAULT_TOKEN
unset INIT NONCE OTP ENCODED

revoke() {
    if vault token revoke -self >/dev/null 2>&1; then
        echo "=== Root token revoked ==="
    else
        echo "WARNING: could not revoke the root token; revoke it now (vault token revoke -self)" >&2
    fi
}
trap revoke EXIT
trap 'exit 130' INT TERM

cd /terraform
terraform init -input=false -no-color >/dev/null
case "$MODE" in
    plan)
        status=0
        terraform plan -input=false -detailed-exitcode || status=$?
        exit "$status" ;;
    apply)
        # Interactive by default: the operator reads the plan and types yes.
        # CEREMONY_AUTO_APPROVE=1 is for the throwaway smoke harness only.
        if [ "${CEREMONY_AUTO_APPROVE:-0}" = "1" ]; then
            terraform apply -input=false -auto-approve
        else
            terraform apply
        fi ;;
esac
