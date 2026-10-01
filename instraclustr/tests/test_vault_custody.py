"""
scripts/vault_custody.py: where the unseal shares live, and whether a root token is around. Read-only.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parent.parent

# A made-up token. Assembled here, never written whole in a source line: a literal is token-shaped, and gitleaks
# (which reads every branch's history) rightly cannot tell a fixture from a leak.
FAKE_TOKEN = "hvs" + "." + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
SECRET_ENV_TOKEN = "hvs" + "." + "SECRETSECRETSECRETSECRET1"
_spec = importlib.util.spec_from_file_location("vault_custody", INFRA / "scripts" / "vault_custody.py")
vc = importlib.util.module_from_spec(_spec)
sys.modules["vault_custody"] = vc          # a dataclass resolves its own module by name
_spec.loader.exec_module(vc)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A checkout as it should be: share 1 present, share 3 gone, no token anywhere."""
    (tmp_path / "instraclustr" / "vault-init-data" / "unseal").mkdir(parents=True)
    (tmp_path / "instraclustr" / "vault-init-data" / "unseal" / "share-1").write_text("not-a-real-share")
    (tmp_path / "instraclustr" / "vault-init-data" / "bootstrap").mkdir()
    (tmp_path / "instraclustr" / "vault-init-data" / "bootstrap" / "role_id").write_text("11111111-2222-3333")
    (tmp_path / "instraclustr" / "vault-recovery").mkdir()
    (tmp_path / "instraclustr" / "vault-recovery" / "README.txt").write_text("move share 3 off this machine")
    (tmp_path / "instraclustr" / "terraform").mkdir()
    (tmp_path / "instraclustr" / "terraform" / "main.tf").write_text('resource "vault_policy" "x" {}')
    return tmp_path


def _docker(volume_exists: bool):
    return lambda cmd: subprocess.CompletedProcess(
        cmd, 0, "instraclustr_vault-unseal-share\n" if volume_exists else "", "")


def _by_name(checks):
    return {c.name: c for c in checks}


def test_a_checkout_as_it_should_be_has_no_findings(tree):
    checks = _by_name(vc.run_checks(tree, docker=_docker(True)))
    assert checks["share 1"].status == vc.OK and "by design" in checks["share 1"].detail
    assert checks["share 2"].status == vc.OK
    assert checks["share 3"].status == vc.OK
    assert checks["root token stored"].status == vc.OK
    assert vc.exit_status(list(checks.values())) == 0


def test_share_3_left_on_this_machine_is_a_finding_and_says_what_to_do(tree):
    (tree / "instraclustr" / "vault-recovery" / "share-3").write_text("not-a-real-share")
    check = _by_name(vc.run_checks(tree, docker=_docker(True)))["share 3"]
    assert check.status == vc.FINDING and "share-3" in check.detail and "Move it" in check.detail


def test_a_missing_share_or_volume_is_unknown_not_ok(tmp_path):
    checks = _by_name(vc.run_checks(tmp_path, docker=_docker(False)))
    assert checks["share 1"].status == vc.UNKNOWN and checks["share 2"].status == vc.UNKNOWN


def test_provider_binaries_and_large_files_are_not_scanned(tree):
    provider = tree / "instraclustr" / "terraform" / ".terraform" / "providers"
    provider.mkdir(parents=True)
    (provider / "terraform-provider-vault").write_text('"root_token" ' + FAKE_TOKEN)
    (tree / "instraclustr" / "terraform" / "big.bin").write_text("root_token" + "x" * 2_100_000)
    assert _by_name(vc.run_checks(tree, docker=_docker(True)))["root token stored"].status == vc.OK


def test_a_file_named_for_a_root_token_is_a_finding(tree):
    (tree / "instraclustr" / "vault-init-data" / "root_token").write_text("whatever")
    check = _by_name(vc.run_checks(tree, docker=_docker(True)))["root token stored"]
    assert check.status == vc.FINDING and "root_token is named for a root token" in check.detail


def test_a_token_shaped_string_is_found_and_never_printed(tree):
    secret = FAKE_TOKEN
    (tree / "instraclustr" / "terraform" / "terraform.tfstate").write_text('{"token": "%s"}' % secret)
    (tree / "instraclustr" / "vault-init-data" / "init.json").write_text('{"root_token": "x"}')
    check = _by_name(vc.run_checks(tree, docker=_docker(True)))["root token stored"]
    assert check.status == vc.FINDING
    assert "terraform.tfstate has 1 token-shaped" in check.detail and "init.json has" in check.detail
    assert secret not in check.detail and "A1b2C3" not in check.detail


