#!/bin/sh
# One-shot, every infrastructure start, after LiteLLM is healthy. Idempotent.
#
# Issues the application's own LiteLLM key and stores it in Vault at
# secret/sentinel/config (LITELLM_API_KEY), replacing the proxy master key the
# application used to hold. The key is a LiteLLM virtual key:
#   * models:     only the aliases the application uses (primary-model and its
#                 only) -- a request for any other alias is refused
#   * max_budget: $LITELLM_APP_BUDGET_USD per 30 days
#   * rpm_limit:  $LITELLM_APP_RPM requests per minute
# The master key never leaves this container and the proxy.
#
# Reuses the key already in Vault when LiteLLM still knows it; otherwise
# (fresh Vault or fresh LiteLLM database, e.g. after `down -v`) it replaces
# any stale key under the same alias and issues a new one.
set -eu
: "${VAULT_ADDR:?}"
PROXY="${LITELLM_URL:-http://nas-litellm:4000}"
ALIAS=sentinel-app
BUDGET="${LITELLM_APP_BUDGET_USD:-10}"
RPM="${LITELLM_APP_RPM:-30}"

# Authenticate as vault-bootstrap (see vault-unseal-and-seed.bash); no root
# token exists on disk any more.
VAULT_TOKEN=$(vault write -field=token auth/approle/login \
    role_id="$(cat /vault-bootstrap/role_id)" secret_id="$(cat /vault-bootstrap/secret_id)")
export VAULT_TOKEN

# The master key, from Vault (secret/litellm, seeded by vault-unseal-and-seed.bash),
# never from .env. An empty or unreadable one stops the job with a plain message.
LITELLM_MASTER_KEY=$(vault kv get -field=LITELLM_MASTER_KEY secret/litellm) \
    || { echo "ERROR: could not read LITELLM_MASTER_KEY at secret/litellm" >&2; exit 1; }
[ -n "$LITELLM_MASTER_KEY" ] || { echo "ERROR: LITELLM_MASTER_KEY at secret/litellm is empty" >&2; exit 1; }

proxy() {  # proxy <path> [json-body]
    if [ $# -gt 1 ]; then
        wget -q -O - --header "Authorization: Bearer $LITELLM_MASTER_KEY" \
             --header "Content-Type: application/json" --post-data "$2" "$PROXY$1"
    else
        wget -q -O - --header "Authorization: Bearer $LITELLM_MASTER_KEY" "$PROXY$1"
    fi
}

CURRENT=$(vault kv get -field=LITELLM_API_KEY secret/sentinel/config 2>/dev/null || true)
if [ -n "$CURRENT" ] && [ "$CURRENT" != "$LITELLM_MASTER_KEY" ] \
        && proxy "/key/info?key=$CURRENT" >/dev/null 2>&1; then
    echo "virtual key '$ALIAS': present in LiteLLM and Vault, kept"
    exit 0
fi

KEY="sk-sentinel-$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
proxy /key/delete "{\"key_aliases\":[\"$ALIAS\"]}" >/dev/null 2>&1 || true
proxy /key/generate "{\"key\":\"$KEY\",\"key_alias\":\"$ALIAS\",\"models\":[\"primary-model\"],\"max_budget\":$BUDGET,\"budget_duration\":\"30d\",\"rpm_limit\":$RPM,\"metadata\":{\"owner\":\"nas-feedops-sentinel\",\"issued_by\":\"litellm-bootstrap\"}}" >/dev/null
vault kv put secret/sentinel/config LITELLM_API_KEY="$KEY" >/dev/null
echo "virtual key '$ALIAS': issued (model primary-model; \$$BUDGET/30d; ${RPM} rpm) and stored in Vault"
