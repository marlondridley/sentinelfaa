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
  * guests: no realm role unless one is named (then exactly that one), a
    temporary password, secrets passed by name only; every realm update
    keeps the public sign-in callback
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
    assert not any(re.search(r":845[0-2]$", p) for p in ports), "8450-8452 are for the tunnel only"


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


def test_the_public_listeners_ask_not_to_be_indexed_and_allow_no_devices():
    blocks = _server_blocks()
    for port in ("8450", "8451"):
        assert re.search(r'add_header\s+X-Robots-Tag\s+"noindex, nofollow"\s+always;', blocks[port]), port
        assert re.search(r'add_header\s+Permissions-Policy\s+"camera=\(\), microphone=\(\), geolocation=\(\)', blocks[port]), port
    # Keycloak sends its own HSTS and X-Robots-Tag: one of each reaches the browser, ours.
    for header in ("Strict-Transport-Security", "X-Robots-Tag"):
        assert re.search(rf"proxy_hide_header\s+{header};", blocks["8451"]), header


def test_the_story_site_listener_serves_static_files_only():
    block = _server_blocks()["8452"]
    assert "proxy_pass" not in block, "the site listener reaches no service"
    assert re.search(r"root\s+/usr/share/nginx/site;", block)
    assert set(_locations(block)) == {"/"}
    assert re.search(r"limit_except GET \{ deny all; \}", block), "GET and HEAD only"
    assert re.search(r"include\s+/etc/nginx/mime.types;", block), "nosniff needs real content types"
    assert re.search(r"if \(\$host ~\* \^www\\\.\(\.\+\)\$\) \{ return 301 https://\$1\$request_uri; \}", block)
    csp = re.search(r'add_header Content-Security-Policy "([^"]+)" always;', block).group(1)
    for directive in ("default-src 'none'", "script-src 'self'", "style-src 'self'", "img-src 'self'",
                      "frame-ancestors 'none'", "base-uri 'none'", "form-action 'none'", "object-src 'none'"):
        assert directive in csp, directive
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "./site:/usr/share/nginx/site:ro" in _compose()["services"]["edge"]["volumes"]


SITE = APP / "site"


def test_the_story_site_loads_nothing_from_elsewhere_and_runs_under_its_policy():
    html = (SITE / "index.html").read_text(encoding="utf-8")
    loads = re.findall(r'(?:src|href)="([^"#]+)"', html)
    external = [u for u in loads if re.match(r"(?i)https?:|//", u)]
    assert external and all(u == "https://sentinel.agenticreliabilityengineering.com/" for u in external), \
        f"the only off-site link is Sign in: {external}"
    assert not re.search(r"<(?:script|link)[^>]+(?:src|href)=\"(?:https?:)?//", html), "no third-party script or style"
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "no inline script: the policy would block it"
    assert not re.search(r"\sstyle=\"", html), "no style attributes: the policy would block them"
    assert not re.search(r"\son[a-z]+=\"", html), "no inline event handlers"
    css = (SITE / "assets" / "styles.css").read_text(encoding="utf-8")
    assert not re.search(r"@import|url\(\s*[\"']?(?:https?:)?//", css), "no fonts or images from elsewhere"


def test_the_story_site_promises_only_what_the_code_does():
    """The Layer 4 of the architecture deck once promised self-healing; the code never executes on its own."""
    text = re.sub(r"<[^>]+>", " ", (SITE / "index.html").read_text(encoding="utf-8")).lower()
    for claim in ("auto-execut", "autoexecut", "self-heal", "worker pod", "reconnect dropped", "retry quarantined",
                  "cryptographic audit chain"):
        assert claim not in text, f"the site must not claim {claim!r}"
    assert "nothing executes on its own" in text
    assert "not affiliated with or endorsed by the federal aviation administration" in text


