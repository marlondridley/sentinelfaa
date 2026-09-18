#!/bin/sh
set -eu

echo "=== Starting Vault Auto-Seeding Script ==="

# 1. Strictly enforce required environment variables
: "${VAULT_ADDR:?Missing VAULT_ADDR in environment}"
: "${VAULT_TOKEN:?Missing VAULT_TOKEN in environment}"
: "${POSTGRES_USER:?Missing POSTGRES_USER in environment}"
: "${POSTGRES_PASSWORD:?Missing POSTGRES_PASSWORD in environment}"
: "${POSTGRES_DB:?Missing POSTGRES_DB in environment}"
: "${DEEPSEEK_API_KEY:?Missing DEEPSEEK_API_KEY in environment}"
: "${GEMINI_API_KEY:?Missing GEMINI_API_KEY in environment}"
: "${LITELLM_MASTER_KEY:?Missing LITELLM_MASTER_KEY in environment}"
: "${LLM_API_KEY:?Missing LLM_API_KEY in environment}"

# 2. Ensure Vault is reachable and usable
echo "Checking Vault server status..."

if ! vault status >/dev/null 2>&1; then
    echo "ERROR: Vault is not ready or is unreachable at ${VAULT_ADDR}" >&2
    exit 1
fi

# 3. Idempotently check and enable the KV v2 secrets engine
echo "Ensuring KV v2 secrets engine is mounted at 'secret/'..."

if vault secrets list -format=json | grep -q '"secret/":'; then
    echo "KV engine 'secret/' already enabled."
else
    echo "Mounting KV v2 engine at 'secret/'..."
    vault secrets enable -path=secret kv-v2
    echo "KV v2 engine mounted successfully."
fi

# 4. Seed PostgreSQL credentials
echo "Seeding PostgreSQL credentials..."

vault kv put secret/postgres/credentials \
    username="${POSTGRES_USER}" \
    password="${POSTGRES_PASSWORD}" \
    database="${POSTGRES_DB}"

# 5. Seed LiteLLM keys
echo "Seeding LLM API keys for LiteLLM..."

vault kv put secret/litellm \
    DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY}" \
    GEMINI_API_KEY="${GEMINI_API_KEY}" \
    LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY}"

vault kv put secret/litellm/deepseek \
    api_key="${DEEPSEEK_API_KEY}"

vault kv put secret/litellm/gemini \
    api_key="${GEMINI_API_KEY}"

# 6. Seed Sentinel application proxy key
# Consumed as LITELLM_API_KEY by vault_config.py
echo "Seeding application proxy keys..."

vault kv put secret/sentinel/config \
    LITELLM_API_KEY="${LLM_API_KEY}"

echo "=== Vault Seeding Complete ==="
