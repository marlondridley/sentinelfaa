# Vault Agent sidecar for the model gateway (litellm-vault-agent,
# instraclustr/docker-compose.yml): as app.hcl, with the gateway's identity,
# rendering its three secrets (vault-policies/litellm.hcl) into a file on a
# memory-only volume. The gateway reads it at start (litellm-entrypoint.sh),
# and so do postgres-scram and litellm-db-init, for the database password.

vault {
  address = "https://vault:8200"
  ca_cert = "/certs-trust/root-ca.crt"
}

auto_auth {
  method "cert" {
    config = {
      ca_cert     = "/certs-trust/root-ca.crt"
      client_cert = "/run/spiffe/svid.pem"
      client_key  = "/run/spiffe/svid_key.pem"
      reload      = true
    }
  }
  # No sink: the Vault token stays in this process's memory, never on disk.
}

template {
  destination          = "/vault/secrets/litellm.env"
  perms                = "0400"
  error_on_missing_key = true
  contents             = <<-EOT
    {{ with secret "secret/data/litellm" -}}
    ANTHROPIC_API_KEY={{ .Data.data.ANTHROPIC_API_KEY }}
    LITELLM_MASTER_KEY={{ .Data.data.LITELLM_MASTER_KEY }}
    LITELLM_DB_PASSWORD={{ .Data.data.LITELLM_DB_PASSWORD }}
    {{ end -}}
    EOT
}
