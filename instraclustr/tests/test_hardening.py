"""
Container hardening that must not quietly regress.

The application services (built from one image) run with no Linux capabilities, no privilege escalation, a
read-only root filesystem, and a process and memory ceiling. The SPIRE agent is the one container that holds
the Docker socket and the host PID namespace, so it also runs with no capabilities and no escalation, and both
SPIRE images are pinned by digest. Each of these was run for real before it was written down here (the
application commands against a throwaway Postgres; the SPIRE attestor in a scratch trust domain), so a test
that fails is a change to be argued for, not a flag that was never known to work.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

INFRA = Path(__file__).resolve().parent.parent
APP = INFRA.parent / "nas-feedops-sentinel"

needs_app = pytest.mark.skipif(not (APP / "docker-compose.yml").is_file(),
                               reason="needs the application stack beside instraclustr/")

# Built from the application image and hardened. edge-certs runs once as root to write the edge and Keycloak
# certificates into ./tls and exits; it is not a long-running service and is named here so a new service has to be
# a deliberate choice.
HARDENED = ("migrate", "temporal-worker", "api", "sentinel", "dashboard")
NOT_HARDENED = {"edge-certs": "one-shot: writes the edge and Keycloak certificates into ./tls as root, then exits"}
# Not built from the application image, but they take the same hardened settings.
HARDENED_OTHER_IMAGES = ("alertmanager",)


def _services(compose: str) -> dict:
    return yaml.safe_load(compose_text(compose))["services"]


def compose_text(which: str) -> str:
    path = INFRA / "docker-compose.yml" if which == "infra" else APP / "docker-compose.yml"
    return path.read_text(encoding="utf-8")


@needs_app
@pytest.mark.parametrize("name", HARDENED)
def test_an_application_service_runs_with_no_capabilities_no_escalation_and_a_read_only_root(name):
    svc = _services("app")[name]

    assert svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]
    assert svc["read_only"] is True
    assert any(t.startswith("/tmp") for t in svc["tmpfs"]), "somewhere to write that is not the image"
    assert 0 < svc["pids_limit"] <= 512
    assert svc.get("mem_limit"), "a memory ceiling"


@needs_app
def test_every_service_built_from_the_application_image_is_hardened_or_named_as_an_exception():
    services = _services("app")
    from_app_image = {n for n, s in services.items() if s.get("image") == "nas-feedops-sentinel:latest"}

    assert from_app_image == set(HARDENED) | set(NOT_HARDENED), (
        "a service built from the application image is neither hardened nor an exception here")


@needs_app
def test_the_hardened_settings_are_one_definition_not_a_copy_per_service():
    text = compose_text("app")
    assert text.count("x-hardened: &hardened") == 1
    assert text.count("*hardened") == len(HARDENED) + len(HARDENED_OTHER_IMAGES)


@needs_app
@pytest.mark.parametrize("name", HARDENED_OTHER_IMAGES)
def test_a_service_from_another_image_that_shares_the_hardened_settings_has_them(name):
    svc = _services("app")[name]

    assert svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]
    assert svc["read_only"] is True
    assert re.search(r"@sha256:[0-9a-f]{64}$", svc["image"]), "pinned by digest, not by a tag"


@pytest.mark.parametrize("name", ["spire-server", "spire-agent"])
def test_the_spire_containers_are_pinned_by_digest_and_drop_every_capability(name):
    svc = _services("infra")[name]

    assert re.fullmatch(r"ghcr\.io/spiffe/spire-(?:server|agent):[\d.]+@sha256:[0-9a-f]{64}", svc["image"]), \
        "a tag can be re-pushed; the digest cannot"
    assert svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]


def test_the_spire_agent_keeps_only_what_the_docker_attestor_needs_and_is_read_only():
    agent = _services("infra")["spire-agent"]

    assert agent["pid"] == "host", "the docker attestor resolves a PID to a container through /proc"
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in agent["volumes"]
    assert agent["read_only"] is True, "it writes only to its data and socket volumes"
    assert "privileged" not in agent and "cap_add" not in agent
    writable = [v for v in agent["volumes"] if not v.endswith(":ro")]
    assert {v.split(":")[1] for v in writable} == {"/opt/spire/data", "/tmp/spire-agent/public"}, \
        "the two volumes a read-only agent has to write to"


def test_the_slack_webhook_is_a_documented_empty_variable_in_the_example_env():
    lines = (INFRA / ".env.example").read_text(encoding="utf-8").splitlines()

    assert "SLACK_WEBHOOK_URL=" in lines, "documented, with no value"
    assert not any(line.startswith(("SLACK_CLIENT_SECRET", "SLACK_SIGNING_SECRET", "SLACK_VERIFICATION_TOKEN"))
                   for line in lines), "a Slack app's other credentials are not used by anything here"


# ── the ACL script ────────────────────────────────────────────────────────────

def test_the_key_material_script_reports_by_default_and_changes_nothing_without_apply():
    script = (INFRA / "scripts" / "Protect-KeyMaterial.ps1").read_text(encoding="utf-8")
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))

    assert "[switch]$Apply" in script
    assert code.count("& icacls") == 1, "one place changes a permission"
    assert code.index("if ($Apply)") < code.index("& icacls"), "and only after the `if ($Apply)` that guards it"
    assert "Get-Content" not in code and "ReadAllText" not in code, "it never reads a file's contents"


def test_the_key_material_script_covers_the_files_the_review_named():
    script = (INFRA / "scripts" / "Protect-KeyMaterial.ps1").read_text(encoding="utf-8")

    for path in ("nas-feedops-sentinel\\tls", "instraclustr\\vault-init-data", "instraclustr\\certs",
                 "instraclustr\\.env", "nas-feedops-sentinel\\.env", "secrets.toml", "instraclustr\\terraform"):
        assert path in script


# ── the browser trust check ───────────────────────────────────────────────────
# Test-BrowserTrust.ps1 runs on the operator's PC and must change nothing there: trusting the offline root is the
# person's own step (docs/runbooks.md, "The browser says Not secure"). PowerShell does not run here, so the script is
# checked by reading it.

BROWSER_TRUST = APP / "scripts" / "Test-BrowserTrust.ps1"

# Comments, here-strings, single-quoted strings ('' is a quote) and double-quoted strings (` escapes, "" is a quote),
# matched from the left so that a quote inside one of them does not start another.
POWERSHELL_TEXT = re.compile(r"""<#.*?#>|#[^\n]*|@'\n.*?\n'@|@"\n.*?\n"@|'(?:[^']|'')*'|"(?:[^"`]|`.|"")*\"""", re.S)
# Writes a certificate store, the registry or a file, or runs text as code.
CHANGES_THE_MACHINE = re.compile(
    r"Import-PfxCertificate|Export-Certificate|certutil|X509Store|Registry|HK(?:LM|CU|EY_)|\breg(?:\.exe)?\s|"
    r"(?:Set|New|Remove|Move|Copy|Rename|Clear)-Item|Set-Content|Add-Content|Out-File|"
    r"Invoke-Expression|\biex\b|Start-Process|ScriptBlock", re.IGNORECASE)
