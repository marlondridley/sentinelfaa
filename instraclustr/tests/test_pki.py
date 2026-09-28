"""
Workload-identity PKI (instraclustr/scripts/pki.sh, policy in certs/policy/):
the trust anchor Vault's cert auth and every Vault client verify against.

Runs the real ceremony -- root-init, request, sign, install, check -- with a
throwaway root, and checks what must hold:
  * custody: the root key exists only where root-init put it (the USB), is
    passphrase-encrypted, and no CSR or pending file outlives the ceremony;
  * Vault's certificate verifies for the names its clients use;
  * the SPIRE branch is name-constrained to the trust domain with no usable
    DNS name or IP address, so SPIRE can never mint a certificate a Vault
    client would accept as Vault -- checked with real TLS handshakes;
  * `sign` applies the policy, never what a CSR asks for;
  * `install` and `check` refuse certificates from another root, for another
    key, or outside the policy, and `check` warns two months ahead.

Run (the test image has pytest, cryptography and OpenSSL 3):
    docker run --rm -v "$PWD/instraclustr:/infra:ro" -w /infra \
        nas-feedops-sentinel:test python -m pytest -p no:cacheprovider -q tests
Go (Vault, SPIRE) is what verifies these chains in production; the live
harness (scripts/smoke/) proves that end to end.
"""
from __future__ import annotations

import datetime as dt
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
from cryptography.x509.oid import NameOID
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError

INFRA = Path(__file__).resolve().parent.parent
PKI = INFRA / "scripts" / "pki.sh"
POLICY = INFRA / "certs" / "policy"
TRUST_DOMAIN = "sentinelfaa.internal"
PASSPHRASE = "throwaway-test-root-passphrase"

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")


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
    assert result.returncode == 2 and "wrong passphrase" in result.stderr
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
    assert result.stdout.count("OK ") == 2


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