def test_the_share_and_approle_folders_are_not_read(tree):
    reads: list[Path] = []
    original = Path.read_text

    def spy(self, *a, **k):
        reads.append(self)
        return original(self, *a, **k)

    Path.read_text = spy
    try:
        vc.run_checks(tree, docker=_docker(True))
    finally:
        Path.read_text = original
    assert not [p for p in reads if "unseal" in p.parts or "bootstrap" in p.parts], reads


def test_env_files_and_secrets_toml_are_never_read(tree):
    sensitive = tree / "instraclustr" / "terraform" / ".env"
    sensitive.write_text("VAULT_TOKEN=" + FAKE_TOKEN)
    reads: list[Path] = []
    original = Path.read_text

    def spy(self, *a, **k):
        reads.append(self)
        return original(self, *a, **k)

    Path.read_text = spy
    try:
        checks = vc.run_checks(tree, docker=_docker(True))
    finally:
        Path.read_text = original
    assert sensitive not in reads
    assert _by_name(checks)["root token stored"].status == vc.OK


# ── the live Vault ─────────────────────────────────────────────────────────────

def _vault(seal=None, lookup=None):
    seal = seal or {"initialized": True, "sealed": False, "type": "shamir", "t": 2, "n": 3}

    def get(url, headers, cacert):
        if url.endswith("/v1/sys/seal-status"):
            return 200, seal
        assert url.endswith("/v1/auth/token/lookup-self")
        return lookup(headers["X-Vault-Token"])

    return get


def test_a_threshold_of_two_of_three_is_as_designed(tree):
    checks = _by_name(vc.run_checks(tree, addr="https://v:8200", docker=_docker(True), get=_vault()))
    assert checks["seal"].status == vc.OK and "threshold 2 of 3" in checks["seal"].detail


def test_a_threshold_of_one_is_a_finding(tree):
    get = _vault(seal={"initialized": True, "sealed": False, "type": "shamir", "t": 1, "n": 1})
    assert _by_name(vc.run_checks(tree, addr="https://v:8200", docker=_docker(True), get=get))["seal"].status == vc.FINDING


def test_a_live_root_token_is_a_finding(tree):
    get = _vault(lookup=lambda token: (200, {"data": {"policies": ["root"]}}))
    check = _by_name(vc.run_checks(tree, addr="https://v:8200", token="t", docker=_docker(True), get=get))["root token"]
    assert check.status == vc.FINDING and "LIVE ROOT" in check.detail


def test_a_live_token_that_is_not_root_is_fine(tree):
    get = _vault(lookup=lambda token: (200, {"data": {"policies": ["default", "vault-bootstrap"]}}))
    check = _by_name(vc.run_checks(tree, addr="https://v:8200", token="t", docker=_docker(True), get=get))["root token"]
    assert check.status == vc.OK and "not root" in check.detail


@pytest.mark.parametrize("code", [400, 403])
def test_a_refused_token_is_a_revoked_one(tree, code):
    get = _vault(lookup=lambda token: (code, {"errors": ["permission denied"]}))
    check = _by_name(vc.run_checks(tree, addr="https://v:8200", token="t", docker=_docker(True), get=get))["root token"]
    assert check.status == vc.OK and "revoked or expired" in check.detail


def test_without_a_token_the_script_says_it_cannot_show_revocation(tree):
    check = _by_name(vc.run_checks(tree, addr="https://v:8200", docker=_docker(True), get=_vault()))["root token"]
    assert check.status == vc.UNKNOWN and "Vault cannot list root tokens" in check.detail


def test_an_unreachable_vault_is_unknown(tree):
    def down(url, headers, cacert):
        raise ConnectionRefusedError

    checks = vc.run_checks(tree, addr="https://v:8200", docker=_docker(True), get=down)
    assert _by_name(checks)["vault"].status == vc.UNKNOWN


def test_the_token_is_never_in_the_output(tree, capsys, monkeypatch):
    monkeypatch.setenv("VAULT_TOKEN", SECRET_ENV_TOKEN)
    checks = vc.run_checks(tree, addr="https://v:8200", token=SECRET_ENV_TOKEN, docker=_docker(True),
                           get=_vault(lookup=lambda t: (200, {"data": {"policies": ["root"]}})))
    assert all("SECRETSECRET" not in f"{c.name} {c.detail}" for c in checks)


def test_the_exit_status_separates_findings_from_not_knowing():
    ok, bad, unk = vc.Check("a", vc.OK, ""), vc.Check("b", vc.FINDING, ""), vc.Check("c", vc.UNKNOWN, "")
    assert vc.exit_status([ok]) == 0 and vc.exit_status([ok, unk]) == 2 and vc.exit_status([ok, unk, bad]) == 1