SECRET_PATHS = re.compile(r"\.(?:key|pfx|p12)\b|\.env\b|\btls[\\/]|certs[\\/](?:spire|vault|web)\b", re.IGNORECASE)


def _powershell_code(script: str) -> tuple[str, str]:
    """(code, what runs): the script without comments, and that again with every single-quoted string emptied.

    A single-quoted string is text: unlike a double-quoted string ("$(...)" runs) or a here-string (the C# source the
    script compiles), nothing in it can run.
    """
    def code(match: re.Match) -> str:
        return "" if match.group().startswith("#") or match.group().startswith("<#") else match.group()

    def runs(match: re.Match) -> str:
        return "''" if match.group().startswith("'") else code(match)

    return POWERSHELL_TEXT.sub(code, script), POWERSHELL_TEXT.sub(runs, script)


@needs_app
def test_the_browser_trust_check_changes_nothing_and_names_no_secret():
    script = BROWSER_TRUST.read_text(encoding="utf-8")
    code, runs = _powershell_code(script)

    assert script.isascii(), "Windows PowerShell 5.1 reads a UTF-8 script without a byte order mark as ANSI"
    assert not CHANGES_THE_MACHINE.findall(code), "no command that writes a certificate store, the registry or a file"
    assert "Import-Certificate -FilePath" in code, "it prints the command for the person at the PC"
    assert "import-certificate" not in runs.lower(), "only as text it prints (a single-quoted string), never run"
    assert not SECRET_PATHS.findall(script), "it never names a private key, .env, tls\\ or certs\\{spire,vault,web}"
