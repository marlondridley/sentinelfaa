# Vault's workload-facing configuration, as code: how an application
# container proves who it is (cert auth over mutual TLS with its X.509-SVID)
# and what each identity may read. Applied only inside the root ceremony
# (ceremony.sh: a root token generated from two unseal shares, used, then
# revoked), and planned against a throwaway Vault in CI for drift.
#
# What is NOT here, deliberately: the KV mount, the AppRole mount and the
# vault-bootstrap role. vault-unseal-and-seed.bash creates those at first
# init, before any ceremony can run, because it needs them to seed the
# secrets the workloads read. Terraform does own vault-bootstrap's POLICY
# (from the same file root_setup() writes it from), so a drift in what the
# bootstrap identity may do is caught like any other.

terraform {
  required_version = ">= 1.9"
  required_providers {
    vault = {
      source  = "hashicorp/vault"
      version = "~> 5.0"
    }
  }
}

# Address, CA and token come from the ceremony's environment (VAULT_ADDR,
# VAULT_CACERT, VAULT_TOKEN) -- never from a file in this directory.
provider "vault" {}

variable "trust_domain" {
  description = "SPIFFE trust domain; must match instraclustr/spire/*.conf and certs/policy/signing.cnf."
  type        = string
  default     = "sentinelfaa.internal"
}

variable "root_ca_file" {
  description = "The offline workload-identity root (instraclustr/certs/README.md)."
  type        = string
  default     = "../certs/trust/root-ca.crt"
}

locals {
  # One Vault role per workload, each bound to exactly one SPIFFE ID -- the
  # same names as instraclustr/spire-entries.yaml (the tests pin that) -- so
  # Vault's audit log names the service that read a secret, not just its
  # class. Services of a class share that class's read-only policy.
  workloads = {
    "api"             = vault_policy.sentinel_app.name
    "dashboard"       = vault_policy.sentinel_app.name
    "sentinel"        = vault_policy.sentinel_app.name
    "temporal-worker" = vault_policy.sentinel_app.name
    "migrate"         = vault_policy.sentinel_migrate.name
    "proof"           = vault_policy.sentinel_migrate.name
  }
}

resource "vault_policy" "sentinel_app" {
  name   = "sentinel-app"
  policy = file("${path.module}/../vault-policies/sentinel-app.hcl")
}

resource "vault_policy" "sentinel_migrate" {
  name   = "sentinel-migrate"
  policy = file("${path.module}/../vault-policies/sentinel-migrate.hcl")
}

resource "vault_policy" "vault_bootstrap" {
  name   = "vault-bootstrap"
  policy = file("${path.module}/../vault-policies/vault-bootstrap.hcl")
}

# Every request and response Vault serves, with the identity that made it --
# for a sidecar, cert-<service> and its SVID's CN (NIST 800-53 AU-2/AU-12).
# Secret values in it are HMAC'd, never plain. Written to Vault's stdout, so
# `docker logs vault` has it and the container's log rotation bounds it
# (instraclustr/docker-compose.yml): Vault refuses any request it cannot
# audit, so an audit FILE left to fill the disk would stop Vault outright.
# Ship it off the host with the rest of the container logs for retention.
resource "vault_audit" "stdout" {
  type = "file"
  path = "stdout"
  options = {
    file_path = "stdout"
  }
}

resource "vault_auth_backend" "cert" {
  type        = "cert"
  path        = "cert"
  description = "SPIFFE X.509-SVIDs from spire-server, chained to the offline root"
}

resource "vault_cert_auth_backend_role" "workload" {
  for_each = local.workloads

  backend = vault_auth_backend.cert.path
  name    = each.key
  # The ROOT, not spire-server's own CA: SPIRE rotates its CA every week, the
  # root lasts ten years. The SVID's chain (leaf, SPIRE CA, upstream CA) is
  # presented by the client; the upstream CA's name constraints confine it to
  # this trust domain.
  certificate      = file(var.root_ca_file)
  allowed_uri_sans = ["spiffe://${var.trust_domain}/workload/${each.key}"]
  # Vault names the login's identity alias after the Subject CN (every Vault
  # version), so the SVID carries one: its entry's .invalid DNS name
  # (spire-entries.yaml). Requiring it here too makes the alias in Vault's
  # audit log exactly this workload's name.
  allowed_common_names = ["${each.key}.sentinelfaa.invalid"]

  token_policies = [each.value]
  # Vault Agent renews within token_ttl and re-authenticates with the current
  # SVID (it rotates hourly) once token_max_ttl is reached.
  token_ttl     = 900
  token_max_ttl = 3600
}
