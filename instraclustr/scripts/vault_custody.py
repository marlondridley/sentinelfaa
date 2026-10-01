#!/usr/bin/env python3
"""
Where Vault's unseal shares live, and whether a root token is still around. Read-only.

    python instraclustr/scripts/vault_custody.py
    python instraclustr/scripts/vault_custody.py --vault-addr https://localhost:8200 --cacert instraclustr/certs/root-ca.crt
    VAULT_TOKEN=<a token you hold> python instraclustr/scripts/vault_custody.py --vault-addr ...

What it checks (docs/operations.md, "Vault unseal and root"):
  shares   share 1 in vault-init-data/unseal/ and share 2 in the vault-unseal-share volume are on this machine by
           design (they auto-unseal Vault). Share 3, in vault-recovery/, must be moved off it and deleted: while it
           is here, any two shares are here and anyone with this disk can unseal Vault.
  root     no root token is stored: nothing under vault-init-data/ or terraform/ looks like a token, and no file is
           named for one. A token you pass in VAULT_TOKEN is asked what it is (token lookup-self); a root one is a
           finding, a refused one means it is revoked.
  vault    the seal's threshold and share count, from the unauthenticated seal-status endpoint, when --vault-addr is given.

It never prints a secret and reads no .env or secrets.toml: it says which files exist and what the pattern search
counted, nothing it matched. Exit status 0: nothing to do. 1: a finding. 2: it could not tell.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

OK, FINDING, UNKNOWN = "ok", "FINDING", "unknown"

# Vault tokens: hvs./hvb./hvr. (1.10+), s./b./r. (older), and the field names `vault operator init` prints.
_TOKEN_LIKE = re.compile(r"\bhv[sbr]\.[A-Za-z0-9_-]{20,}|\"root_token\"|\broot_token\b|Initial Root Token", re.IGNORECASE)
_NEVER_READ = {".env", "secrets.toml"}
# Where the unseal share and the bootstrap AppRole credential are kept: their names are checked, their contents are
# not read at all. A `vault operator init` output, the thing that would hold a root token, is not one of these.
_KEY_FOLDERS = {"unseal", "bootstrap"}
# Downloaded provider binaries contain token-shaped strings (a regular expression compiled into a Go program) and
# are not where a token is written down.
_SKIP_FOLDERS = {".terraform"}
_MAX_BYTES = 2_000_000


@dataclass
class Check:
    name: str
    status: str
    detail: str


def _files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*") if p.is_file()) if directory.is_dir() else []


def _describe(path: Path) -> str:
    stat = path.stat()
    when = datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y-%m-%d")
    return f"{path.name} ({stat.st_size} bytes, written {when})"


def check_share1(root: Path) -> Check:
    found = _files(root / "instraclustr" / "vault-init-data" / "unseal")
    if not found:
        return Check("share 1", UNKNOWN, "vault-init-data/unseal/ is empty or absent: Vault cannot auto-unseal from it")
    return Check("share 1", OK, "on this machine, by design (auto-unseal): " + ", ".join(_describe(p) for p in found)
                 + ". Restrict who can read it: scripts/Protect-KeyMaterial.ps1")


def check_share2(run: Callable[[list[str]], subprocess.CompletedProcess] | None = None) -> Check:
    if run is None and shutil.which("docker") is None:
        return Check("share 2", UNKNOWN, "docker is not available here, so the vault-unseal-share volume was not looked for")
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=20))
    try:
        # Compose prefixes the project name (instraclustr_vault-unseal-share), so match the volume by its suffix.
        result = run(["docker", "volume", "ls", "--quiet", "--filter", "name=vault-unseal-share"])
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("share 2", UNKNOWN, f"could not ask docker: {type(exc).__name__}")
    if result.returncode == 0 and "vault-unseal-share" in (result.stdout or ""):
        return Check("share 2", OK, "in the vault-unseal-share volume, by design (mounted only into the unseal job)")
    return Check("share 2", UNKNOWN, "the vault-unseal-share volume was not found: Vault cannot auto-unseal without it")


def check_share3(root: Path) -> Check:
    found = [p for p in _files(root / "instraclustr" / "vault-recovery") if p.name.lower() != "readme.txt"]
    if found:
        return Check("share 3", FINDING, "still on this machine: " + ", ".join(_describe(p) for p in found)
                     + ". Move it to storage off this machine (a password manager, offline media), then delete it here.")
    return Check("share 3", OK, "not on this machine (vault-recovery/ holds only its README): "
                 "so no two shares can be put together from this disk and the volume alone")


def check_no_stored_root_token(root: Path) -> Check:
    problems: list[str] = []
    scanned = 0
    for folder in ("instraclustr/vault-init-data", "instraclustr/terraform"):
        for path in _files(root / folder):
            if path.name in _NEVER_READ:
                continue
            if "root" in path.name.lower():
                problems.append(f"{path.relative_to(root).as_posix()} is named for a root token")
                continue
            parts = set(path.relative_to(root).parts)
            if (_KEY_FOLDERS | _SKIP_FOLDERS) & parts or path.stat().st_size > _MAX_BYTES:
                continue
            try:
                count = len(_TOKEN_LIKE.findall(path.read_text(encoding="utf-8", errors="ignore")))
            except OSError:
                continue
            scanned += 1
            if count:
                problems.append(f"{path.relative_to(root).as_posix()} has {count} token-shaped match(es) (not shown)")
    if problems:
        return Check("root token stored", FINDING, "; ".join(problems))
    return Check("root token stored", OK, f"nothing token-shaped in {scanned} file(s) under vault-init-data/ and terraform/ "
                 "(the share and AppRole folders and the .env files are not read; look for VAULT_TOKEN by name yourself)")


def _get(url: str, headers: dict[str, str], cacert: str | None) -> tuple[int, dict]:
    context = ssl.create_default_context(cafile=cacert) if url.startswith("https") else None
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10, context=context) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")
        except ValueError:
            return exc.code, {}


def check_vault(addr: str, cacert: str | None, token: str | None,
                get: Callable[[str, dict[str, str], str | None], tuple[int, dict]] = _get) -> list[Check]:
    checks: list[Check] = []
    try:
        status, seal = get(f"{addr}/v1/sys/seal-status", {}, cacert)
    except (OSError, ValueError) as exc:
        return [Check("vault", UNKNOWN, f"could not reach {addr}: {type(exc).__name__}")]
    if status != 200:
        return [Check("vault", UNKNOWN, f"seal-status answered {status}")]
    threshold, shares = seal.get("t"), seal.get("n")
    detail = (f"initialized={seal.get('initialized')} sealed={seal.get('sealed')} type={seal.get('type')} "
              f"threshold {threshold} of {shares} shares")
    checks.append(Check("seal", FINDING if (threshold or 0) < 2 else OK,
                        detail + ("" if (threshold or 0) >= 2 else ": one share unseals Vault")))
    if token:
        code, body = get(f"{addr}/v1/auth/token/lookup-self", {"X-Vault-Token": token}, cacert)
        if code == 200:
            data = body.get("data") or {}
            policies = set(data.get("policies") or [])
            if "root" in policies:
                checks.append(Check("root token", FINDING, "the token you passed is a LIVE ROOT token: revoke it "
                                    "(vault token revoke -self), then run the ceremony for the work that needed it"))
            else:
                checks.append(Check("root token", OK, f"the token you passed is live and is not root (policies: "
                                    f"{', '.join(sorted(policies)) or 'none'})"))
        elif code in (400, 403):
            checks.append(Check("root token", OK, f"the token you passed is refused ({code}): it is revoked or expired"))
        else:
            checks.append(Check("root token", UNKNOWN, f"lookup-self answered {code}"))
    else:
        checks.append(Check("root token", UNKNOWN, "no token passed (VAULT_TOKEN), so none was asked what it is: "
                            "Vault cannot list root tokens, so 'revoked' can only be shown for a token you hold"))
    return checks


def run_checks(root: Path, *, addr: str | None = None, cacert: str | None = None, token: str | None = None,
               docker: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
               get: Callable[[str, dict[str, str], str | None], tuple[int, dict]] = _get) -> list[Check]:
    checks = [check_share1(root), check_share2(docker), check_share3(root), check_no_stored_root_token(root)]
    if addr:
        checks += check_vault(addr, cacert, token, get)
    return checks


def exit_status(checks: list[Check]) -> int:
    if any(c.status == FINDING for c in checks):
        return 1
    return 2 if any(c.status == UNKNOWN for c in checks) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vault-addr", help="e.g. https://localhost:8200; adds the seal and the token checks")
    parser.add_argument("--cacert", default=os.environ.get("VAULT_CACERT"), help="CA certificate for a Vault on TLS")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent.parent
    checks = run_checks(root, addr=args.vault_addr, cacert=args.cacert, token=os.environ.get("VAULT_TOKEN"))
    for check in checks:
        print(f"  {check.status:8} {check.name:18} {check.detail}")
    code = exit_status(checks)
    print({0: "\nNothing to do.", 1: "\nFindings above need action.", 2: "\nSome checks could not tell; see unknown."}[code])
    return code


if __name__ == "__main__":
    sys.exit(main())
