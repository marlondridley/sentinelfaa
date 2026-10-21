"""
The edge and the identity service are HTTPS only, with certificates from the
web TLS branch of the offline root.

src/web_tls.py (the edge-certs job) signs the edge's and Keycloak's
certificates with the web TLS CA from the root ceremony
(instraclustr/certs/web), and every client trusts the root alone
(instraclustr/certs/trust/root-ca.crt). The dashboard's OIDC client verifies
Keycloak against that root, and identity URLs must be HTTPS because the
realm's sign-in carries the operator's password.

These tests run the issuer against a throwaway root and web TLS CA made here,
never the real root, and cover: what it writes and where, renewal, every
refusal (nothing is issued without a sound web TLS CA), a real TLS handshake
on loopback verified the way the OIDC client verifies Keycloak, the compose
wiring, and the realm file fitting Keycloak's columns (it did not, so a fresh
Keycloak could not import it). The name constraints themselves are proven
with handshakes against the real ceremony in instraclustr/tests/test_pki.py.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import json
import re
import ssl
import stat
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from src import web_tls

REPO = Path(__file__).resolve().parent.parent
LEAF_FILES = ["edge/edge.crt", "edge/edge.key", "keycloak/keycloak.crt", "keycloak/keycloak.key"]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _usage(**on: bool) -> x509.KeyUsage:
    flags = dict.fromkeys(("digital_signature", "content_commitment", "key_encipherment", "data_encipherment",
                           "key_agreement", "key_cert_sign", "crl_sign"), False)
    return x509.KeyUsage(**{**flags, **on}, encipher_only=False, decipher_only=False)


PERMITTED = [x509.DNSName("localhost"), x509.DNSName("keycloak"),
             x509.IPAddress(ipaddress.ip_network("127.0.0.1/32")), x509.UniformResourceIdentifier("invalid")]
EXCLUDED = [x509.IPAddress(ipaddress.ip_network("::/0"))]
ROOT_POLICY = (
    (x509.BasicConstraints(ca=True, path_length=2), True),
    (_usage(key_cert_sign=True, crl_sign=True, digital_signature=True), True),
)
# instraclustr/certs/policy/signing.cnf, web_tls_ext: (extension, critical).
WEB_POLICY = (
    (x509.BasicConstraints(ca=True, path_length=0), True),
    (_usage(key_cert_sign=True, crl_sign=True), True),
    (x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False),
    (x509.NameConstraints(permitted_subtrees=PERMITTED, excluded_subtrees=EXCLUDED), True),
)


def _policy(**replace: x509.ExtensionType | None) -> tuple:
    """WEB_POLICY with an extension, named by its class, replaced by another value or (None) left out."""
    out = []
    for ext, critical in WEB_POLICY:
        value = replace.get(type(ext).__name__, ext)
        if value is not None:
            out.append((value, critical))
    return tuple(out)


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _spki(cert: x509.Certificate) -> bytes:
    return cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def _ca(name: str, issuer: tuple[x509.Certificate, ec.EllipticCurvePrivateKey] | None, extensions, *,
        key: ec.EllipticCurvePrivateKey | None = None, not_before: dt.datetime | None = None,
        days: int = 365) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    """A CA certificate and its key: self-signed when `issuer` is None."""
    key = key or ec.generate_private_key(ec.SECP384R1())
    subject = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "sentinelfaa"),
                         x509.NameAttribute(NameOID.COMMON_NAME, name)])
    issuer_name, issuer_key = (issuer[0].subject, issuer[1]) if issuer else (subject, key)
    start = not_before or _now() - dt.timedelta(minutes=5)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(issuer_name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(start).not_valid_after(start + dt.timedelta(days=days))
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False)
    )
    for ext, critical in extensions:
        builder = builder.add_extension(ext, critical)
    return builder.sign(issuer_key, hashes.SHA384()), key


@dataclass
class Pki:
    """A throwaway root and web TLS CA, laid out as edge-certs sees them."""
    root: tuple[x509.Certificate, ec.EllipticCurvePrivateKey]
    web: Path      # <web-ca dir>: web-ca.crt, web-ca.key
    trust: Path    # <trust dir>: root-ca.crt
    tls: Path      # <tls dir>: what the issuer writes

    def install_web_ca(self, extensions=WEB_POLICY, *, signed_by=None, **kw) -> x509.Certificate:
        """A new web TLS CA (a new key, the policy's name), as `pki.ps1 install` leaves it."""
        cert, key = _ca("sentinelfaa Web TLS CA", signed_by or self.root, extensions, **kw)
        self.web.mkdir(parents=True, exist_ok=True)
        (self.web / "web-ca.crt").write_bytes(_pem(cert))
        (self.web / "web-ca.key").write_bytes(_key_pem(key))
        return cert

    @property
    def web_ca(self) -> x509.Certificate:
        return x509.load_pem_x509_certificate((self.web / "web-ca.crt").read_bytes())

    @property
    def web_key(self):
        return serialization.load_pem_private_key((self.web / "web-ca.key").read_bytes(), password=None)

    def args(self) -> list[str]:
        return [str(self.web), str(self.trust), str(self.tls)]

    def issue(self) -> bool:
        return web_tls.ensure_certificates(self.web, self.trust, self.tls)


@pytest.fixture
def pki(tmp_path: Path) -> Pki:
    root = _ca("sentinelfaa Root CA (throwaway)", None, ROOT_POLICY, days=3650)
    p = Pki(root, tmp_path / "web", tmp_path / "trust", tmp_path / "tls")
    p.trust.mkdir()
    (p.trust / "root-ca.crt").write_bytes(_pem(root[0]))
    p.install_web_ca()
    return p


def _chain(path: Path) -> list[x509.Certificate]:
    return x509.load_pem_x509_certificates(path.read_bytes())


def _snapshot(directory: Path) -> dict[str, tuple[bytes, int] | None]:
    """Every path under `directory`: a file's bytes and mode, None for a directory."""
    return {
        p.relative_to(directory).as_posix(): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode)) if p.is_file() else None
        for p in sorted(directory.rglob("*"))
    }


