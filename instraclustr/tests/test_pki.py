"""
Workload-identity and web TLS PKI (instraclustr/scripts/pki.sh, policy in
certs/policy/): the trust anchor Vault's cert auth, every Vault client, the
browser and every client of the edge and Keycloak verify against.

Runs the real ceremony -- root-init, request, sign, install, check -- with a
throwaway root, and checks what must hold:
  * custody: the root key exists only where root-init put it (the USB), is
    passphrase-encrypted, and no CSR or pending file outlives the ceremony;
  * Vault's certificate verifies for the names its clients use;
  * the SPIRE branch is name-constrained to the trust domain with no usable
    DNS name or IP address, so SPIRE can never mint a certificate a Vault
    client would accept as Vault -- checked with real TLS handshakes;
  * the web TLS branch issues TLS server certificates for localhost,
    keycloak and 127.0.0.1 only, and no spiffe:// identity: the edge's and
    Keycloak's certificates from nas-feedops-sentinel/src/web_tls.py verify
    against the root alone, and anything else minted with its key is
    refused -- checked with real TLS handshakes;
  * `sign` applies the policy, never what a CSR asks for;
  * `install` and `check` refuse certificates from another root, for another
    key, or outside the policy, and `check` warns two months ahead.

Run (the test image has pytest, cryptography and OpenSSL 3):
    docker run --rm -v "$PWD/instraclustr:/infra:ro" -w /infra \
        nas-feedops-sentinel:test python -m pytest -p no:cacheprovider -q tests
Go (Vault, SPIRE, cloudflared) and OpenSSL (nginx, the dashboard) are what
verify these chains in production; the live harness (scripts/smoke/) proves
that end to end.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import ipaddress
import os
import re
import shutil
import ssl
import stat
import subprocess
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError

INFRA = Path(__file__).resolve().parent.parent
PKI = INFRA / "scripts" / "pki.sh"
POLICY = INFRA / "certs" / "policy"
TRUST_DOMAIN = "sentinelfaa.internal"
PASSPHRASE = "throwaway-test-root-passphrase"
# The issuer edge-certs runs (standard library and cryptography only, so it
# loads here by path).
WEB_TLS = INFRA.parent / "nas-feedops-sentinel" / "src" / "web_tls.py"
# OpenSSL's reason for a certificate used for a purpose its chain does not
# allow (X509_V_ERR_INVALID_PURPOSE): "unsupported certificate purpose" in
# older OpenSSL 3 releases, "unsuitable certificate purpose" in 3.5, which the
# test image has.
WRONG_PURPOSE = "uns(?:upported|uitable) certificate purpose"

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")
needs_app = pytest.mark.skipif(not WEB_TLS.is_file(), reason="needs the application's src/web_tls.py beside instraclustr/")


def pki(*args: str, passphrase: str = PASSPHRASE, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "PKI_ROOT_PASS": "env:PKI_TEST_PASS", "PKI_TEST_PASS": passphrase}
    result = subprocess.run(["sh", str(PKI), *args], capture_output=True, text=True, env=env)
    if check and result.returncode != 0:
        raise AssertionError(f"pki.sh {' '.join(args)} exited {result.returncode}:\n{result.stdout}{result.stderr}")
    return result


@pytest.fixture
def ceremony(tmp_path: Path) -> tuple[Path, Path]:
    """A complete ceremony: (usb, certs)."""
    usb, certs = tmp_path / "usb", tmp_path / "certs"
    pki("root-init", str(usb))
    pki("request", str(certs))
    pki("sign", str(usb), str(certs))
    pki("install", str(certs))
    return usb, certs


def _cert(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def _root_key(usb: Path):
    return serialization.load_pem_private_key((usb / "root-ca.key").read_bytes(), password=PASSPHRASE.encode())


# -- Custody ------------------------------------------------------------------

def test_ceremony_leaves_each_file_where_the_custody_matrix_says(ceremony):
    usb, certs = ceremony
    assert sorted(p.name for p in usb.iterdir()) == ["root-ca.crt", "root-ca.key", "root-ca.srl"]
    assert sorted(str(p.relative_to(certs)).replace(os.sep, "/") for p in certs.rglob("*") if p.is_file()) == [
        "spire/intermediate-ca.crt", "spire/intermediate-ca.key",
        "trust/root-ca.crt", "vault/vault.crt", "vault/vault.key",
        "web/web-ca.crt", "web/web-ca.key",
    ], "no root key, CSR or pending file on the host"
    assert (certs / "trust" / "root-ca.crt").read_bytes() == (usb / "root-ca.crt").read_bytes()


def test_root_key_is_passphrase_encrypted(ceremony):
    usb, _ = ceremony
    assert (usb / "root-ca.key").read_text().startswith("-----BEGIN ENCRYPTED PRIVATE KEY-----")
    with pytest.raises(TypeError):          # no password: refused
        serialization.load_pem_private_key((usb / "root-ca.key").read_bytes(), password=None)


def test_issued_keys_are_owner_only(ceremony):
    _, certs = ceremony
    assert stat.S_IMODE((certs / "spire" / "intermediate-ca.key").stat().st_mode) == 0o600
    assert stat.S_IMODE((certs / "vault" / "vault.key").stat().st_mode) == 0o400
    assert stat.S_IMODE((certs / "web" / "web-ca.key").stat().st_mode) == 0o600


def test_root_init_never_overwrites_a_root(ceremony):
    usb, _ = ceremony
    before = (usb / "root-ca.key").read_bytes()
    result = pki("root-init", str(usb), check=False)
    assert result.returncode == 2 and "created once" in result.stderr
    assert (usb / "root-ca.key").read_bytes() == before


def test_wrong_passphrase_signs_nothing(tmp_path):
    usb, certs = tmp_path / "usb", tmp_path / "certs"
    pki("root-init", str(usb))
    pki("request", str(certs))
    result = pki("sign", str(usb), str(certs), passphrase="not-the-passphrase", check=False)
    assert result.returncode == 2 and "does not open" in result.stderr
    assert not list(certs.rglob("*.crt.pending"))


# -- The policy ---------------------------------------------------------------

def test_root_allows_exactly_the_two_ca_levels_below_it(ceremony):
    usb, _ = ceremony
    root = _cert(usb / "root-ca.crt")
    assert root.extensions.get_extension_for_class(x509.BasicConstraints).value.path_length == 2
    assert isinstance(root.public_key(), ec.EllipticCurvePublicKey) and root.public_key().curve.name == "secp384r1"


def test_spire_branch_is_constrained_to_the_trust_domain_with_no_usable_hostnames(ceremony):
    _, certs = ceremony
    upstream = _cert(certs / "spire" / "intermediate-ca.crt")
    ext = upstream.extensions.get_extension_for_class(x509.NameConstraints)
    assert ext.critical, "RFC 5280: name constraints MUST be critical"
    assert ext.value.permitted_subtrees == [
        x509.UniformResourceIdentifier(TRUST_DOMAIN), x509.DNSName("invalid")]
    assert {str(n.value) for n in ext.value.excluded_subtrees} == {"0.0.0.0/0", "::/0"}
    assert upstream.extensions.get_extension_for_class(x509.BasicConstraints).value.path_length == 1


WEB_PERMITTED = {x509.DNSName("localhost"), x509.DNSName("keycloak"),
                 x509.IPAddress(ipaddress.ip_network("127.0.0.1/32")), x509.UniformResourceIdentifier("invalid")}
WEB_EXCLUDED = {x509.IPAddress(ipaddress.ip_network("::/0"))}


def test_web_branch_issues_tls_servers_only_for_localhost_keycloak_and_127_0_0_1(ceremony):
    _, certs = ceremony
    web = _cert(certs / "web" / "web-ca.crt")
    assert {type(e.value) for e in web.extensions} == {
        x509.BasicConstraints, x509.KeyUsage, x509.ExtendedKeyUsage, x509.NameConstraints,
        x509.SubjectKeyIdentifier, x509.AuthorityKeyIdentifier,
    }, "exactly the policy's extensions"
    names = web.extensions.get_extension_for_class(x509.NameConstraints)
    assert names.critical, "RFC 5280: name constraints MUST be critical"
    assert set(names.value.permitted_subtrees) == WEB_PERMITTED, "and URI invalid: no spiffe:// identity"
    assert set(names.value.excluded_subtrees) == WEB_EXCLUDED
    basic = web.extensions.get_extension_for_class(x509.BasicConstraints)
    assert basic.critical and basic.value.ca and basic.value.path_length == 0, "leaves only, never another CA"
    usage = web.extensions.get_extension_for_class(x509.KeyUsage)
    assert usage.critical and usage.value.key_cert_sign and usage.value.crl_sign
    assert not (usage.value.digital_signature or usage.value.content_commitment or usage.value.key_encipherment
                or usage.value.data_encipherment or usage.value.key_agreement)
    purpose = web.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    assert list(purpose.value) == [ExtendedKeyUsageOID.SERVER_AUTH], "TLS servers only"
    assert isinstance(web.public_key(), ec.EllipticCurvePublicKey) and web.public_key().curve.name == "secp384r1"
    assert web.subject.rfc4514_string() == "CN=sentinelfaa Web TLS CA,O=sentinelfaa"


def test_vault_certificate_verifies_for_the_names_its_clients_use(ceremony):
    usb, certs = ceremony
    root, leaf = _cert(usb / "root-ca.crt"), _cert(certs / "vault" / "vault.crt")
    for name in ("vault", "localhost"):
        verifier = PolicyBuilder().store(Store([root])).build_server_verifier(x509.DNSName(name))
        assert verifier.verify(leaf, [])[-1] == root
    with pytest.raises(VerificationError):
        PolicyBuilder().store(Store([root])).build_server_verifier(x509.DNSName("postgres")).verify(leaf, [])


def test_sign_applies_the_policy_not_what_the_csr_asks_for(tmp_path):
    """A request for the Vault slot that asks to be a CA for evil.example gets the policy."""
    usb, certs = tmp_path / "usb", tmp_path / "certs"
    pki("root-init", str(usb))
    pki("request", str(certs))
    key = ec.generate_private_key(ec.SECP256R1())
    (certs / "vault" / "vault.key.pending").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    greedy = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vault")]))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("evil.example")]), critical=False)
        # One the policy does not set at all: must not appear either.
        .add_extension(x509.OCSPNoCheck(), critical=False)
        .sign(key, hashes.SHA256())
    )
    (certs / "vault" / "vault.csr").write_bytes(greedy.public_bytes(serialization.Encoding.PEM))

    pki("sign", str(usb), str(certs))
    pki("install", str(certs))

    cert = _cert(certs / "vault" / "vault.crt")
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert set(sans.get_values_for_type(x509.DNSName)) == {"vault", "localhost"}
    assert {type(e.value) for e in cert.extensions} == {
        x509.BasicConstraints, x509.KeyUsage, x509.ExtendedKeyUsage, x509.SubjectAlternativeName,
        x509.SubjectKeyIdentifier, x509.AuthorityKeyIdentifier,
    }, "exactly the policy's extensions"


def test_sign_refuses_a_key_the_policy_does_not_allow(tmp_path):
    usb, certs = tmp_path / "usb", tmp_path / "certs"
    pki("root-init", str(usb))
    pki("request", str(certs))
    weak = ec.generate_private_key(ec.SECP256R1())           # the SPIRE CA must be P-384
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "x")])).sign(weak, hashes.SHA256()))
    (certs / "spire" / "intermediate-ca.csr").write_bytes(csr.public_bytes(serialization.Encoding.PEM))

    result = pki("sign", str(usb), str(certs), check=False)
    assert result.returncode == 2 and "policy wants P-384" in result.stderr


def test_policy_trust_domain_matches_spire(ceremony):
    signing = (POLICY / "signing.cnf").read_text(encoding="utf-8")
    assert f"permitted;URI:{TRUST_DOMAIN}," in signing
    for conf in ("server.conf", "agent.conf"):
        text = (INFRA / "spire" / conf).read_text(encoding="utf-8")
        assert re.search(rf'trust_domain\s*=\s*"{re.escape(TRUST_DOMAIN)}"', text), conf


# -- Real TLS handshakes (OpenSSL) -------------------------------------------
# cryptography's own verifier cannot judge the SPIRE branch: it rejects URI
# name constraints as unsupported, so it would "reject" every chain under
# that branch, legitimate SVIDs included. OpenSSL implements them, so these
# handshakes check the constraints for the right reason.

def _under_spire_branch(certs: Path, tmp_path: Path, name: str, san: x509.GeneralName) -> tuple[Path, Path]:
    """A leaf signed by the SPIRE upstream CA: (chain file, key file)."""
    upstream = _cert(certs / "spire" / "intermediate-ca.crt")
    upstream_key = serialization.load_pem_private_key(
        (certs / "spire" / "intermediate-ca.key").read_bytes(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SPIRE")]))
        .issuer_name(upstream.subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1))
        .not_valid_after(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([san]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(upstream_key.public_key()), critical=False)
        .sign(upstream_key, hashes.SHA256())
    )
    chain, key_file = tmp_path / f"{name}.crt", tmp_path / f"{name}.key"
    chain.write_bytes(leaf.public_bytes(serialization.Encoding.PEM) + upstream.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return chain, key_file


def _under_web_branch(certs: Path, tmp_path: Path, name: str, sans: list[x509.GeneralName],
                      purposes: tuple = (ExtendedKeyUsageOID.SERVER_AUTH,)) -> tuple[Path, Path]:
    """A leaf signed with the web TLS CA's key, with these EKU purposes (none: no EKU): (chain file, key file).

    serverAuth by default, so a rejection fails on the name, not on the purpose.
    """
    web = _cert(certs / "web" / "web-ca.crt")
    web_key = serialization.load_pem_private_key((certs / "web" / "web-ca.key").read_bytes(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "web")]))
        .issuer_name(web.subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1))
        .not_valid_after(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(web_key.public_key()), critical=False)
    )
    if purposes:
        builder = builder.add_extension(x509.ExtendedKeyUsage(list(purposes)), critical=False)
    chain, key_file = tmp_path / f"{name}.crt", tmp_path / f"{name}.key"
    chain.write_bytes(builder.sign(web_key, hashes.SHA384()).public_bytes(serialization.Encoding.PEM)
                      + web.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return chain, key_file


def _handshake(client_ctx: ssl.SSLContext, server_ctx: ssl.SSLContext, server_hostname: str) -> None:
    """A full TLS handshake over memory BIOs; a verification failure raises."""
    c_in, c_out, s_in, s_out = ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO()
    sides = [
        [client_ctx.wrap_bio(c_in, c_out, server_hostname=server_hostname), c_out, s_in, False],
        [server_ctx.wrap_bio(s_in, s_out, server_side=True), s_out, c_in, False],
    ]
    for _ in range(10):
        for side in sides:
            tls, outgoing, peer_incoming, done = side
            if not done:
                try:
                    tls.do_handshake()
                    side[3] = True
                except ssl.SSLWantReadError:
                    pass
            if outgoing.pending:
                peer_incoming.write(outgoing.read())
        if sides[0][3] and sides[1][3]:
            return
    raise AssertionError("handshake did not complete")


def _vault_server(certs: Path, require_client_cert: bool) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certs / "vault" / "vault.crt", certs / "vault" / "vault.key")
    if require_client_cert:
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(certs / "trust" / "root-ca.crt")
    return ctx


def _client(certs: Path, cert: tuple[Path, Path] | None = None) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certs / "trust" / "root-ca.crt")
    if cert:
        ctx.load_cert_chain(*cert)
    return ctx


def _server(chain: tuple[Path, Path]) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(*chain)
    return ctx


def test_vault_clients_trusting_the_root_accept_vault(ceremony):
    _, certs = ceremony
    _handshake(_client(certs), _vault_server(certs, False), "vault")


def test_spire_cannot_mint_a_certificate_clients_accept_as_vault(ceremony, tmp_path):
    _, certs = ceremony
    rogue = _under_spire_branch(certs, tmp_path, "rogue", x509.DNSName("vault"))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(*rogue)

    with pytest.raises(ssl.SSLCertVerificationError, match="permitted subtree violation"):
        _handshake(_client(certs), server, "vault")


def test_spire_cannot_mint_a_certificate_for_an_ip_address(ceremony, tmp_path):
    _, certs = ceremony
    import ipaddress
    rogue = _under_spire_branch(certs, tmp_path, "rogue-ip", x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(*rogue)

    with pytest.raises(ssl.SSLCertVerificationError, match="excluded subtree violation"):
        _handshake(_client(certs), server, "127.0.0.1")


def test_a_spiffe_id_in_the_trust_domain_authenticates_over_mutual_tls(ceremony, tmp_path):
    _, certs = ceremony
    svid = _under_spire_branch(certs, tmp_path, "api",
                               x509.UniformResourceIdentifier(f"spiffe://{TRUST_DOMAIN}/workload/api"))
    _handshake(_client(certs, svid), _vault_server(certs, True), "vault")


def test_a_spiffe_id_outside_the_trust_domain_is_refused(ceremony, tmp_path):
    _, certs = ceremony
    foreign = _under_spire_branch(certs, tmp_path, "foreign",
                                  x509.UniformResourceIdentifier("spiffe://evil.example/workload/api"))
    with pytest.raises(ssl.SSLError, match="permitted subtree violation"):
        _handshake(_client(certs, foreign), _vault_server(certs, True), "vault")


# -- The web TLS branch, with real handshakes ----------------------------------

def _web_tls():
    """nas-feedops-sentinel/src/web_tls.py, the issuer edge-certs runs, loaded by path."""
    spec = importlib.util.spec_from_file_location("web_tls", WEB_TLS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@needs_app
def test_the_issuers_certificates_verify_against_the_root_alone(ceremony, tmp_path):
    """What edge-certs writes, served as nginx and Keycloak serve it, to clients holding only root-ca.crt."""
    _, certs = ceremony
    tls = tmp_path / "tls"

    assert _web_tls().ensure_certificates(certs / "web", certs / "trust", tls) is True

    for leaf, names in (("edge/edge", ("localhost", "127.0.0.1")),
                        ("keycloak/keycloak", ("keycloak", "localhost", "127.0.0.1"))):
        server = _server((tls / f"{leaf}.crt", tls / f"{leaf}.key"))
        for name in names:
            _handshake(_client(certs), server, name)


def test_the_web_branch_cannot_mint_a_certificate_for_any_other_name(ceremony, tmp_path):
    _, certs = ceremony
    rogue = _under_web_branch(certs, tmp_path, "rogue", [x509.DNSName("example.com")])

    with pytest.raises(ssl.SSLCertVerificationError, match="permitted subtree violation"):
        _handshake(_client(certs), _server(rogue), "example.com")


# ::1 is outside the one permitted address and inside the excluded IPv6
# range: OpenSSL may name either subtree.
@pytest.mark.parametrize("address, reason", [("127.0.0.2", "permitted subtree violation"),
                                             ("::1", "subtree violation")])
def test_the_web_branch_cannot_mint_a_certificate_for_any_other_address(ceremony, tmp_path, address, reason):
    _, certs = ceremony
    rogue = _under_web_branch(certs, tmp_path, "rogue-ip", [x509.IPAddress(ipaddress.ip_address(address))])

    with pytest.raises(ssl.SSLCertVerificationError, match=reason):
        _handshake(_client(certs), _server(rogue), address)


def test_the_web_branch_cannot_mint_a_certificate_without_server_auth(ceremony, tmp_path):
    _, certs = ceremony
    client_only = _under_web_branch(certs, tmp_path, "client-only", [x509.DNSName("localhost")],
                                    purposes=(ExtendedKeyUsageOID.CLIENT_AUTH,))

    with pytest.raises(ssl.SSLCertVerificationError, match=WRONG_PURPOSE):
        _handshake(_client(certs), _server(client_only), "localhost")


@pytest.mark.parametrize("name", ["localhost", "keycloak"])
def test_spire_cannot_mint_a_certificate_clients_accept_as_the_edge_or_keycloak(ceremony, tmp_path, name):
    _, certs = ceremony
    rogue = _under_spire_branch(certs, tmp_path, "rogue", x509.DNSName(name))

    with pytest.raises(ssl.SSLCertVerificationError, match="permitted subtree violation"):
        _handshake(_client(certs), _server(rogue), name)


def test_the_web_branch_cannot_mint_a_spiffe_identity(ceremony, tmp_path):
    _, certs = ceremony
    spiffe = x509.UniformResourceIdentifier(f"spiffe://{TRUST_DOMAIN}/workload/api")
    served = _under_web_branch(certs, tmp_path, "served", [x509.DNSName("localhost"), spiffe])

    with pytest.raises(ssl.SSLCertVerificationError, match="permitted subtree violation"):
        _handshake(_client(certs), _server(served), "localhost")

    # Presented to Vault as a client certificate, the way an SVID logs in:
    # refused, because the web TLS CA is for TLS servers only.
    presented = _under_web_branch(certs, tmp_path, "presented", [spiffe], purposes=())
    with pytest.raises(ssl.SSLError, match=WRONG_PURPOSE):
        _handshake(_client(certs, presented), _vault_server(certs, True), "vault")


# -- install and check refuse what does not fit -----------------------------

def _age(path: Path, days_left: int, usb: Path) -> None:
    """Re-sign an installed certificate (same key, same extensions) to expire in `days_left` days."""
    cert, root = _cert(path), _cert(usb / "root-ca.crt")
    builder = (
        x509.CertificateBuilder()
        .subject_name(cert.subject).issuer_name(root.subject)
        .public_key(cert.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=300))
        .not_valid_after(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days_left))
    )
    for ext in cert.extensions:
        builder = builder.add_extension(ext.value, ext.critical)
    path.write_bytes(builder.sign(_root_key(usb), hashes.SHA384()).public_bytes(serialization.Encoding.PEM))


def test_check_is_clean_after_the_ceremony(ceremony):
    _, certs = ceremony
    result = pki("check", str(certs))
    assert result.stdout.count("OK ") == 3 and "OK web/web-ca" in result.stdout


def test_check_warns_two_months_ahead_and_renewal_clears_it(ceremony):
    usb, certs = ceremony
    _age(certs / "vault" / "vault.crt", 20, usb)

    due = pki("check", str(certs), check=False)
    assert due.returncode == 1 and "DUE vault/vault" in due.stdout

    pki("request", str(certs))
    pki("sign", str(usb), str(certs))
    pki("install", str(certs))
    assert pki("check", str(certs)).returncode == 0


def test_check_reports_a_missing_key_as_broken(ceremony):
    _, certs = ceremony
    (certs / "spire" / "intermediate-ca.key").unlink()
    result = pki("check", str(certs), check=False)
    assert result.returncode == 2 and "BROKEN spire/intermediate-ca: missing" in result.stdout


def test_check_warns_two_months_ahead_of_the_web_tls_ca_and_renewal_clears_it(ceremony):
    usb, certs = ceremony
    _age(certs / "web" / "web-ca.crt", 20, usb)

    due = pki("check", str(certs), check=False)
    assert due.returncode == 1 and "DUE web/web-ca" in due.stdout

    pki("request", str(certs))
    pki("sign", str(usb), str(certs))
    pki("install", str(certs))
    assert pki("check", str(certs)).returncode == 0


def test_check_reports_a_missing_web_tls_key_as_broken(ceremony):
    _, certs = ceremony
    (certs / "web" / "web-ca.key").unlink()
    result = pki("check", str(certs), check=False)
    assert result.returncode == 2 and "BROKEN web/web-ca: missing" in result.stdout


def _resign_web_ca(usb: Path, certs: Path, names: x509.NameConstraints | None) -> None:
    """Re-sign the installed web TLS CA with the root (same key and extensions), with these name constraints."""
    path = certs / "web" / "web-ca.crt"
    cert, root = _cert(path), _cert(usb / "root-ca.crt")
    builder = (
        x509.CertificateBuilder()
        .subject_name(cert.subject).issuer_name(root.subject)
        .public_key(cert.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(cert.not_valid_before_utc).not_valid_after(cert.not_valid_after_utc)
    )
    for ext in cert.extensions:
        if not isinstance(ext.value, x509.NameConstraints):
            builder = builder.add_extension(ext.value, ext.critical)
    if names is not None:
        builder = builder.add_extension(names, critical=True)
    path.write_bytes(builder.sign(_root_key(usb), hashes.SHA384()).public_bytes(serialization.Encoding.PEM))


@pytest.mark.parametrize("names", [
    None,
    # The policy's names in the policy's order, and one more.
    x509.NameConstraints(permitted_subtrees=[
        x509.DNSName("localhost"), x509.DNSName("keycloak"), x509.IPAddress(ipaddress.ip_network("127.0.0.1/32")),
        x509.UniformResourceIdentifier("invalid"), x509.DNSName("example.com"),
    ], excluded_subtrees=[x509.IPAddress(ipaddress.ip_network("::/0"))]),
], ids=["without name constraints", "with an extra permitted name"])
def test_check_reports_a_web_tls_ca_outside_the_policy_as_broken(ceremony, names):
    usb, certs = ceremony
    _resign_web_ca(usb, certs, names)

    result = pki("check", str(certs), check=False)
    assert result.returncode == 2
    assert "BROKEN web/web-ca: does not carry the web_tls_ext policy" in result.stdout


def test_check_reports_a_missing_root_as_broken(ceremony):
    _, certs = ceremony
    (certs / "trust" / "root-ca.crt").unlink()
    assert pki("check", str(certs), check=False).returncode == 2


def test_install_refuses_a_certificate_from_another_root(ceremony, tmp_path):
    usb, certs = ceremony
    other_usb, other_certs = tmp_path / "other-usb", tmp_path / "other-certs"
    pki("root-init", str(other_usb))
    pki("request", str(other_certs))
    pki("sign", str(other_usb), str(other_certs))
    shutil.copy(other_certs / "vault" / "vault.crt.pending", certs / "vault" / "vault.crt.pending")
    shutil.copy(other_certs / "vault" / "vault.key.pending", certs / "vault" / "vault.key.pending")

    result = pki("install", str(certs), check=False)
    assert result.returncode == 2 and "does not verify" in result.stderr


def test_install_refuses_a_certificate_for_another_key(ceremony):
    usb, certs = ceremony
    pki("request", str(certs))
    pki("sign", str(usb), str(certs))
    key = ec.generate_private_key(ec.SECP256R1())
    (certs / "vault" / "vault.key.pending").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    result = pki("install", str(certs), check=False)
    assert result.returncode == 2 and "not issued for" in result.stderr


def test_sign_refuses_a_certs_dir_that_trusts_a_different_root(ceremony, tmp_path):
    _, certs = ceremony
    other_usb = tmp_path / "other-usb"
    pki("root-init", str(other_usb))
    pki("request", str(certs))
    result = pki("sign", str(other_usb), str(certs), check=False)
    assert result.returncode == 2 and "different root" in result.stderr


# -- The passphrase at a real terminal ----------------------------------------
# The ceremony types the passphrase: once per command (root-init confirms it),
# handed to every OpenSSL call on a pipe. It used to be asked three times by
# OpenSSL itself, and a slip on the third left a root key with no
# certificate that then blocked every retry -- which is what happened to the
# first real ceremony.

def pki_tty(*args: str, inputs: list[str], timeout: float = 60) -> tuple[int, str]:
    """Run pki.sh on a pseudo-terminal, answering each passphrase prompt in turn."""
    import pty
    import select
    import time

    env = {k: v for k, v in os.environ.items() if k != "PKI_ROOT_PASS"}
    pid, fd = pty.fork()
    if pid == 0:                                   # child: the ceremony, on the tty
        os.execvpe("sh", ["sh", str(PKI), *args], env)
    out, answered, deadline = b"", 0, time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            if os.waitpid(pid, os.WNOHANG)[0]:
                break
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:                            # EIO: the child has exited
            break
        if not chunk:
            break
        out += chunk
        prompts = out.count(b"passphrase: ") + out.count(b"confirm: ")
        while answered < prompts and answered < len(inputs):
            os.write(fd, (inputs[answered] + "\n").encode())
            answered += 1
    else:
        os.kill(pid, 9)
        raise AssertionError(f"pki.sh {args} did not finish:\n{out.decode(errors='replace')}")
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), out.decode(errors="replace")


GOOD = "correct horse battery staple"


def test_root_init_asks_once_confirms_and_creates_the_root(tmp_path):
    code, out = pki_tty("root-init", str(tmp_path / "usb"), inputs=[GOOD, GOOD])
    assert code == 0, out
    assert out.count("Root key passphrase:") == 1 and out.count("Again, to confirm:") == 1
    assert GOOD not in out, "the passphrase is never echoed"
    key = (tmp_path / "usb" / "root-ca.key").read_bytes()
    assert serialization.load_pem_private_key(key, password=GOOD.encode())


def test_a_mismatched_confirmation_creates_nothing(tmp_path):
    code, out = pki_tty("root-init", str(tmp_path / "usb"), inputs=[GOOD, GOOD + "x"])
    assert code == 2 and "the two entries differ" in out
    assert not (tmp_path / "usb" / "root-ca.key").exists()


def test_a_short_passphrase_creates_nothing(tmp_path):
    code, out = pki_tty("root-init", str(tmp_path / "usb"), inputs=["short"])
    assert code == 2 and "at least 12 characters" in out
    assert not (tmp_path / "usb" / "root-ca.key").exists()


def test_sign_with_the_wrong_passphrase_signs_nothing_and_the_right_one_signs_them_all(tmp_path):
    usb, certs = tmp_path / "usb", tmp_path / "certs"
    assert pki_tty("root-init", str(usb), inputs=[GOOD, GOOD])[0] == 0
    pki("request", str(certs))

    code, out = pki_tty("sign", str(usb), str(certs), inputs=["not the passphrase"])
    assert code == 2 and "does not open" in out
    assert not list(certs.rglob("*.crt.pending")) and not list(certs.rglob("*.crt.signing"))

    code, out = pki_tty("sign", str(usb), str(certs), inputs=[GOOD])
    assert code == 0, out
    assert out.count("Root key passphrase:") == 1, "one entry signs every certificate"
    assert sorted(p.name for p in certs.rglob("*.crt.pending")) == [
        "intermediate-ca.crt.pending", "vault.crt.pending", "web-ca.crt.pending"]


def test_an_unfinished_root_init_says_exactly_what_to_do(tmp_path):
    usb = tmp_path / "usb"
    usb.mkdir()
    (usb / "root-ca.key").write_text("half-made\n", encoding="utf-8")   # a key, no certificate

    result = pki("root-init", str(usb), check=False)

    assert result.returncode == 2
    assert "unfinished root-init" in result.stderr and "Delete root-ca.key" in result.stderr


def test_a_failed_self_signature_leaves_nothing_behind(tmp_path):
    broken = tmp_path / "policy"
    shutil.copytree(POLICY, broken)
    (broken / "root.cnf").write_text("[ req ]\nprompt = no\ndistinguished_name = nope\n", encoding="utf-8")
    env = {**os.environ, "PKI_POLICY": str(broken), "PKI_ROOT_PASS": "env:PKI_TEST_PASS", "PKI_TEST_PASS": PASSPHRASE}

    result = subprocess.run(["sh", str(PKI), "root-init", str(tmp_path / "usb")],
                            capture_output=True, text=True, env=env)

    assert result.returncode == 2 and "nothing was kept" in result.stderr
    assert not (tmp_path / "usb" / "root-ca.key").exists()
