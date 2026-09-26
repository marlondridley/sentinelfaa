"""
The identity service is HTTPS only.

Keycloak terminates TLS with a certificate from the local authority
(src/devcert.py); the dashboard verifies it against that authority and
refuses any identity URL that is not HTTPS, because the in-app sign-in sends
the operator's password. These tests cover the certificate, a real TLS
handshake on loopback verified the way the client verifies Keycloak, the
refusals, and the realm file fitting Keycloak's columns (it did not, so a
fresh Keycloak could not import it).
"""
from __future__ import annotations

import json
import ssl
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
import yaml
from cryptography import x509

from src import devcert
from src import keycloak_auth as ka

REPO = Path(__file__).resolve().parent.parent


def test_keycloak_gets_its_own_certificate_from_the_local_authority(tmp_path: Path):
    devcert.ensure_certificate(tmp_path)
    ca = x509.load_pem_x509_certificate((tmp_path / devcert.CA_CERT).read_bytes())
    leaf, chained_ca = x509.load_pem_x509_certificates((tmp_path / devcert.KEYCLOAK_DIR / devcert.KEYCLOAK_CERT).read_bytes())

    assert leaf.issuer == ca.subject and chained_ca == ca
    sans = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    # keycloak: the dashboard's server-to-server name; localhost and
    # 127.0.0.1: the admin console on the host and the in-container health check.
    assert set(sans.get_values_for_type(x509.DNSName)) == {"keycloak", "localhost"}
    assert [str(ip) for ip in sans.get_values_for_type(x509.IPAddress)] == ["127.0.0.1"]
    # Keycloak runs as uid 1000 in group 0; the key is group-readable, never world-readable.
    assert stat.S_IMODE((tmp_path / devcert.KEYCLOAK_DIR / devcert.KEYCLOAK_KEY).stat().st_mode) == 0o640
    assert stat.S_IMODE((tmp_path / devcert.CA_KEY).stat().st_mode) == 0o600


def test_an_existing_edge_setup_gains_the_keycloak_certificate_without_a_new_authority(tmp_path: Path):
    devcert.ensure_certificate(tmp_path)
    ca_before = (tmp_path / devcert.CA_CERT).read_bytes()
    (tmp_path / devcert.KEYCLOAK_DIR / devcert.KEYCLOAK_CERT).unlink()  # a tls/ directory from before Keycloak had TLS
    (tmp_path / devcert.KEYCLOAK_DIR / devcert.KEYCLOAK_KEY).unlink()
    assert devcert.ensure_certificate(tmp_path) is True
    assert (tmp_path / devcert.CA_CERT).read_bytes() == ca_before, "the operator's trusted authority is kept"
    assert (tmp_path / devcert.KEYCLOAK_DIR / devcert.KEYCLOAK_CERT).exists()


# -- Every certificate mount is a directory ----------------------------------
# `docker compose up` creates every container before edge-certs has run, and
# Docker creates a missing bind-mount source as an empty directory. Mounted
# file by file, that left directories named keycloak.crt and keycloak.key in
# tls/, and edge-certs then failed on them (IsADirectoryError).

def test_every_tls_mount_in_compose_is_a_directory_devcert_fills():
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    sources = {
        volume.split(":")[0]
        for service in compose["services"].values()
        for volume in service.get("volumes", [])
        if isinstance(volume, str) and volume.startswith("./tls")
    }
    allowed = {"./tls"} | {f"./tls/{name}" for name in devcert.MOUNT_DIRS}
    assert sources and sources <= allowed, f"file mounts under ./tls: {sorted(sources - allowed)}"


def test_certificates_are_issued_into_the_directories_docker_created_first(tmp_path: Path):
    for name in devcert.MOUNT_DIRS:          # what `docker compose up` leaves before edge-certs runs
        (tmp_path / name).mkdir()
    assert devcert.ensure_certificate(tmp_path) is True
    contents = {name: sorted(p.name for p in (tmp_path / name).iterdir()) for name in devcert.MOUNT_DIRS}
    # Each container gets only what it needs; none gets the authority's key.
    assert contents == {
        devcert.EDGE_DIR: sorted([devcert.CERT_NAME, devcert.KEY_NAME]),
        devcert.KEYCLOAK_DIR: sorted([devcert.CA_CERT, devcert.KEYCLOAK_CERT, devcert.KEYCLOAK_KEY]),
        devcert.TRUST_DIR: [devcert.CA_CERT],
    }
    ca = (tmp_path / devcert.CA_CERT).read_bytes()
    assert all((tmp_path / name / devcert.CA_CERT).read_bytes() == ca for name in devcert.CA_PUBLISHED_IN)


