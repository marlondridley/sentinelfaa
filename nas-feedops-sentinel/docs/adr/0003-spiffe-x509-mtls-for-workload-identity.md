# 0003. Workload identity is SPIFFE X.509-SVID over mutual TLS

Status: Accepted; live since the 2026-09-28 ceremony

## Context
Services need to authenticate to Vault to fetch secrets. The first design used
Vault AppRoles. The system is aimed at federal buyers, where a plaintext Vault is
a standalone finding against encrypted transport (NIST SC-8, OMB M-22-09), and a
long-lived shared secret is a weak way to prove identity.

## Decision
Each service has its own SPIFFE ID and authenticates to Vault with an
X.509-SVID over mutual TLS (Vault `cert` auth). Vault serves TLS only. Each
service gets a `<svc>.sentinelfaa.invalid` CN, which Vault uses to name its
alias, and a spiffe-helper plus Vault Agent sidecar writing to tmpfs volumes. The
root CA is offline on removable media, passphrase-protected, under a written
custody matrix (`instraclustr/certs/README.md`). The AppRoles were retired in the
cutover.

## Alternatives considered
- **JWT-SVIDs.** Rejected: a bearer token that can be replayed if captured.
- **AppRole with response wrapping.** Rejected: a secret to distribute and rotate,
  and it identifies a role rather than a workload.
- **Shared service credentials in the environment.** Rejected: no per-service
  identity and no revocation of one service without all.

## Consequences
- Identity is per service and short-lived, rotated by SPIRE.
- More moving parts: SPIRE server and agent, two sidecars per service, a
  ceremony for the root. After a Docker restart an expired SPIRE agent must be
  re-attested before the stack starts.

## Evidence
Live negative probe suite 18/18 on 2026-09-28; probes 01 to 03 and
`run_phase6_smoke_suite.sh` pass live; a CI job checks SPIRE configuration
drift (PRs 13 and 14).

## Limits
- The ceremony was network-isolated, not air-gapped: there is no offline machine.
- Identity covers service-to-Vault. Not every east-west hop is encrypted yet:
  Postgres TLS and Temporal mTLS are open, and the Docker network is flat. This is
  planned as a final, watched cutover because it touches the live identity plane.
- The local CA is not yet constrained by name or by extended key usage.
- Vault 1.15.2 is end of life; the upgrade is deferred.
