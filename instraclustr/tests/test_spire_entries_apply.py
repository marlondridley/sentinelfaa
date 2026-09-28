"""
scripts/spire-entries-apply.py: the drift engine for SPIRE registrations.

The case that matters most is the one it used to miss: an entry whose
selectors still match but whose identity (SPIFFE ID or DNS names) changed.
Keyed on selectors alone, it reported "no drift" and left the OLD identity
live, still issued to that workload -- exactly what moving from one shared
sentinel-app ID to per-service IDs would have hit.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("spire_entries_apply", INFRA / "scripts" / "spire-entries-apply.py")
apply = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = apply    # dataclasses resolve annotations through it
_spec.loader.exec_module(apply)

API = frozenset({"docker:label:com.sentinelfaa.identity:api"})
MIGRATE = frozenset({"docker:label:com.sentinelfaa.identity:migrate"})


def ident(name: str, dns: bool = True) -> "apply.Identity":
    return apply.Identity(f"spiffe://sentinelfaa.internal/workload/{name}",
                          (f"{name}.sentinelfaa.invalid",) if dns else ())


def test_matching_server_is_no_drift():
    p = apply.plan({API: ident("api")}, {API: ("e1", ident("api"))})
    assert not p.drift and p.unchanged == [API]


def test_missing_entry_is_created_and_extra_entry_deleted():
    p = apply.plan({API: ident("api")}, {MIGRATE: ("e2", ident("migrate"))})
    assert p.create == [(API, ident("api"))]
    assert p.delete == [(MIGRATE, "e2", ident("migrate"))]


def test_changed_spiffe_id_under_the_same_selectors_is_replaced():
    shared = apply.Identity("spiffe://sentinelfaa.internal/workload/sentinel-app", ())
    p = apply.plan({API: ident("api")}, {API: ("e1", shared)})
    assert p.drift
    assert p.delete == [(API, "e1", shared)] and p.create == [(API, ident("api"))]


def test_changed_dns_names_under_the_same_id_are_replaced():
    p = apply.plan({API: ident("api")}, {API: ("e1", ident("api", dns=False))})
    assert p.delete == [(API, "e1", ident("api", dns=False))] and p.create == [(API, ident("api"))]


def test_manifest_loads_dns_names_in_order(tmp_path):
    manifest = tmp_path / "m.yaml"
    manifest.write_text(
        "parent_id: spiffe://sentinelfaa.internal/agent/nas-host\n"
        "entries:\n"
        "  - spiffe_id: spiffe://sentinelfaa.internal/workload/api\n"
        "    dns_names: [api.sentinelfaa.invalid, second.invalid]\n"
        "    selectors: [docker:label:com.sentinelfaa.identity:api]\n", encoding="utf-8")
    parent, desired = apply.load_manifest(str(manifest))
    assert parent == "spiffe://sentinelfaa.internal/agent/nas-host"
    assert desired[API].dns_names == ("api.sentinelfaa.invalid", "second.invalid"), "the first becomes the CN"


def test_the_real_manifest_loads():
    parent, desired = apply.load_manifest(str(INFRA / "spire-entries.yaml"))
    assert parent == "spiffe://sentinelfaa.internal/agent/nas-host" and len(desired) == 6


@pytest.mark.parametrize("live, code", [({API: ("e1", ident("api"))}, 0), ({}, 1)])
def test_check_exits_1_on_drift_and_changes_nothing(monkeypatch, tmp_path, live, code):
    manifest = tmp_path / "m.yaml"
    manifest.write_text(
        "parent_id: spiffe://sentinelfaa.internal/agent/nas-host\n"
        "entries:\n"
        "  - spiffe_id: spiffe://sentinelfaa.internal/workload/api\n"
        "    dns_names: [api.sentinelfaa.invalid]\n"
        "    selectors: [docker:label:com.sentinelfaa.identity:api]\n", encoding="utf-8")
    monkeypatch.setattr(apply, "load_live", lambda *a: live)
    monkeypatch.setattr(apply, "sh", lambda *a: pytest.fail("--check must not call spire-server to change anything"))
    assert apply.main([str(manifest), "--check"]) == code


def test_a_parent_id_that_contradicts_the_manifest_is_refused(tmp_path):
    with pytest.raises(SystemExit, match="not the manifest's parent_id"):
        apply.main([str(INFRA / "spire-entries.yaml"), "--parent-id", "spiffe://other/agent/x", "--check"])
