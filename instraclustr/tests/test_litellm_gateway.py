"""
The model gateway's secrets come from Vault, and its spend record and metrics are what the docs say.

The Anthropic key, the LiteLLM master key and the litellm database password live at secret/litellm. The gateway reads
them through its own Vault Agent (vault-agent/litellm.hcl, policy vault-policies/litellm.hcl), the database jobs read
the password from the same rendered file, litellm-bootstrap reads the master key from Vault, and none of them is in
compose's environment or set in .env.example. The seed job keeps a value Vault has, takes one from the environment
only when Vault has none, and otherwise generates it; that rule is run here against a stand-in for the vault CLI.

The gateway's metrics take no key (the owner's choice, docs/operations.md), so they are scraped with none and its
host port stays loopback only; its spend record keeps no prompt or response text.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

INFRA = Path(__file__).resolve().parent.parent
APP = INFRA.parent / "nas-feedops-sentinel"
SECRETS = ("ANTHROPIC_API_KEY", "LITELLM_MASTER_KEY", "LITELLM_DB_PASSWORD")
# The services that start the gateway or read its secrets.
GATEWAY_SERVICES = ("litellm", "litellm-bootstrap", "postgres-scram", "litellm-db-init")

needs_app = pytest.mark.skipif(not (APP / "monitoring" / "prometheus.yml").is_file(),
                               reason="needs the application stack beside instraclustr/")


def _compose() -> dict:
    return yaml.safe_load((INFRA / "docker-compose.yml").read_text(encoding="utf-8"))


def _config() -> dict:
    return yaml.safe_load((INFRA / "litellm-config.yaml").read_text(encoding="utf-8"))


# ── no secret through compose or .env ───────────────────────────────────────────

@pytest.mark.parametrize("service", GATEWAY_SERVICES)
def test_no_gateway_secret_is_passed_to_the_service_through_its_environment(service):
    environment = _compose()["services"][service].get("environment") or {}
    names = environment if isinstance(environment, dict) else [str(item).split("=", 1)[0] for item in environment]
    assert not set(SECRETS) & set(names), f"{service} gets {sorted(set(SECRETS) & set(names))} from compose"


def test_compose_requires_none_of_the_gateway_secrets():
    text = (INFRA / "docker-compose.yml").read_text(encoding="utf-8")
    for key in SECRETS:
        assert not re.search(rf"\$\{{{key}:\?", text), f"{key} is still required in .env"


def test_the_example_env_sets_no_gateway_secret():
    text = (INFRA / ".env.example").read_text(encoding="utf-8")
    for key in SECRETS:
        assert not re.search(rf"^\s*{key}\s*=\s*\S", text, re.M), f".env.example sets {key}"
    assert re.search(r"^LITELLM_APP_BUDGET_USD=\d", text, re.M) and re.search(r"^LITELLM_APP_RPM=\d", text, re.M)


# ── Vault: what the gateway's identity renders and may read ─────────────────────

def test_the_gateway_vault_agent_renders_secret_litellm_and_nothing_else():
    hcl = (INFRA / "vault-agent" / "litellm.hcl").read_text(encoding="utf-8")
    assert re.findall(r'secret "([^"]+)"', hcl) == ["secret/data/litellm"]
    assert re.findall(r"\.Data\.data\.(\w+)", hcl) == list(SECRETS)
    assert 'destination          = "/vault/secrets/litellm.env"' in hcl
    assert 'perms                = "0400"' in hcl
    assert "error_on_missing_key = true" in hcl


def test_the_gateway_policy_is_read_only_on_secret_litellm():
    policy = (INFRA / "vault-policies" / "litellm.hcl").read_text(encoding="utf-8")
    rules = re.findall(r'^path\s+"([^"]+)"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]\s*\}', policy, re.M)
    assert rules == [("secret/data/litellm", '"read"')], rules
    assert policy.count("path ") == 1, "one path, nothing else"


def test_terraform_registers_the_gateway_policy_from_its_file():
    text = (INFRA / "terraform" / "main.tf").read_text(encoding="utf-8")
    assert re.search(r'resource "vault_policy" "litellm" \{\s*name\s*=\s*"litellm"\s*'
                     r'policy\s*=\s*file\("\$\{path\.module\}/\.\./vault-policies/litellm\.hcl"\)', text)


def test_the_bootstrap_job_reads_the_master_key_from_vault_with_its_existing_identity():
    script = (INFRA / "litellm-bootstrap.sh").read_text(encoding="utf-8")
    assert "vault kv get -field=LITELLM_MASTER_KEY secret/litellm" in script
    assert '"${LITELLM_MASTER_KEY:?}"' not in script
    # The vault-bootstrap policy already reads secret/data/*: no new grant.
    assert 'path "secret/data/*"' in (INFRA / "vault-policies" / "vault-bootstrap.hcl").read_text(encoding="utf-8")


@pytest.mark.parametrize("script, secrets", [
    # Only the deployment smoke test, which has no Vault, points postgres-scram at a throwaway file.
    ("postgres-scram.sh", 'SECRETS="${LITELLM_SECRETS_FILE:-/vault/secrets/litellm.env}"'),
    ("litellm-db-init.sh", "SECRETS=/vault/secrets/litellm.env"),
])
def test_the_database_jobs_read_the_password_from_the_rendered_file(script, secrets):
    text = (INFRA / script).read_text(encoding="utf-8")
    assert secrets in text
    assert "sed -n 's/^LITELLM_DB_PASSWORD=//p' \"$SECRETS\"" in text
    assert '"${LITELLM_DB_PASSWORD:?}"' not in text


# ── the seed's per-key rule, against a stand-in for the vault CLI ───────────────

# kv get -field=<key> secret/litellm and kv put secret/litellm k=v..., with the messages Vault 1.15 gives for a
# missing secret and a missing field. VAULT_STUB_DENY makes every read fail the way a denied request does.
VAULT_STUB = r"""#!/bin/sh
store="$VAULT_STUB_DIR/secret-litellm"
case "$1 $2" in
  "kv get")
    [ -n "${VAULT_STUB_DENY:-}" ] && { echo "Error reading secret/data/litellm: Code: 403. * permission denied" >&2; exit 2; }
    field="${3#-field=}"
    [ -f "$store" ] || { echo "No value found at secret/data/litellm" >&2; exit 2; }
    line=$(grep "^$field=" "$store") || { echo "Field \"$field\" not present in secret" >&2; exit 1; }
    printf '%s\n' "${line#*=}" ;;
  "kv put")
    shift 3
    : > "$store"
    for pair in "$@"; do printf '%s\n' "$pair" >> "$store"; done ;;
  *) echo "unexpected: vault $*" >&2; exit 64 ;;