def test_the_flat_layout_is_cleared_and_the_trusted_authority_kept(tmp_path: Path):
    devcert.ensure_certificate(tmp_path)
    ca_before = (tmp_path / devcert.CA_CERT).read_bytes()
    # The earlier layout: server files beside the authority, and the empty
    # directories Docker made in place of the Keycloak pair.
    (tmp_path / devcert.CERT_NAME).write_bytes(b"old")
    (tmp_path / devcert.KEY_NAME).write_bytes(b"old")
    (tmp_path / devcert.KEYCLOAK_CERT).mkdir()
    (tmp_path / devcert.KEYCLOAK_KEY).mkdir()
    devcert.ensure_certificate(tmp_path)
    assert (tmp_path / devcert.CA_CERT).read_bytes() == ca_before, "the operator's trusted authority is kept"
    assert not any((tmp_path / name).exists() for name in devcert.FLAT_LAYOUT)


class _Discovery(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps({"issuer": "https://localhost/realms/x"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def keycloak_like_server(tmp_path: Path):
    """An HTTPS server on loopback presenting Keycloak's certificate."""
    devcert.ensure_certificate(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    keycloak_dir = tmp_path / devcert.KEYCLOAK_DIR
    context.load_cert_chain(keycloak_dir / devcert.KEYCLOAK_CERT, keycloak_dir / devcert.KEYCLOAK_KEY)
    server = HTTPServer(("127.0.0.1", 0), _Discovery)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"https://localhost:{server.server_address[1]}/", tmp_path / devcert.CA_CERT
    server.shutdown()
    server.server_close()


def test_the_client_verifies_keycloak_against_the_local_authority(keycloak_like_server):
    url, ca_file = keycloak_like_server
    assert ka._get_json(url, 5.0, str(ca_file)) == {"issuer": "https://localhost/realms/x"}


def test_the_client_refuses_keycloak_under_any_other_authority(keycloak_like_server, tmp_path: Path):
    url, _ = keycloak_like_server
    other = tmp_path / "other"
    devcert.ensure_certificate(other)
    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        ka._get_json(url, 5.0, str(other / devcert.CA_CERT))
    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        ka._get_json(url, 5.0)                                  # system trust store: not the local authority


def test_a_plain_http_identity_url_is_refused_before_anything_is_sent():
    settings = ka.KeycloakSettings(client_id="c", client_secret="s",
                                   metadata_url="http://keycloak:8080/realms/x/.well-known/openid-configuration")
    outcome = ka.password_login(settings, "sentinel", "a-password")
    assert (outcome.ok, outcome.error_code) == (False, "identity_endpoint_not_https")
    assert "https://keycloak:8443" in outcome.remediation


def test_a_discovery_document_advertising_an_http_token_endpoint_is_refused(monkeypatch):
    monkeypatch.setattr(ka, "_get_json", lambda url, timeout_s, ca_file=None: {
        "issuer": "https://localhost:8081/realms/x", "jwks_uri": "https://keycloak:8443/certs",
        "token_endpoint": "http://keycloak:8080/realms/x/protocol/openid-connect/token",
    })
    settings = ka.KeycloakSettings(client_id="c", client_secret="s",
                                   metadata_url="https://keycloak:8443/realms/x/.well-known/openid-configuration")
    with pytest.raises(ka.InsecureIdentityEndpoint, match="token_endpoint"):
        ka.fetch_metadata(settings)


def test_sign_out_goes_to_the_backchannel_host_not_the_browser_facing_one():
    """Keycloak advertises logout on https://localhost:8081, which is not Keycloak inside the dashboard container."""
    assert ka.backchannel_url(
        "https://localhost:8081/realms/nas-sentinel/protocol/openid-connect/logout",
        "https://keycloak:8443/realms/nas-sentinel/protocol/openid-connect/token",
    ) == "https://keycloak:8443/realms/nas-sentinel/protocol/openid-connect/logout"
    assert ka.backchannel_url(None, "https://keycloak:8443/t") is None


def test_the_realm_file_fits_keycloaks_columns():
    """CLIENT.DESCRIPTION and KEYCLOAK_ROLE.DESCRIPTION are VARCHAR(255): longer and the import fails."""
    realm = json.loads((REPO / "keycloak" / "realm-nas-sentinel.json").read_text(encoding="utf-8"))
    for client in realm.get("clients", []):
        assert len(client.get("description") or "") <= 255, client["clientId"]
    for role in realm.get("roles", {}).get("realm", []):
        assert len(role.get("description") or "") <= 255, role["name"]


def test_compose_serves_keycloak_over_https_only():
    import yaml

    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    env = compose["services"]["keycloak"]["environment"]
    assert env["KC_HTTP_ENABLED"] == "false"
    assert env["KC_HOSTNAME"].startswith("https://")
    assert env["KC_HTTPS_CERTIFICATE_FILE"] and env["KC_HTTPS_CERTIFICATE_KEY_FILE"]
    assert compose["services"]["keycloak"]["ports"] == ["127.0.0.1:8081:8443"]
    mounts = " ".join(compose["services"]["keycloak"]["volumes"])
    assert "edge-ca.key" not in mounts, "the authority's key never reaches Keycloak"
