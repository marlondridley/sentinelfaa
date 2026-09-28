# Vault Agent sidecar for the schema jobs (migrate, proof): as app.hcl, with
# the schema owner's identity and the application role's password, which the
# job sets (vault-policies/sentinel-migrate.hcl, src/storage/migrate.py).

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
}

template {
  destination          = "/vault/secrets/app-config.env"
  perms                = "0400"
  error_on_missing_key = true
  contents             = <<-EOT
    {{ with secret "secret/data/postgres/credentials" -}}
    POSTGRES_USER={{ .Data.data.username }}
    POSTGRES_PASSWORD={{ .Data.data.password }}
    {{ end -}}
    {{ with secret "secret/data/postgres/app" -}}
    APP_DB_USER={{ .Data.data.username }}
    APP_DB_PASSWORD={{ .Data.data.password }}
    {{ end -}}
    {{ with secret "secret/data/sentinel/config" -}}
    LITELLM_API_KEY={{ .Data.data.LITELLM_API_KEY }}
    {{ end -}}
    EOT
}