def test_no_tunnel_listener_reaches_any_other_service():
    blocks = _server_blocks()
    for port in ("8450", "8451", "8452"):
        for other in ("grafana", "prometheus", "temporal", "api:8000", "vault", "spire", "postgres"):
            assert other not in blocks[port], f"listener {port} must not reach {other}"


def test_the_edge_can_verify_keycloak():
    volumes = _compose()["services"]["edge"]["volumes"]
    assert "./tls/trust:/etc/nginx/trust:ro" in volumes


# -- Keycloak -------------------------------------------------------------------

def test_keycloak_public_address_is_a_setting_and_the_admin_console_stays_local():
    kc = _compose()["services"]["keycloak"]
    env = kc["environment"]
    assert env["KC_HOSTNAME"] == "${KEYCLOAK_BROWSER_URL:-https://localhost:8081}"
    assert env["KC_HOSTNAME_ADMIN"] == "https://localhost:8081"
    assert all(p.startswith("127.0.0.1:") for p in kc["ports"]), "Keycloak's own port is loopback"


# -- The switch: public on with -Public, off without it ----------------------------

def _set_public() -> str:
    return (APP / "scripts" / "Set-PublicAccess.ps1").read_text(encoding="utf-8")


def test_the_launcher_switches_public_access_on_every_launch():
    launcher = (INFRA.parent / "launch_nas_sentinel.ps1").read_text(encoding="utf-8")
    assert "$publicMode = if ($Public) { 'On' } else { 'Off' }" in launcher
    assert "'scripts\\Set-PublicAccess.ps1') -Mode $publicMode" in launcher
    assert "cloudflared" not in launcher.split("# 4b.")[1].split("# 5.")[0], \
        "the tunnel is started and stopped by Set-PublicAccess.ps1 only"


def test_off_restores_local_sign_in_and_stops_the_tunnel():
    text = _set_public()
    assert "$localSignIn   = 'https://localhost:8081'" in text
    assert "$localCallback = 'https://localhost:8443/oauth2callback'" in text
    off = text.split("} else {\n    $signIn   = $localSignIn")[0]
    assert "if ($pair[1] -notmatch '^https://[a-z0-9.-]+[a-z0-9]$')" in off, "On accepts https://host only"
    assert "--profile public stop cloudflared" in text


def test_one_name_for_the_sign_in_address_everywhere():
    # compose reads it, Set-PublicAccess writes it, probe 05 checks Keycloak
    # against it: a rename in one place alone breaks sign-in.
    assert "${KEYCLOAK_BROWSER_URL:-" in _compose()["services"]["keycloak"]["environment"]["KC_HOSTNAME"]
    assert "$browserKey    = 'KEYCLOAK_BROWSER_URL'" in _set_public()
    probe = (INFRA.parent / "scripts" / "smoke" / "05_public_exposure_probe.sh").read_text(encoding="utf-8")
    assert "BROWSER_URL=$(envval KEYCLOAK_BROWSER_URL)" in probe


# -- Guests ---------------------------------------------------------------------

def test_a_guest_gets_no_role_and_a_temporary_password():
    op = (APP / "scripts" / "keycloak-operator.sh").read_text(encoding="utf-8")
    assert 'if [ "$APP_ROLES" = none ]; then' in op
    assert 'for role in $REALM_ROLES; do' in op and 'kc remove-roles' in op
    assert "--temporary" in op
    guest = (APP / "scripts" / "Add-Guest.ps1").read_text(encoding="utf-8")
    # Read-only unless a role is asked for by name, from the realm's own roles.
    assert "[ValidateSet('none', 'auditor', 'watch_officer', 'supervisor')][string]$Role = 'none'" in guest
    assert "$env:APP_ROLES = $Role" in guest
    assert "$env:APP_TEMPORARY_PASSWORD = '1'" in guest


def test_a_role_change_replaces_the_old_role():
    op = (APP / "scripts" / "keycloak-operator.sh").read_text(encoding="utf-8")
    assert 'if [ "${APP_ROLES_EXACT:-0}" = 1 ]; then' in op
    guest = (APP / "scripts" / "Add-Guest.ps1").read_text(encoding="utf-8")
    assert "$env:APP_ROLES_EXACT = '1'" in guest and "-e APP_ROLES_EXACT" in guest


