#!/bin/sh
# One-shot, every infrastructure start, after LiteLLM is healthy. Idempotent.
#
# Issues the application's own LiteLLM key and stores it in Vault at
# secret/sentinel/config (LITELLM_API_KEY), replacing the proxy master key the
# application used to hold. The key is a LiteLLM virtual key:
#   * models:     only the aliases the application uses (primary-model and its
#                 gemini-backup fallback) -- a request for any other alias is refused
#   * max_budget: $LITELLM_APP_BUDGET_USD per 30 days
#   * rpm_limit:  $LITELLM_APP_RPM requests per minute
# The master key never leaves this container and the proxy.
#
# Reuses the key already in Vault when LiteLLM still knows it; otherwise
# (fresh Vault or fresh LiteLLM database, e.g. after `down -v`) it replaces
# any stale key under the same alias and issues a new one.
set -eu
: "${VAULT_ADDR:?}" "${LITELLM_MASTER_KEY:?}"
PROXY="${LITELLM_URL:-http://nas-litellm:4000}"
ALIAS=sentinel-app
BUDGET="${LITELLM_APP_BUDGET_USD:-10}"
RPM="${LITELLM_APP_RPM:-30}"

FLAT=$(tr -d '\n\t ' < /vault-init-data/init.json)
VAULT_TOKEN=$(echo "$FLAT" | grep -o '"root_token":"[^"]*"' | sed -E 's/.*"([^"]+)"$/\1/')
export VAULT_TOKEN

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
proxy /key/generate "{\"key\":\"$KEY\",\"key_alias\":\"$ALIAS\",\"models\":[\"primary-model\",\"gemini-backup\"],\"max_budget\":$BUDGET,\"budget_duration\":\"30d\",\"rpm_limit\":$RPM,\"metadata\":{\"owner\":\"nas-feedops-sentinel\",\"issued_by\":\"litellm-bootstrap\"}}" >/dev/null
vault kv put secret/sentinel/config LITELLM_API_KEY="$KEY" >/dev/null
echo "virtual key '$ALIAS': issued (models primary-model, gemini-backup; \$$BUDGET/30d; ${RPM} rpm) and stored in Vault"
