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

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}

api_addr     = "http://vault:8200"
cluster_addr = "http://vault:8201"
ui           = true
