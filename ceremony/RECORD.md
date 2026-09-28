# Ceremony record

Fill in on the day and keep with the ceremony's output. A copy goes with the USB drives; this template stays in the repository blank. Never write the passphrase or any key here.

| | |
|---|---|
| Date and time (UTC) | |
| Location | |
| Operator | |
| Witness(es) | |
| Workstation (hostname) | |
| Ceremony type | network-isolated (not air-gapped): see README |
| Root USB (make, model, serial) | |
| Backup USB (make, model, serial) | |
| Passphrase stored in | (password manager and entry name, not the passphrase) |

## Fingerprints

Print each with:

```powershell
& "C:\Program Files\Git\mingw64\bin\openssl.exe" x509 -in <file> -noout -subject -enddate -fingerprint -sha256
```

| Certificate | File | SHA-256 fingerprint | Expires |
|---|---|---|---|
| Root CA | `D:\sentinelfaa-root-ca\root-ca.crt` | | |
| SPIRE upstream CA | `instraclustr\certs\spire\intermediate-ca.crt` | | |
| Vault server | `instraclustr\certs\vault\vault.crt` | | |

## Steps

| # | Step | Result (success line or error) | Initials |
|---|---|---|---|
| 1 | create root | | |
| 2 | request certificates | | |
| 3 | sign certificates | | |
| — | backup USB copied, both unplugged | | |
| 4 | install and check | | |
| 5 | start infrastructure | | |
| 6 | Vault ceremony (plan read, apply) | | |
| 7 | SPIRE first boot | | |
| 8 | launch | | |
| 9 | verify: negative access probe | | |

## After

| | Done (date, initials) |
|---|---|
| Database passwords removed from `nas-feedops-sentinel\.env` | |
| Unseal share 3 moved offline and deleted here | |
| USB drives stored apart from the workstation | |
| Next renewal due (60 days before the earliest expiry above) | |