def _resign(chain_file: Path, ca: x509.Certificate, ca_key, days_left: int) -> None:
    """Re-sign the leaf in `chain_file` (same key, names and extensions) by `ca`, to expire in `days_left` days."""
    leaf = _chain(chain_file)[0]
    builder = (
        x509.CertificateBuilder()
        .subject_name(leaf.subject).issuer_name(ca.subject)
        .public_key(leaf.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(_now() - dt.timedelta(days=300))
        .not_valid_after(_now() + dt.timedelta(days=days_left))
    )
    for ext in leaf.extensions:
        builder = builder.add_extension(ext.value, ext.critical)
    chain_file.write_bytes(_pem(builder.sign(ca_key, hashes.SHA384())) + _pem(ca))


# -- What it writes, and where ------------------------------------------------
# `docker compose up` creates every container before edge-certs has run, and
# Docker creates a missing bind-mount source as an empty directory. Mounted
# file by file, that once left directories named keycloak.crt and
# keycloak.key in tls/, and issuing then failed (IsADirectoryError).

def test_it_writes_the_two_certificates_into_the_directories_docker_created_first(pki: Pki):
    for name in (web_tls.EDGE_DIR, web_tls.KEYCLOAK_DIR):
        (pki.tls / name).mkdir(parents=True)

    assert pki.issue() is True

    assert sorted(path for path, entry in _snapshot(pki.tls).items() if entry) == LEAF_FILES
    # Keycloak runs as uid 1000 in group 0; its key is group-readable, never world-readable.
    assert stat.S_IMODE((pki.tls / "edge" / "edge.key").stat().st_mode) == 0o600
    assert stat.S_IMODE((pki.tls / "keycloak" / "keycloak.key").stat().st_mode) == 0o640
    web_ca = pki.web_ca
    for name, dns_names in (("edge/edge", ("localhost",)), ("keycloak/keycloak", ("keycloak", "localhost"))):
        leaf, *rest = _chain(pki.tls / f"{name}.crt")
        assert rest == [web_ca], "the leaf and the web TLS CA, never the root"
        leaf.verify_directly_issued_by(web_ca)
        key = serialization.load_pem_private_key((pki.tls / f"{name}.key").read_bytes(), password=None)
        assert key.public_key().public_bytes(serialization.Encoding.DER,
                                             serialization.PublicFormat.SubjectPublicKeyInfo) == _spki(leaf)
        sans = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        assert set(sans.get_values_for_type(x509.DNSName)) == set(dns_names)
        assert sans.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address("127.0.0.1")]
        assert leaf.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == dns_names[0]
        assert list(leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == [
            ExtendedKeyUsageOID.SERVER_AUTH]
        basic = leaf.extensions.get_extension_for_class(x509.BasicConstraints)
        assert basic.critical and basic.value.ca is False
        assert leaf.not_valid_after_utc <= _now() + dt.timedelta(days=397), "at most 397 days after the run"


def test_a_second_run_writes_nothing(pki: Pki):
    assert pki.issue() is True               # into a tls directory that does not exist yet
    before = _snapshot(pki.tls)
    assert pki.issue() is False
    assert _snapshot(pki.tls) == before


# -- Renewal ------------------------------------------------------------------

def test_a_leaf_with_30_days_or_fewer_left_is_reissued_and_one_with_more_is_kept(pki: Pki):
    pki.issue()
    edge, keycloak = pki.tls / "edge" / "edge.crt", pki.tls / "keycloak" / "keycloak.crt"
    _resign(edge, pki.web_ca, pki.web_key, days_left=30)
    _resign(keycloak, pki.web_ca, pki.web_key, days_left=31)
    edge_key = (pki.tls / "edge" / "edge.key").read_bytes()
    kept = _snapshot(pki.tls / "keycloak")

    assert pki.issue() is True

    assert _chain(edge)[0].not_valid_after_utc - _now() > dt.timedelta(days=396), "re-issued for a full term"
    assert (pki.tls / "edge" / "edge.key").read_bytes() != edge_key, "with a fresh key"
    assert _snapshot(pki.tls / "keycloak") == kept


def test_a_new_web_tls_ca_reissues_both_leaves_and_the_chains_carry_it(pki: Pki):
    pki.issue()
    old = pki.web_ca
    new = pki.install_web_ca()               # the yearly renewal: a new key under the same name
    assert new.subject == old.subject and _spki(new) != _spki(old)

    assert pki.issue() is True

    for name in ("edge/edge.crt", "keycloak/keycloak.crt"):
        leaf, ca = _chain(pki.tls / name)
        assert ca == new
        leaf.verify_directly_issued_by(new)


def test_a_leaf_from_another_authority_is_reissued(pki: Pki):
    """A stand-in for the authority this branch replaced: same names and files, another issuer."""
    pki.issue()
    other, other_key = _ca("another authority", None, ((x509.BasicConstraints(ca=True, path_length=0), True),),
                           key=ec.generate_private_key(ec.SECP256R1()), days=3650)
    _resign(pki.tls / "edge" / "edge.crt", other, other_key, days_left=300)

    assert pki.issue() is True

    leaf, ca = _chain(pki.tls / "edge" / "edge.crt")
    assert ca == pki.web_ca
    leaf.verify_directly_issued_by(pki.web_ca)


# -- Refusals: nothing is issued without a sound web TLS CA --------------------

def _another_root_signs(p: Pki) -> None:
    p.install_web_ca(signed_by=_ca("sentinelfaa Root CA (another)", None, ROOT_POLICY))


def _expired_root(p: Pki) -> None:
    root = _ca("sentinelfaa Root CA (throwaway)", None, ROOT_POLICY, not_before=_now() - dt.timedelta(days=3651),
               days=3650)
    (p.trust / "root-ca.crt").write_bytes(_pem(root[0]))
    p.install_web_ca(signed_by=root)


REFUSALS = {
    "missing intermediate": (lambda p: (p.web / "web-ca.crt").unlink(), "missing or unreadable"),
    "missing key": (lambda p: (p.web / "web-ca.key").unlink(), "missing or unreadable"),
    "missing root": (lambda p: (p.trust / "root-ca.crt").unlink(), "missing or unreadable"),
    "garbled certificate": (lambda p: (p.web / "web-ca.crt").write_text("not a certificate"), "not a PEM certificate"),
    "garbled root": (lambda p: (p.trust / "root-ca.crt").write_text("not a certificate"), "not a PEM certificate"),
    "garbled key": (lambda p: (p.web / "web-ca.key").write_text("not a key"), "not an unencrypted PEM private key"),
    "expired": (lambda p: p.install_web_ca(not_before=_now() - dt.timedelta(days=366), days=365),
                "outside its validity period"),
    "expired root": (_expired_root, "outside its validity period"),
    "from another root": (_another_root_signs, "not signed by the root"),
    "key mismatch": (lambda p: (p.web / "web-ca.key").write_bytes(_key_pem(ec.generate_private_key(ec.SECP384R1()))),
                     "not the key of"),
    "missing name constraints": (lambda p: p.install_web_ca(_policy(NameConstraints=None)),
                                 "does not carry the web TLS policy"),
    "one extra permitted name": (lambda p: p.install_web_ca(_policy(NameConstraints=x509.NameConstraints(
        permitted_subtrees=[*PERMITTED, x509.DNSName("example.com")], excluded_subtrees=EXCLUDED))),
        "does not carry the web TLS policy"),
    "may sign another CA": (lambda p: p.install_web_ca(_policy(
        BasicConstraints=x509.BasicConstraints(ca=True, path_length=None))), "does not carry the web TLS policy"),
    "may sign more than certificates": (lambda p: p.install_web_ca(_policy(
        KeyUsage=_usage(key_cert_sign=True, crl_sign=True, digital_signature=True))),
        "does not carry the web TLS policy"),
    "may issue client certificates": (lambda p: p.install_web_ca(_policy(ExtendedKeyUsage=x509.ExtendedKeyUsage(
        [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]))), "does not carry the web TLS policy"),
}


def _says_what_to_do(err: str, reason: str) -> None:
    assert reason in err and "Nothing was issued" in err
    for step in (r".\instraclustr\scripts\pki.ps1 request", r".\instraclustr\scripts\pki.ps1 sign -RootDir",
                 r".\instraclustr\scripts\pki.ps1 install", "instraclustr/certs/README.md"):
        assert step in err, step


def test_without_the_web_tls_ca_it_refuses_and_creates_nothing(pki: Pki, capsys):
    """Before the ceremony instraclustr/certs/web is empty: edge-certs stops there, it does not improvise."""
    (pki.web / "web-ca.crt").unlink()
    (pki.web / "web-ca.key").unlink()

    with pytest.raises(web_tls.WebCaRefused, match="missing or unreadable"):
        pki.issue()
    assert web_tls.main(pki.args()) == 2

    err = capsys.readouterr().err
    _says_what_to_do(err, "missing or unreadable")
    assert str(pki.web / "web-ca.crt") in err
    assert not pki.tls.exists()


@pytest.mark.parametrize("case", list(REFUSALS))
def test_a_refusal_issues_nothing_and_names_the_ceremony(pki: Pki, case: str, capsys):
    pki.issue()                              # leaves already in place: they stay exactly as they are
    before = _snapshot(pki.tls)
    break_it, reason = REFUSALS[case]
    break_it(pki)

    with pytest.raises(web_tls.WebCaRefused, match=re.escape(reason)):
        pki.issue()
    assert web_tls.main(pki.args()) == 2

    _says_what_to_do(capsys.readouterr().err, reason)
    assert _snapshot(pki.tls) == before


def test_main_says_whether_it_issued_and_where(pki: Pki, capsys):
    assert web_tls.main(pki.args()) == 0
    out = capsys.readouterr().out
    assert out.startswith("issued ")
    assert str(pki.tls / "edge" / "edge.crt") in out and str(pki.tls / "keycloak" / "keycloak.crt") in out
    assert "recreate edge, keycloak, dashboard and cloudflared" in out

    assert web_tls.main(pki.args()) == 0
    assert capsys.readouterr().out.startswith("kept existing ")


def test_main_takes_exactly_the_three_directories(capsys):
    assert web_tls.main(["/web-ca", "/certs-trust"]) == 2
    assert "usage: python -m src.web_tls <web-ca dir> <trust dir> <tls dir>" in capsys.readouterr().err


# -- The OIDC client, over a real TLS handshake --------------------------------

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
def keycloak_like_server(pki: Pki):
    """An HTTPS server on loopback presenting Keycloak's certificate and chain."""
    pki.issue()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(pki.tls / "keycloak" / "keycloak.crt", pki.tls / "keycloak" / "keycloak.key")
    server = HTTPServer(("127.0.0.1", 0), _Discovery)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"https://localhost:{server.server_address[1]}/", pki.trust / "root-ca.crt"
    server.shutdown()
    server.server_close()


# Streamlit's OIDC client is Authlib over httpx, and secrets.toml's
# [auth.keycloak.client_kwargs] verify reaches httpx unchanged: this is how the
# dashboard's discovery, token and signing-key requests trust Keycloak.

def test_the_oidc_client_verifies_keycloak_against_the_root(keycloak_like_server):
    url, root = keycloak_like_server
    assert httpx.get(url, verify=str(root), timeout=5.0).json() == {"issuer": "https://localhost/realms/x"}


def test_the_oidc_client_refuses_keycloak_under_any_other_root(keycloak_like_server, tmp_path: Path):
    url, _ = keycloak_like_server
    other = tmp_path / "other-root.crt"
    other.write_bytes(_pem(_ca("sentinelfaa Root CA (another)", None, ROOT_POLICY)[0]))
    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        httpx.get(url, verify=str(other), timeout=5.0)
    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        httpx.get(url, timeout=5.0)                             # system trust store: not the offline root


# -- Compose: who mounts what ----------------------------------------------------

def _compose() -> dict:
    return yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))


