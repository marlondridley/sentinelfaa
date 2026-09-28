# Vault Agent sidecar for the long-running application services (api,
# dashboard, sentinel, temporal-worker): authenticates with this service's
# X.509-SVID over mutual TLS and renders the secrets its policy allows
# (vault-policies/sentinel-app.hcl) into a file on a memory-only volume the
# service mounts read-only (src/vault_config.py, VaultAgentFileSettingsSource).
# The service itself never talks to Vault, holds no Vault token, and has no
# credential on disk.

vault {
  address = "https://vault:8200"
  ca_cert = "/certs-trust/root-ca.crt"
}

auto_auth {
  method "cert" {
    config = {
      # No role name: Vault checks the SVID against every cert role and
      # accepts the one whose allowed_uri_sans is this service's exact
      # SPIFFE ID (terraform/main.tf), so one file serves every service of
      # the class, and a service can only ever get its own role.
      ca_cert     = "/certs-trust/root-ca.crt"
      client_cert = "/run/spiffe/svid.pem"
      client_key  = "/run/spiffe/svid_key.pem"
      # Re-read the SVID from disk on every authentication: spiffe-helper
      # rotates it hourly.
      reload = true
    }
  }
  # No sink: the Vault token stays in this process's memory, never on disk.
}

template_config {
  # Secrets are re-read every five minutes, so a rotated password or LiteLLM
  # key reaches the file without a restart.
  static_secret_render_interval = "5m"
}

template {
  destination          = "/vault/secrets/app-config.env"
  perms                = "0400"
  error_on_missing_key = true
  contents             = <<-EOT
    {{ with secret "secret/data/postgres/app" -}}
    POSTGRES_USER={{ .Data.data.username }}
    POSTGRES_PASSWORD={{ .Data.data.password }}
    {{ end -}}
    {{ with secret "secret/data/sentinel/config" -}}
    LITELLM_API_KEY={{ .Data.data.LITELLM_API_KEY }}
    {{ end -}}
    EOT
}
