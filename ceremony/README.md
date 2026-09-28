# Workload-identity ceremony

The one-time cutover to SPIFFE workload identity with mutual TLS to Vault. Run the steps in order, from the repository root, in PowerShell. Each script says at the top what it does and what success looks like. Stop at the first step that doesn't show its success line.

**Until step 4 is done, `launch_nas_sentinel.ps1` refuses to start**: it needs the certificates this creates. Don't restart the stack before you're ready to go through.

## Before you start

- [ ] The root USB drive plugged in (the scripts default to `D:\sentinelfaa-root-ca`; pass `-RootDir` for another location) and a second, backup USB drive.
- [ ] A new password-manager entry for the root passphrase, at least 12 characters, saved **before** step 1.
- [ ] Docker Desktop running ("Engine running").
- [ ] Git for Windows installed: its OpenSSL 3 runs the two steps that touch the USB.
- [ ] Python 3 with PyYAML (step 7).
- [ ] `RECORD.md` open, to fill in as you go.

## Steps

| # | Script | Needs | Touches |
|---|---|---|---|
| 1 | `.\ceremony\01-create-root.ps1` | USB, passphrase ×2 | the USB only |
| 2 | `.\ceremony\02-request-certificates.ps1` | Docker | `instraclustr\certs` |
| 3 | `.\ceremony\03-sign-certificates.ps1` | USB, passphrase ×1 | reads the USB; writes `instraclustr\certs` |
| — | copy `D:\sentinelfaa-root-ca` to the backup USB, then unplug both | | |
| 4 | `.\ceremony\04-install-certificates.ps1` | Docker | `instraclustr\certs` |
| 5 | `.\ceremony\05-start-infrastructure.ps1` | Docker | the infrastructure stack |
| 6 | `.\ceremony\06-vault-ceremony.ps1` | you type `yes` | Vault's auth and policies |
| 7 | `.\ceremony\07-spire-first-boot.ps1` | Python + PyYAML | spire-agent, SPIRE registrations |
| 8 | `.\ceremony\08-launch.ps1` | | everything |
| 9 | `.\ceremony\09-verify.ps1` | Git for Windows | reads only |

Steps 1 and 3 run natively with Git for Windows' OpenSSL, not in Docker. Docker Desktop doesn't share removable drives, and a folder on an unshared drive is silently swapped for an empty one on Docker's own disk. The root key never touches this computer's disk.

## After step 9 passes

- [ ] Remove the database passwords from `nas-feedops-sentinel\.env`. The application no longer reads them. Keep `instraclustr\.env` as it is.
- [ ] Move unseal share 3 (`instraclustr\vault-recovery\share-3`) to offline storage, then delete it here.
- [ ] Store both USB drives apart from this computer.

## What this is, stated plainly

The root key is created and used on this networked workstation. The OpenSSL process has no network access, and the key stays encrypted on the USB, but the workstation itself is online. Describe it as a *network-isolated* ceremony, not an air-gapped one. An air-gapped ceremony (root key only ever on a machine that is never networked) uses the same `pki.sh`; see `instraclustr\certs\README.md`.

## If something goes wrong

- **Step 1 says `root-ca.key exists without root-ca.crt`:** an earlier attempt stopped halfway, and that key was never used. Delete `root-ca.key` from the USB folder (nothing else), then run step 1 again.
- **"Docker is not running (or still starting)":** wait for "Engine running", then run the same step again.
- **A step fails:** nothing half-done is kept. Fix what it says and run the same step again. Paste the output if it isn't clear.
