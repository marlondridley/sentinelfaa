# Workload-identity PKI

The trust anchor for how application containers prove who they are to Vault. Each service's sidecar holds a short-lived X.509-SVID from SPIRE and presents it to Vault over mutual TLS. Vault's own TLS certificate comes from the same root. Nothing here is a bearer credential: a stolen certificate is useless without its private key, and no private key ever leaves the container or USB drive that holds it.

```
root CA              10 years, P-384, passphrase-encrypted     OFFLINE (USB + backup USB)
 ├─ SPIRE upstream CA  1 year, P-384                             certs/spire  -> spire-server
 │   └─ SPIRE's CA       7 days, rotated by spire-server
 │       └─ X.509-SVIDs    1 hour, rotated by spire-agent        (memory only, per service)
 └─ Vault server cert  1 year, P-256                             certs/vault  -> vault
```

Vault's cert auth trusts the root, not SPIRE's CA, so SPIRE's weekly rotation never needs a Vault change. The root signs exactly two things, once a year.

The policy is [`policy/signing.cnf`](policy/signing.cnf), and `pki.sh sign` applies it whatever a request asks for. The SPIRE branch is name-constrained:

- it may issue `spiffe://sentinelfaa.internal/...` identities;
- its only DNS names are under the reserved `.invalid` TLD;
- it may issue no IP address at all.

So even a compromised SPIRE server can't mint a certificate that any client would accept as Vault or any other real host. `instraclustr/tests/test_pki.py` proves each of those with real TLS handshakes.

## Custody

| File | Where | Sensitivity | On this host? | In git? | Mounted into |
|---|---|---|---|---|---|
| `root-ca.key` | Offline USB + backup USB | 🔴 Critical | ❌ Never | ❌ | Nothing (read in place from the USB during `sign`) |
| Root passphrase | Password manager | 🔴 Critical | ❌ | ❌ | Nothing (typed at the prompt) |
| `root-ca.srl` | USB, next to the root | 🟢 Public | ❌ | ❌ | Nothing |
| `root.cnf`, `signing.cnf` | USB + `certs/policy/` | 🟢 Public (the CA policy) | ✅ | ✅ Committed | Nothing |
| `root-ca.crt` | `certs/trust/` | 🟢 Public trust anchor | ✅ | ❌ (per environment) | spire-server, spire-agent, vault (health check), vault-unseal, litellm-bootstrap, vault-ceremony, every Vault Agent sidecar |
| `intermediate-ca.crt` | `certs/spire/` | 🟢 Public | ✅ | ❌ | spire-server only |
| `intermediate-ca.key` | `certs/spire/` | 🔴 High | ✅ root 0600 | ❌ | spire-server only |
| `vault.crt` | `certs/vault/` | 🟢 Public | ✅ | ❌ | vault only |
| `vault.key` | `certs/vault/` | 🔴 High | ✅ uid 100 0400 | ❌ | vault only |
| `*.csr`, `*.pending` | `certs/spire/`, `certs/vault/` | 🟢 CSRs public; pending keys High | Only during a ceremony | ❌ | Nothing |

`.gitignore` keeps everything here except `policy/` and this file out of git.

## The ceremony

Commands are PowerShell from the repository root. `root-init` and `sign`, the steps that touch the root's USB, run natively with Git for Windows' OpenSSL 3. Docker Desktop does not share removable drives, and a folder on an unshared drive is silently swapped for an empty one on Docker's own disk. `request`, `install` and `check` run in a throwaway `postgres:16` container (already in the stack, ships OpenSSL 3) with **no network**. `E:\sentinelfaa-root` stands for the root's folder on the USB drive.

### First time

1. **Create the root, offline, once.** Ideally do this on a machine that has never been online; at minimum, with the network off.
   ```powershell
   .\instraclustr\scripts\pki.ps1 root-init -RootDir E:\sentinelfaa-root
   ```
   Choose a long passphrase and store it in the password manager. Copy `E:\sentinelfaa-root` to the backup USB, and put both USBs away.
2. **Request, on this host.** A fresh key for the SPIRE upstream CA and one for Vault. The keys stay here; the CSRs are public.
   ```powershell
   .\instraclustr\scripts\pki.ps1 request
   ```
3. **Sign, with the root USB plugged in.** This asks for the passphrase. The root key is read from the USB and never copied to this disk. It writes `*.crt.pending`, deletes the CSRs, and installs `trust/root-ca.crt` the first time.
   ```powershell
   .\instraclustr\scripts\pki.ps1 sign -RootDir E:\sentinelfaa-root
   ```
   Unplug the USB. (Air-gapped signing machine instead: carry `certs\spire\*.csr` and `certs\vault\*.csr` there, keeping the folder names. Run `pki.sh sign <root-dir> <that folder>` with OpenSSL 3, then carry the `*.crt.pending` files and `root-ca.crt` back.)
4. **Install, on this host.** Each certificate is checked against its pending key, the root and the policy before anything is replaced.
   ```powershell
   .\instraclustr\scripts\pki.ps1 install
   .\instraclustr\scripts\pki.ps1 check      # 0 current, 1 renewal due, 2 broken
   ```
5. Continue with first-time setup in [`../README.md`](../README.md#first-time-setup).

### Every year

`launch_nas_sentinel.ps1` runs `pki.ps1 check` on every start and warns 60 days before anything expires. To renew, run `request`, `sign` and `install` as above, then pick up the new certificates:

```powershell
docker kill --signal HUP vault     # Vault reloads its TLS certificate on SIGHUP: no restart, no re-seal
docker restart spire-server        # the upstream CA is read at start; agents reconnect by themselves
```

Each renewal generates new keys; the old ones are overwritten.

### If something is lost

- **Root key or passphrase lost** (both USBs, or the passphrase): nothing breaks until the next renewal is due. Before then, create a new root. Vault's cert auth trusts the root through `terraform/`, so that means a new ceremony and a Terraform apply.
- **`certs/` lost on this host:** run `request`, `sign`, `install`. The root is unchanged, so nothing else changes.
- **A host key exposed** (`intermediate-ca.key` or `vault.key`): renew now; this replaces the key. For the SPIRE key, also restart spire-agent so every SVID is re-issued under the new CA.
