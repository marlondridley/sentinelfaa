"""
Public access (nas-feedops-sentinel/docs/public-access.md): what the
Cloudflare tunnel can reach must stay exactly the dashboard and Keycloak's
sign-in pages. These tests pin the configuration that guarantees it, so an
edit that would widen it fails CI instead of reaching the internet:

  * the tunnel container: opt-in (profile), no port, locked down, pinned,
    verifying the edge with the local authority's certificate only
  * the edge's two tunnel listeners: never published on the host; the
    dashboard one proxies only the dashboard, the sign-in one only the
    nas-sentinel realm's pages and assets -- admin console, master realm,
    account console and everything else answer 404
  * Keycloak: the public sign-in address is a setting, the admin console is
    always loopback
  * guests: no realm role, a temporary password, secrets passed by name only
scripts/smoke/05_public_exposure_probe.sh proves the same on the live stack.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

INFRA = Path(__file__).resolve().parent.parent
APP = INFRA.parent / "nas-feedops-sentinel"
pytestmark = pytest.mark.skipif(not (APP / "docker-compose.yml").is_file(),
                                reason="needs the application stack beside instraclustr/")


def _compose() -> dict:
    return yaml.safe_load((APP / "docker-compose.yml").read_text(encoding="utf-8"))


def _server_blocks() -> dict[str, str]:
    """listen port -> the text of that nginx server block."""
    text = (APP / "docker" / "edge" / "nginx.conf").read_text(encoding="utf-8")
    text = "\n".join(line.split("#", 1)[0] for line in text.splitlines())   # drop comments
    blocks, i = {}, 0
    while (start := text.find("server {", i)) != -1:
        depth, j = 0, text.index("{", start)
        while True:
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            j += 1
            if depth == 0:
                break
        body = text[start:j]
        port = re.search(r"listen\s+(\d+)", body).group(1)
        blocks[port] = body
        i = j
    return blocks


def _locations(block: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in re.finditer(r"location\s+(\S+)\s*\{([^}]*)\}", block)}


# -- The tunnel ----------------------------------------------------------------

def test_the_tunnel_is_opt_in_unpublished_and_locked_down():
    svc = _compose()["services"]["cloudflared"]
    assert svc["profiles"] == ["public"], "only `launch_nas_sentinel.ps1 -Public` starts it"
    assert "ports" not in svc, "outbound only: no port on this machine"
    assert "network_mode" not in svc, "on nas-network, reaching only the edge"
    assert re.fullmatch(r"cloudflare/cloudflared:[\d.]+@sha256:[0-9a-f]{64}", svc["image"]), "pinned by digest"
    assert svc["read_only"] is True
    assert svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]
    assert svc["environment"]["TUNNEL_TOKEN"] == "${CLOUDFLARE_TUNNEL_TOKEN:-}", "the token comes from an env file, never the file"
    assert svc["depends_on"]["edge"]["condition"] == "service_healthy"
    assert svc["healthcheck"]["test"][-1] == "ready"


def test_the_tunnel_verifies_the_edge_with_the_authoritys_certificate_only():
    # Routes use CA Pool /etc/cloudflared/trust/edge-ca.crt, not noTLSVerify;
    # the tunnel gets the public certificate and never the edge's key.
    assert _compose()["services"]["cloudflared"]["volumes"] == ["./tls/trust:/etc/cloudflared/trust:ro"]
    doc = (APP / "docs" / "public-access.md").read_text(encoding="utf-8")
    assert "/etc/cloudflared/trust/edge-ca.crt" in doc
    probe = (INFRA.parent / "scripts" / "smoke" / "05_public_exposure_probe.sh").read_text(encoding="utf-8")
    assert "check_hostname = False" not in probe and 'server_hostname="localhost"' in probe
    assert "noTLSVerify" in probe, "the live probe fails a route that skips verification"


def test_the_tunnel_listeners_are_never_published_on_the_host():
    ports = _compose()["services"]["edge"]["ports"]
    assert all(p.startswith("127.0.0.1:") for p in ports), "every published edge port is loopback"
    assert not any(re.search(r":845[01]$", p) for p in ports), "8450/8451 are for the tunnel only"


# -- The edge's two tunnel listeners --------------------------------------------

def test_the_public_dashboard_listener_serves_only_the_dashboard():
    block = _server_blocks()["8450"]
    locs = _locations(block)
    assert set(locs) == {"/"}
    assert "set $upstream http://dashboard:8501;" in locs["/"]
    # Host without a port: Streamlit compares it with the browser's Origin.
    assert re.search(r"proxy_set_header\s+Host\s+\$host;", locs["/"])
    assert "Strict-Transport-Security" in block


def test_the_public_sign_in_listener_serves_only_the_realm_sign_in_pages():
    block = _server_blocks()["8451"]
    locs = _locations(block)
    assert set(locs) == {"/realms/nas-sentinel/", "/resources/", "/realms/nas-sentinel/account", "/"}
    for path in ("/realms/nas-sentinel/", "/resources/"):
        assert "set $upstream https://keycloak:8443;" in locs[path]
    for path in ("/realms/nas-sentinel/account", "/"):
        assert re.fullmatch(r"\s*return 404;\s*", locs[path]), f"{path} must answer 404"
    assert re.search(r"proxy_ssl_verify\s+on;", block), "TLS to Keycloak is verified"
    assert re.search(r"proxy_ssl_trusted_certificate\s+/etc/nginx/trust/edge-ca\.crt;", block)


def test_no_tunnel_listener_reaches_any_other_service():
    blocks = _server_blocks()
    for port in ("8450", "8451"):
        for other in ("grafana", "prometheus", "temporal", "api:8000", "vault", "spire", "postgres"):
            assert other not in blocks[port], f"listener {port} must not reach {other}"


def test_the_edge_can_verify_keycloak():
    volumes = _compose()["services"]["edge"]["volumes"]
    assert "./tls/trust:/etc/nginx/trust:ro" in volumes


# -- Keycloak -------------------------------------------------------------------

def test_keycloak_public_address_is_a_setting_and_the_admin_console_stays_local():
    kc = _compose()["services"]["keycloak"]
    env = kc["environment"]
    assert env["KC_HOSTNAME"] == "${KEYCLOAK_PUBLIC_URL:-https://localhost:8081}"
    assert env["KC_HOSTNAME_ADMIN"] == "https://localhost:8081"
    assert all(p.startswith("127.0.0.1:") for p in kc["ports"]), "Keycloak's own port is loopback"


# -- Guests ---------------------------------------------------------------------

def test_a_guest_gets_no_role_and_a_temporary_password():
    op = (APP / "scripts" / "keycloak-operator.sh").read_text(encoding="utf-8")
    assert 'if [ "$APP_ROLES" = none ]; then' in op
    assert 'for role in $REALM_ROLES; do' in op and 'kc remove-roles' in op
    assert "--temporary" in op
    guest = (APP / "scripts" / "Add-Guest.ps1").read_text(encoding="utf-8")
    assert "$env:APP_ROLES = 'none'" in guest
    assert "$env:APP_TEMPORARY_PASSWORD = '1'" in guest


def test_guest_and_public_scripts_pass_secrets_by_name_only():
    for name in ("Add-Guest.ps1", "Enable-PublicAccess.ps1"):
        text = (APP / "scripts" / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            if "docker exec" in line and "-e" in line:
                assert not re.search(r"-e\s+[A-Z_]+=", line), f"{name}: a value on the command line: {line.strip()}"


def test_the_public_callback_must_be_https():
    rbac = (APP / "scripts" / "keycloak-rbac.sh").read_text(encoding="utf-8")
    assert "https://*[!/]) ;;" in rbac, "PUBLIC_DASHBOARD_URL must be https://host"
