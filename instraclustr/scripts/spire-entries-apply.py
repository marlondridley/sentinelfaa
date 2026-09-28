#!/usr/bin/env python3
"""
Reconcile spire-entries.yaml against a live SPIRE server: create entries the
manifest lists but the server doesn't have, delete entries the server has
(under the manifest's parent) that the manifest no longer lists, replace an
entry whose selectors match but whose SPIFFE ID differs, leave matching ones
alone. Idempotent by design -- re-running with no manifest change makes no
calls at all.

This is the practical substitute for "there's no maintained official
Terraform provider for SPIRE entries" (see ../spire-entries.yaml's header):
the manifest is the single source of truth, and --check here is the same
kind of proof `terraform plan -detailed-exitcode` gives on the Vault side.

Usage, against the real deployment (from instraclustr/):
    python scripts/spire-entries-apply.py spire-entries.yaml \
        --exec-prefix "docker compose -f docker-compose.yml exec -T spire-server" \
        --check          # exit 0 no drift, 1 drift; changes nothing
Then without --check (add --yes to skip the confirmation prompt) once the
diff looks right. --exec-prefix also works unchanged against a throwaway
harness (docker exec into a disposable container) -- the reconcile logic
itself never assumes which.
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass

import yaml

Key = frozenset[str]


@dataclass(frozen=True)
class Identity:
    """What an entry hands the workload its selectors match."""
    spiffe_id: str
    dns_names: tuple[str, ...] = ()

    def __str__(self) -> str:
        return self.spiffe_id + (f" dns={list(self.dns_names)}" if self.dns_names else "")


def sh(prefix: str, args: list[str]) -> subprocess.CompletedProcess:
    cmd = (shlex.split(prefix) if prefix else []) + args
    return subprocess.run(cmd, capture_output=True, text=True)


def spiffe_id_str(d: dict) -> str:
    return f"spiffe://{d['trust_domain']}{d['path']}"


def selector_key(selectors: list[dict]) -> Key:
    return frozenset(f"{s['type']}:{s['value']}" for s in selectors)


def load_manifest(path: str) -> tuple[str, dict[Key, Identity]]:
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    parent_id = doc["parent_id"]
    desired: dict[Key, Identity] = {}
    for entry in doc["entries"]:
        key = frozenset(entry["selectors"])
        if key in desired:
            raise ValueError(f"duplicate selector set in manifest: {sorted(key)}")
        # Order matters: SPIRE makes the FIRST DNS name the SVID's CN.
        desired[key] = Identity(entry["spiffe_id"], tuple(entry.get("dns_names") or ()))
    return parent_id, desired


def load_live(exec_prefix: str, socket_path: str, parent_id: str) -> dict[Key, tuple[str, Identity]]:
    """Returns {selector_key: (entry_id, identity)} for entries under parent_id."""
    proc = sh(exec_prefix, ["/opt/spire/bin/spire-server", "entry", "show",
                            "-socketPath", socket_path, "-output", "json"])
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(f"spire-server entry show failed (exit {proc.returncode})")
    data = json.loads(proc.stdout or "{}")
    live: dict[Key, tuple[str, Identity]] = {}
    for e in data.get("entries", []):
        if spiffe_id_str(e["parent_id"]) != parent_id:
            continue  # not ours to manage (e.g. the node's own alias entries)
        live[selector_key(e["selectors"])] = (
            e["id"], Identity(spiffe_id_str(e["spiffe_id"]), tuple(e.get("dns_names") or ())))
    return live


@dataclass
class Plan:
    create: list[tuple[Key, Identity]]            # (selectors, identity)
    delete: list[tuple[Key, str, Identity]]       # (selectors, entry_id, identity)
    unchanged: list[Key]

    @property
    def drift(self) -> bool:
        return bool(self.create or self.delete)


def plan(desired: dict[Key, Identity], live: dict[Key, tuple[str, Identity]]) -> Plan:
    """
    Compare on the whole entry: selectors, SPIFFE ID and DNS names. Keying on
    selectors alone would call an entry whose identity changed "unchanged" --
    and leave the old identity live, still handed to that workload, while the
    manifest says otherwise. Such an entry is replaced: deleted, created again.
    """
    create, delete, unchanged = [], [], []
    for key, identity in desired.items():
        if key not in live:
            create.append((key, identity))
        elif live[key][1] != identity:
            delete.append((key, live[key][0], live[key][1]))
            create.append((key, identity))
        else:
            unchanged.append(key)
    for key, (entry_id, identity) in live.items():
        if key not in desired:
            delete.append((key, entry_id, identity))
    return Plan(create, delete, unchanged)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--parent-id",
                    help="Defaults to the manifest's parent_id; given, it must match it")
    ap.add_argument("--exec-prefix", default="")
    ap.add_argument("--socket-path", default="/tmp/spire-server/private/api.sock")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="Exit 1 if the live server differs from the manifest; change nothing")
    mode.add_argument("--dry-run", action="store_true", help="Show the diff; change nothing")
    ap.add_argument("--yes", action="store_true", help="Apply without a confirmation prompt")
    args = ap.parse_args(argv)

    parent_id, desired = load_manifest(args.manifest)
    if args.parent_id and args.parent_id != parent_id:
        raise SystemExit(f"--parent-id {args.parent_id} is not the manifest's parent_id {parent_id}")
    live = load_live(args.exec_prefix, args.socket_path, parent_id)
    p = plan(desired, live)

    print(f"desired={len(desired)} live={len(live)} unchanged={len(p.unchanged)} "
          f"to_create={len(p.create)} to_delete={len(p.delete)}")
    for key, identity in p.create:
        print(f"  + {identity}  selectors={sorted(key)}")
    for key, entry_id, identity in p.delete:
        print(f"  - {identity}  selectors={sorted(key)}  (entry {entry_id})")

    if not p.drift:
        print("no drift: manifest matches the live server exactly")
        return 0
    if args.check:
        print("DRIFT: the live server does not match the manifest")
        return 1
    if args.dry_run:
        print("dry-run: no changes applied")
        return 0

    if not args.yes:
        resp = input("Apply these changes? [y/N] ")
        if resp.strip().lower() != "y":
            print("aborted")
            return 1

    # Deletes first: a replaced entry's selectors are free before its new ID
    # is created, so the workload never holds two identities at once.
    for key, entry_id, identity in p.delete:
        proc = sh(args.exec_prefix, [
            "/opt/spire/bin/spire-server", "entry", "delete",
            "-socketPath", args.socket_path,
            "-entryID", entry_id,
        ])
        if proc.returncode != 0:
            print(proc.stdout, proc.stderr, file=sys.stderr)
            raise SystemExit(f"failed to delete entry {entry_id} ({identity})")
        print(f"deleted: {identity}  selectors={sorted(key)}  (was entry {entry_id})")

    for key, identity in p.create:
        selector_args = []
        for sel in sorted(key):
            selector_args += ["-selector", sel]
        for dns in identity.dns_names:
            selector_args += ["-dns", dns]
        proc = sh(args.exec_prefix, [
            "/opt/spire/bin/spire-server", "entry", "create",
            "-socketPath", args.socket_path,
            "-parentID", parent_id,
            "-spiffeID", identity.spiffe_id,
            *selector_args,
        ])
        if proc.returncode != 0:
            print(proc.stdout, proc.stderr, file=sys.stderr)
            raise SystemExit(f"failed to create entry for {identity}")
        print(f"created: {identity}  selectors={sorted(key)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