def _volumes(service: dict) -> list[str]:
    return [v for v in service.get("volumes", []) if isinstance(v, str)]


def test_every_tls_mount_in_compose_is_a_directory_the_issuer_fills():
    sources = {v.split(":")[0] for s in _compose()["services"].values() for v in _volumes(s) if v.startswith("./tls")}
    allowed = {"./tls"} | {f"./tls/{name}" for name in (web_tls.EDGE_DIR, web_tls.KEYCLOAK_DIR)}
    assert sources and sources <= allowed, f"file mounts under ./tls: {sorted(sources - allowed)}"


def test_only_edge_certs_holds_the_web_tls_key_and_every_client_trusts_the_root_read_only():
    services = _compose()["services"]
    edge_certs = services["edge-certs"]
    assert edge_certs["command"] == ["python", "-m", "src.web_tls", "/web-ca", "/certs-trust", "/tls"]
    assert _volumes(edge_certs) == ["./tls:/tls", "../instraclustr/certs/web:/web-ca:ro",
                                    "../instraclustr/certs/trust:/certs-trust:ro"]
    assert edge_certs["user"] == "0" and edge_certs["restart"] == "no"
    for name, service in services.items():
        if name != "edge-certs":
            sources = {v.split(":")[0] for v in _volumes(service)}
            assert not sources & {"./tls", "../instraclustr/certs", "../instraclustr/certs/web"}, name
    for name, target in (("dashboard", "/run/tls"), ("edge", "/etc/nginx/trust"),
                         ("keycloak", "/opt/keycloak/conf/trust"), ("cloudflared", "/etc/cloudflared/trust")):
        assert f"../instraclustr/certs/trust:{target}:ro" in _volumes(services[name]), name
    assert not [v for s in services.values() for v in _volumes(s) if v.startswith("./tls/trust")]
    assert services["keycloak"]["healthcheck"]["test"][-1] == "/opt/keycloak/conf/trust/root-ca.crt"


