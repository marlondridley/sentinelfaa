# Real (non-dev) Vault server config. Replaces the VAULT_DEV_* env vars that
# previously put this container in -dev mode -- convenient (auto-unsealed,
# no init step) but purely in-memory, so every container restart wiped every
# secret vault-init.bash had seeded. Raft integrated storage is the current
# HashiCorp-recommended backend (the older standalone "file" backend still
# works but raft is the forward-looking default, and works fine single-node,
# not just clustered) -- it writes to /vault/file (see the comment further
# down for why that path, not /vault/data), which docker-compose.yml mounts
# as a real named volume, so secrets now survive a restart.
#
# Trade-off this brings: Vault now starts SEALED and, on first ever boot,
# UNINITIALIZED. Nothing can read secrets from a sealed Vault. The
# vault-unseal service (see vault-unseal-and-seed.bash) handles both the
# one-time init and the unseal-on-every-restart step automatically.

# Storage path is /vault/file, not the more common-looking /vault/data --
# deliberately. The hashicorp/vault:1.15.2 image's docker-entrypoint.sh only
# auto-chowns three bind/volume-mounted directories to the non-root "vault"
# user it runs as: /vault/config, /vault/logs, and /vault/file. Any other
# path (including /vault/data, which is what HashiCorp's own docs example
# uses -- but that example bakes a custom image with its own explicit
# `chown -R vault:vault /vault` step, which this project doesn't do) stays
# root-owned on a fresh named volume, so the vault user gets a permission
# error writing Raft data and the container exits immediately. /vault/file
# is just a directory path here, not a reference to the legacy "file"
# storage backend -- raft doesn't care what the path is named, and reusing
# this exact name is what makes the stock image's chown logic apply to it.
storage "raft" {
  path    = "/vault/file"
  node_id = "vault-1"
}

# TLS only. Every secret Vault returns, and every credential presented to it,
# is encrypted in transit (NIST 800-53 SC-8; OMB M-22-09 asks for internal
# HTTP traffic to be encrypted too). The certificate is issued by the offline
# workload-identity root (instraclustr/certs/README.md); every client
# verifies it against certs/trust/root-ca.crt.
#
# Client certificates are requested but not required at the listener: the
# cert auth method (instraclustr/terraform/) is what verifies an application
# sidecar's X.509-SVID -- its chain to the root and its exact spiffe:// ID --
# while the bootstrap jobs (vault-unseal, litellm-bootstrap) authenticate
# with their AppRole over the same encrypted connection.
listener "tcp" {
  address                            = "0.0.0.0:8200"
  tls_cert_file                      = "/vault/tls/vault.crt"
  tls_key_file                       = "/vault/tls/vault.key"
  tls_min_version                    = "tls12"
  tls_require_and_verify_client_cert = false
  tls_disable_client_certs           = false
}

api_addr     = "https://vault:8200"
cluster_addr = "https://vault:8201"
ui           = true