esac
"""


@pytest.fixture
def vault(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "vault"
    stub.write_text(VAULT_STUB, encoding="utf-8", newline="\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)

    def stored() -> dict[str, str]:
        path = tmp_path / "secret-litellm"
        if not path.exists():
            return {}
        return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines())

    def preset(values: dict[str, str]) -> None:
        (tmp_path / "secret-litellm").write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")

    def seed(env: dict[str, str] | None = None, deny: bool = False) -> subprocess.CompletedProcess:
        environment = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "VAULT_STUB_DIR": str(tmp_path),
                       "VAULT_ADDR": "https://vault.invalid:8200", "VAULT_BOOTSTRAP_SOURCED": "1", **(env or {})}
        if deny:
            environment["VAULT_STUB_DENY"] = "1"
        return subprocess.run(["sh", "-c", '. "$0" && seed_litellm', str(INFRA / "vault-unseal-and-seed.bash")],
                              env=environment, capture_output=True, text=True, timeout=30)

    return type("Vault", (), {"stored": staticmethod(stored), "preset": staticmethod(preset),
                              "seed": staticmethod(seed)})


def test_an_empty_vault_takes_the_old_env_values_once_the_migration(vault):
    old = {"ANTHROPIC_API_KEY": "sk-ant-old", "LITELLM_MASTER_KEY": "sk-old-master", "LITELLM_DB_PASSWORD": "old-pw"}
    run = vault.seed(old)

    assert run.returncode == 0, run.stderr
    assert vault.stored() == old
    assert run.stderr.count("copied from the environment") == 3


def test_an_empty_vault_and_no_env_generates_the_master_key_and_password_but_never_the_provider_key(vault):
    run = vault.seed()

    assert run.returncode == 0, run.stderr
    stored = vault.stored()
    assert re.fullmatch(r"sk-[0-9a-f]{64}", stored["LITELLM_MASTER_KEY"])
    assert re.fullmatch(r"[0-9a-f]{64}", stored["LITELLM_DB_PASSWORD"])
    assert stored["ANTHROPIC_API_KEY"] == ""
    assert "WARNING: secret/litellm ANTHROPIC_API_KEY: none in Vault or the environment" in run.stderr
    assert vault.seed().returncode == 0 and vault.stored() == stored, "a second start generates nothing new"


def test_a_value_in_vault_is_never_overwritten_from_the_env(vault):
    current = {"ANTHROPIC_API_KEY": "sk-ant-vault", "LITELLM_MASTER_KEY": "sk-vault-master", "LITELLM_DB_PASSWORD": "vault-pw"}
    vault.preset(current)
    run = vault.seed({"ANTHROPIC_API_KEY": "sk-ant-env", "LITELLM_MASTER_KEY": "sk-env", "LITELLM_DB_PASSWORD": "env-pw"})

    assert run.returncode == 0, run.stderr
    assert vault.stored() == current
    assert run.stderr.count("in Vault, kept") == 3


def test_the_live_stacks_first_start_keeps_vaults_two_keys_and_adds_the_password(vault):
    """Before this change the seed wrote the provider and master keys to Vault every start, but not the password."""
    vault.preset({"ANTHROPIC_API_KEY": "sk-ant-vault", "LITELLM_MASTER_KEY": "sk-vault-master"})
    run = vault.seed({"LITELLM_DB_PASSWORD": "old-pw"})

    assert run.returncode == 0, run.stderr
    assert vault.stored() == {"ANTHROPIC_API_KEY": "sk-ant-vault", "LITELLM_MASTER_KEY": "sk-vault-master",
                              "LITELLM_DB_PASSWORD": "old-pw"}


def test_a_vault_that_cannot_be_read_stops_the_seed_and_generates_nothing(vault):
    current = {"ANTHROPIC_API_KEY": "sk-ant-vault", "LITELLM_MASTER_KEY": "sk-vault-master", "LITELLM_DB_PASSWORD": "vault-pw"}
    vault.preset(current)
    run = vault.seed(deny=True)

    assert run.returncode != 0
    assert "permission denied" in run.stderr and "could not read ANTHROPIC_API_KEY" in run.stderr
    assert vault.stored() == current


def test_the_seed_never_prints_a_value(vault):
    old = {"ANTHROPIC_API_KEY": "sk-ant-old", "LITELLM_MASTER_KEY": "sk-old-master", "LITELLM_DB_PASSWORD": "old-pw"}
    run = vault.seed(old)
    assert run.returncode == 0, run.stderr
    for value in old.values():
        assert value not in run.stdout + run.stderr


# ── spend record and metrics ───────────────────────────────────────────────────

def test_the_spend_record_keeps_no_prompt_or_response_text():
    config = _config()
    for section in ("general_settings", "litellm_settings"):
        assert not (config.get(section) or {}).get("store_prompts_in_spend_logs"), section


def test_the_gateway_serves_prometheus_metrics_without_a_key_by_the_owners_choice():
    settings = _config()["litellm_settings"]
    assert "prometheus" in settings["callbacks"]
    # Decided by the owner (docs/operations.md, "The gateway's spend record"). Turning it back on needs a scrape key.
    assert settings["require_auth_for_metrics_endpoint"] is False


def test_the_gateways_host_port_is_loopback_only_because_its_metrics_take_no_key():
    assert _compose()["services"]["litellm"]["ports"] == ["127.0.0.1:4000:4000"]


@needs_app
def test_prometheus_scrapes_the_gateway_by_its_container_name_with_no_key():
    jobs = {j["job_name"]: j for j in yaml.safe_load(
        (APP / "monitoring" / "prometheus.yml").read_text(encoding="utf-8"))["scrape_configs"]}
    litellm = _compose()["services"]["litellm"]
    assert jobs["nas-litellm"]["static_configs"][0]["targets"] == [f"{litellm['container_name']}:4000"]
    assert litellm["command"][-2:] == ["--port", "4000"]
    text = (APP / "monitoring" / "prometheus.yml").read_text(encoding="utf-8")
    assert "sk-" not in text and "authorization" not in text and "bearer" not in text.lower()
