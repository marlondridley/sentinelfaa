#!/bin/sh
set -e

echo "=== Starting Vault Auto-Seeding Script ==="
# Enable KV v2 secrets engine
vault secrets enable -version=2 -path=secret kv 2>/dev/null || echo "Secret engine already enabled"

# 1. Strictly enforce that all required secrets were passed in from the .env file
# If any of these are missing in instaclustr/.env, the script fails immediately and alerts you.
: "${VAULT_TOKEN:?Missing VAULT_TOKEN in environment}"
: "${POSTGRES_USER:?Missing POSTGRES_USER in environment}"
: "${POSTGRES_PASSWORD:?Missing POSTGRES_PASSWORD in environment}"
: "${POSTGRES_DB:?Missing POSTGRES_DB in environment}"
: "${ANTHROPIC_API_KEY:?Missing ANTHROPIC_API_KEY in environment}"
: "${LLM_API_KEY:?Missing LLM_API_KEY in environment}"

# 2. Enable KV Version 2 engine at path 'secret'
echo "Ensuring KV v2 secrets engine is mounted at 'secret/'..."
vault secrets enable -path=secret kv-v2 2>/dev/null || echo "KV engine 'secret/' already enabled."

# 3. Seed PostgreSQL credentials
echo "Seeding PostgreSQL credentials..."
vault kv put secret/postgres/credentials \
  username="${POSTGRES_USER}" \
  password="${POSTGRES_PASSWORD}" \
  database="${POSTGRES_DB}"

# 4. Seed LiteLLM Keys
echo "Seeding LLM API keys for LiteLLM..."
vault kv put secret/litellm \
  ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY}" \
  LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY}"

# 5. Seed Sentinel App Proxy Key
echo "Seeding application proxy keys..."
vault kv put secret/sentinel/config \
  LITELLM_API_KEY="${LLM_API_KEY}"

echo "=== Vault Seeding Complete ==="
