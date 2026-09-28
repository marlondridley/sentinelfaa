"""
Workload identity is declared in four places that must agree, and a mismatch
fails silently at runtime -- a service whose sidecar never gets an SVID, or
gets one Vault has no role for, just waits on its Vault Agent forever:

  spire-entries.yaml                 which container is which service (SPIRE)
  terraform/main.tf                  which service may log in as which role (Vault)
  nas-feedops-sentinel compose file  the service, its two sidecars, its label
  nas-feedops-sentinel Dockerfile    the uid the sidecars share files with

Plus what the migration away from AppRole must never regress: no credential
or Vault route in an application container, TLS-only Vault, and SVID files
spiffe-helper can rewrite on rotation.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

INFRA = Path(__file__).resolve().parent.parent
REPO = INFRA.parent
APP = REPO / "nas-feedops-sentinel"
TRUST_DOMAIN = "sentinelfaa.internal"
# Which Vault Agent config and policy each service's class uses.
CLASS = {"migrate": "migrate", "proof": "migrate",
         "api": "app", "dashboard": "app", "sentinel": "app", "temporal-worker": "app"}
POLICY = {"migrate": "sentinel_migrate", "app": "sentinel_app"}

needs_app = pytest.mark.skipif(not (APP / "docker-compose.yml").is_file(),
                               reason="needs the application stack beside instraclustr/")


def _manifest() -> list[dict]:
    return yaml.safe_load((INFRA / "spire-entries.yaml").read_text(encoding="utf-8"))["entries"]


def _terraform_workloads() -> dict[str, str]:
    text = (INFRA / "terraform" / "main.tf").read_text(encoding="utf-8")
    block = re.search(r"workloads = \{(.*?)\n  \}", text, re.S)
    assert block, "terraform/main.tf: locals.workloads not found"
    return dict(re.findall(r'"([a-z-]+)"\s*=\s*vault_policy\.(\w+)\.name', block.group(1)))


def _app_compose() -> dict:
    return yaml.safe_load((APP / "docker-compose.yml").read_text(encoding="utf-8"))


def _labelled(compose: dict) -> dict[str, str]:
    """identity -> the compose service that carries it."""
    return {svc["labels"]["com.sentinelfaa.identity"]: name
            for name, svc in compose["services"].items()
            if "com.sentinelfaa.identity" in (svc.get("labels") or {})}


# -- SPIRE and Vault agree --------------------------------------------------

def test_every_spire_identity_is_one_service_with_a_matching_vault_role():
    names = [e["spiffe_id"].rsplit("/", 1)[1] for e in _manifest()]
    assert sorted(names) == sorted(CLASS), "spire-entries.yaml and the service list differ"
    assert set(_terraform_workloads()) == set(names), "terraform workloads and spire-entries.yaml differ"


def test_each_entry_is_its_own_label_id_and_invalid_tld_cn():
    for entry in _manifest():
        name = entry["spiffe_id"].rsplit("/", 1)[1]
        assert entry["spiffe_id"] == f"spiffe://{TRUST_DOMAIN}/workload/{name}"
        assert entry["selectors"] == [f"docker:label:com.sentinelfaa.identity:{name}"]
        # Vault names the identity alias after the CN; SPIRE sets the CN from
        # the first DNS name; the upstream CA permits DNS names under .invalid only.
        assert entry["dns_names"] == [f"{name}.sentinelfaa.invalid"]


def test_vault_roles_bind_the_exact_spiffe_id_and_cn():
    text = (INFRA / "terraform" / "main.tf").read_text(encoding="utf-8")
    assert 'allowed_uri_sans = ["spiffe://${var.trust_domain}/workload/${each.key}"]' in text
    assert 'allowed_common_names = ["${each.key}.sentinelfaa.invalid"]' in text
    assert re.search(r'certificate\s*=\s*file\(var\.root_ca_file\)', text), "roles trust the offline root"


def test_each_service_gets_its_class_policy():
    for name, policy in _terraform_workloads().items():
        assert policy == POLICY[CLASS[name]], name


# -- The application stack agrees -------------------------------------------

@needs_app
def test_every_identity_has_its_service_and_both_sidecars():
    compose = _app_compose()
    services = compose["services"]
    assert _labelled(compose) == {name: f"{name}-spiffe" for name in CLASS}
    for name, cls in CLASS.items():
        helper, agent, app = services[f"{name}-spiffe"], services[f"{name}-vault-agent"], services[name]
        assert f"{name}-svid:/run/spiffe" in helper["volumes"]
        assert "nas-spire-agent-socket:/tmp/spire-agent/public:ro" in helper["volumes"]
        assert agent["entrypoint"] == ["vault", "agent", f"-config=/vault/agent/{cls}.hcl"]
        assert f"{name}-svid:/run/spiffe:ro" in agent["volumes"]
        assert f"{name}-secrets:/vault/secrets" in agent["volumes"]
        assert "../instraclustr/certs/trust:/certs-trust:ro" in agent["volumes"]
        # The service reads only its own rendered file, and waits for it.
        assert f"{name}-secrets:/vault/secrets:ro" in app["volumes"]
        assert app["depends_on"][f"{name}-vault-agent"]["condition"] == "service_healthy"
        assert app["environment"]["VAULT_AGENT_ENV_FILE"] == "/vault/secrets/app-config.env"
        # Profiles follow the service (proof is on demand).
        assert helper.get("profiles") == agent.get("profiles") == app.get("profiles")


@needs_app
def test_secret_volumes_are_memory_only_and_owned_by_the_app_uid():
    compose = _app_compose()
    dockerfile = (APP / "docker" / "Dockerfile").read_text(encoding="utf-8")
    uid = re.search(r"useradd -r -u (\d+)", dockerfile)
    gid = re.search(r"groupadd -r -g (\d+)", dockerfile)
    assert uid and gid, "the app image's uid/gid must be pinned"
    for name in CLASS:
        for kind in ("svid", "secrets"):
            opts = compose["volumes"][f"{name}-{kind}"]["driver_opts"]
            assert opts["type"] == "tmpfs", f"{name}-{kind} must never touch disk"
            assert f"uid={uid.group(1)}" in opts["o"] and f"gid={gid.group(1)}" in opts["o"]
            assert "mode=0700" in opts["o"]
        for sidecar in ("spiffe", "vault-agent"):
            assert compose["services"][f"{name}-{sidecar}"]["user"] == f"{uid.group(1)}:{gid.group(1)}"


@needs_app
def test_no_application_container_holds_a_credential_or_a_vault_route():
    compose = _app_compose()
    app_services = [n for n, s in compose["services"].items()
                    if s.get("image") in ("nas-feedops-sentinel:latest", "nas-feedops-sentinel:test")
                    and n != "edge-certs"]
    assert set(CLASS) <= set(app_services)
    forbidden_env = {"POSTGRES_PASSWORD", "APP_DB_PASSWORD", "LITELLM_API_KEY", "VAULT_TOKEN",
                     "VAULT_ADDR", "VAULT_APPROLE_DIR", "VAULT_ROLE_ID", "VAULT_SECRET_ID"}
    for name in app_services:
        svc = compose["services"][name]
        assert not forbidden_env & set(svc.get("environment") or {}), name
        assert not any("approle" in v or "vault-init-data" in v for v in svc.get("volumes") or []), name


@needs_app
def test_env_example_holds_no_database_credential():
    text = (APP / ".env.example").read_text(encoding="utf-8")
    for key in ("POSTGRES_PASSWORD", "APP_DB_PASSWORD", "VAULT_TOKEN", "VAULT_ADDR"):
        assert not re.search(rf"^{key}=", text, re.M), key


# -- Transport and rotation ------------------------------------------------

def test_vault_is_tls_only_and_every_infra_client_verifies_the_root():
    config = (INFRA / "vault-config.hcl").read_text(encoding="utf-8")
    assert "tls_disable" not in config.replace("tls_disable_client_certs", "")
    assert 'tls_cert_file' in config and 'tls_min_version                    = "tls12"' in config
    compose = yaml.safe_load((INFRA / "docker-compose.yml").read_text(encoding="utf-8"))
    for name, svc in compose["services"].items():
        env = svc.get("environment") or {}
        if isinstance(env, dict) and "VAULT_ADDR" in env:
            assert env["VAULT_ADDR"].startswith("https://"), name
            assert env.get("VAULT_CACERT", "").endswith("root-ca.crt"), name


def test_vault_agents_verify_vault_and_reload_rotated_svids():
    for cls in ("app", "migrate"):
        hcl = (INFRA / "vault-agent" / f"{cls}.hcl").read_text(encoding="utf-8")
        assert 'address = "https://vault:8200"' in hcl
        assert hcl.count('ca_cert     = "/certs-trust/root-ca.crt"') + hcl.count('ca_cert = "/certs-trust/root-ca.crt"') == 2
        assert "reload      = true" in hcl or "reload = true" in hcl
        assert 'method "cert"' in hcl and "sink" not in hcl.split("template")[0].split("# No sink")[0], \
            "the Vault token stays in the agent's memory: no sink"


def test_spiffe_helper_can_rewrite_the_svid_on_rotation():
    """Read-only modes made the second write fail: the SVID then expired an hour later."""
    conf = (INFRA / "vault-agent" / "spiffe-helper.conf").read_text(encoding="utf-8")
    cert = re.search(r"^cert_file_mode = 0([0-7]{3})$", conf, re.M)
    key = re.search(r"^key_file_mode = 0([0-7]{3})$", conf, re.M)
    assert cert and key
    assert int(cert.group(1)[0]) & 0o2 and int(key.group(1)[0]) & 0o2, "owner must be able to write"
    assert key.group(1)[1:] == "00", "the key is owner-only"


def test_spire_agent_needs_no_shell_and_trusts_the_root_from_first_boot():
    compose = yaml.safe_load((INFRA / "docker-compose.yml").read_text(encoding="utf-8"))
    agent = compose["services"]["spire-agent"]
    assert "entrypoint" not in agent, "the spire-agent image has no shell"
    assert agent["command"][-2:] == ["-joinToken", "${SPIRE_JOIN_TOKEN:-}"]
    conf = (INFRA / "spire" / "agent.conf").read_text(encoding="utf-8")
    assert "insecure_bootstrap" not in conf.split("plugins")[0].replace("no insecure_bootstrap", "")
    assert 'trust_bundle_path = "/opt/spire/conf/trust/root-ca.crt"' in conf