def test_every_realm_update_keeps_the_public_callback():
    # keycloak-rbac.sh rewrites the dashboard client's callbacks on every run;
    # a caller that leaves out PUBLIC_DASHBOARD_URL drops the public one, and
    # sign-in through the public address fails.
    for name in ("Setup-Keycloak.ps1", "Enable-PublicAccess.ps1", "Add-Guest.ps1"):
        runs = [line for line in (APP / "scripts" / name).read_text(encoding="utf-8").splitlines()
                if "docker exec" in line and "bash /tmp/keycloak-rbac.sh" in line]
        assert runs, f"{name}: does not run keycloak-rbac.sh"
        for line in runs:
            assert "-e PUBLIC_DASHBOARD_URL" in line, f"{name}: {line.strip()}"


def test_logins_expire_unless_renewed():
    guest = (APP / "scripts" / "Add-Guest.ps1").read_text(encoding="utf-8")
    assert "[ValidateRange(1, 365)][int]$Days = 30" in guest, "every login from this script has a last day"
    assert "$env:APP_EXPIRES_ON = $expiresOn" in guest and "-e APP_EXPIRES_ON" in guest
    launcher = (INFRA.parent / "launch_nas_sentinel.ps1").read_text(encoding="utf-8")
    assert "'scripts\\Disable-ExpiredLogins.ps1')" in launcher, "every launch turns expired logins off"
    sweep = (APP / "scripts" / "Disable-ExpiredLogins.ps1").read_text(encoding="utf-8")
    assert "if (-not $user.enabled -or -not $ends) { continue }" in sweep, "logins without an end date are never touched"
    assert "if ($ends -match '^\\d{4}-\\d{2}-\\d{2}$' -and $ends -ge $today) { continue }" in sweep, \
        "still in date: kept; not a date: turned off"
    assert "$env:APP_DISABLE = '1'" in sweep, "the same path as -Disable: off, and sessions ended"


def test_the_keycloak_query_helper_only_reads():
    helper = (APP / "scripts" / "keycloak-get.sh").read_text(encoding="utf-8")
    assert "events|admin-events|users|users/profile) ;;" in helper, "a fixed list of read-only queries"
    calls = re.findall(r"^\s*kc (\w+)", helper, flags=re.M)
    assert calls == ["config", "config", "get"], "configures kcadm, then one get; never create, update or delete"


def test_guest_and_public_scripts_pass_secrets_by_name_only():
    for name in ("Add-Guest.ps1", "Enable-PublicAccess.ps1", "Get-SignIns.ps1", "Disable-ExpiredLogins.ps1"):
        text = (APP / "scripts" / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            if "docker exec" in line and "-e" in line:
                assert not re.search(r"-e\s+[A-Z_]+=", line), f"{name}: a value on the command line: {line.strip()}"


def test_turning_public_access_on_restarts_nothing_else():
    # Starting the tunnel (or restarting keycloak/edge/dashboard for it) must
    # not bring back the migrate job's sidecars: they hold the schema owner's
    # credentials and run only during a migration.
    for name in ("Set-PublicAccess.ps1", "Enable-PublicAccess.ps1"):
        text = (APP / "scripts" / name).read_text(encoding="utf-8")
        ups = [line for line in text.splitlines() if re.search(r"docker compose .* up -d", line)]
        assert ups, f"{name}: no compose up"
        for line in ups:
            assert "--no-deps" in line, f"{name}: {line.strip()}"


def test_the_public_callback_must_be_https():
    rbac = (APP / "scripts" / "keycloak-rbac.sh").read_text(encoding="utf-8")
    assert "https://*[!/]) ;;" in rbac, "PUBLIC_DASHBOARD_URL must be https://host"