def test_the_realm_file_fits_keycloaks_columns():
    """CLIENT.DESCRIPTION and KEYCLOAK_ROLE.DESCRIPTION are VARCHAR(255): longer and the import fails."""
    realm = json.loads((REPO / "keycloak" / "realm-nas-sentinel.json").read_text(encoding="utf-8"))
    for client in realm.get("clients", []):
        assert len(client.get("description") or "") <= 255, client["clientId"]
    for role in realm.get("roles", {}).get("realm", []):
        assert len(role.get("description") or "") <= 255, role["name"]


def test_compose_serves_keycloak_over_https_only():
    compose = _compose()
    env = compose["services"]["keycloak"]["environment"]
    assert env["KC_HTTP_ENABLED"] == "false"
    # The public sign-in address is a setting (docs/public-access.md); its
    # default, and the admin console always, are https on loopback.
    hostname = re.fullmatch(r"\$\{KEYCLOAK_BROWSER_URL:-(.+)\}", env["KC_HOSTNAME"])
    assert hostname and hostname.group(1).startswith("https://"), env["KC_HOSTNAME"]
    assert env["KC_HOSTNAME_ADMIN"] == "https://localhost:8081"
    assert env["KC_HTTPS_CERTIFICATE_FILE"] and env["KC_HTTPS_CERTIFICATE_KEY_FILE"]
    assert compose["services"]["keycloak"]["ports"] == ["127.0.0.1:8081:8443"]
    mounts = " ".join(compose["services"]["keycloak"]["volumes"])
    assert "instraclustr/certs/web" not in mounts, "Keycloak mounts nothing under instraclustr/certs/web"
