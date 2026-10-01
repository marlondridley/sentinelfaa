"""
Local TLS for the edge proxy and the identity service: `python -m src.devcert /tls`.

Creates a small local certificate authority and two server certificates
signed by it, one directory per container that uses them:

  edge-ca.crt            the authority's certificate -- trust this ONCE in the OS store
  edge-ca.key            the authority's key (owner-only; never leaves this machine)
  edge/edge.crt          nginx's certificate + CA chain, for localhost / 127.0.0.1
  edge/edge.key          its key (owner-only)
  keycloak/keycloak.crt  Keycloak's certificate + CA chain, for keycloak (the
                         compose service name the dashboard and API use),
                         localhost and 127.0.0.1 (the admin console on the
                         host, and the health check)
  keycloak/keycloak.key  its key: owner and group read, because Keycloak runs
                         as uid 1000 in group 0 and the file is created by root
  keycloak/edge-ca.crt   the authority's certificate, for its health check
  trust/edge-ca.crt      the authority's certificate, for the dashboard

Each directory is mounted whole, never file by file. `docker compose up`
creates every container before edge-certs has run, and Docker creates a
missing bind-mount source as an empty directory -- which for a file mount
leaves a directory where the key should be, and issuing then fails. A missing
directory mount is created as a directory, which is what it should be. Each
container also sees only what it needs: no container but edge-certs mounts
the authority's key.

The dashboard verifies Keycloak against edge-ca.crt (secrets.toml
[auth.keycloak.client_kwargs] verify), so identity traffic is TLS end to end,
including server to server.

Why an authority rather than a bare self-signed certificate: browsers reject a
self-signed server certificate (NET::ERR_CERT_AUTHORITY_INVALID) unless that
exact certificate is trusted, and every renewal would need trusting again.
With an authority, the operator trusts edge-ca.crt once; server certificates
it signs -- including renewals -- are trusted automatically.

    Import-Certificate -FilePath nas-feedops-sentinel\\tls\\edge-ca.crt `
        -CertStoreLocation Cert:\\CurrentUser\\Root

What trusting it vouches for: this stack's local names, nothing else. The
authority's certificate carries critical name constraints (RFC 5280 4.2.1.10)
and is for TLS servers only:

  permitted DNS  localhost, keycloak   the names its server certificates carry
                                       (and names under them, e.g. a.localhost)
  permitted IP   127.0.0.1             the address they carry
  excluded  IP   every IPv6 address    none is used
  usage          serverAuth            no client, code-signing or e-mail use

So whoever reads edge-ca.key can mint a certificate for these names and
nothing else: Windows, Chrome and Edge (112 and later), OpenSSL (nginx, the
dashboard) and Go (cloudflared) refuse one for any other name, and Windows
refuses one used to sign code. (Java, in the Keycloak container, does not
apply a trust anchor's constraints; its clients there only reach localhost
and 127.0.0.1.) This mirrors the SPIRE upstream CA's constraints
(../instraclustr/certs/policy/signing.cnf). A new name for a server
certificate needs a new authority: add it to both LEAVES and PERMITTED_DNS.

Idempotent: the authority is kept for its lifetime, unless its key is missing
or not its own, or its constraints are not exactly the ones above -- every
authority made before they existed is replaced, with a new key, and every
server certificate re-issued. Its thumbprint then changes: trust the new
edge-ca.crt once and remove the old one from the OS store (the launcher and
scripts/Setup-Keycloak.ps1 name it). A server certificate is re-issued when
missing, near expiry, issued for other names, or not signed by the current
authority's key. An authority with another name is the operator's own
(docs/runbooks.md, Certificates): it is published as it is, never replaced,
and its server certificates are theirs to renew.

This is for one machine. Anything shared needs a certificate from a real CA
(an internal PKI).
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import os
import sys
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_CERT, CA_KEY = "edge-ca.crt", "edge-ca.key"
CERT_NAME, KEY_NAME = "edge.crt", "edge.key"
KEYCLOAK_CERT, KEYCLOAK_KEY = "keycloak.crt", "keycloak.key"

# One directory per container, each mounted whole (see the module docstring).
EDGE_DIR, KEYCLOAK_DIR, TRUST_DIR = "edge", "keycloak", "trust"
MOUNT_DIRS = (EDGE_DIR, KEYCLOAK_DIR, TRUST_DIR)

# (directory, certificate file, key file, key file mode, DNS names). Every
# server certificate also carries LOOPBACK.
LOOPBACK = "127.0.0.1"
LEAVES = (
    (EDGE_DIR, CERT_NAME, KEY_NAME, 0o600, ("localhost",)),
    (KEYCLOAK_DIR, KEYCLOAK_CERT, KEYCLOAK_KEY, 0o640, ("keycloak", "localhost")),
)
# Where the authority's public certificate is published for verification.
CA_PUBLISHED_IN = (KEYCLOAK_DIR, TRUST_DIR)
# The earlier layout kept server certificates beside the authority, mounted
# file by file.
FLAT_LAYOUT = (CERT_NAME, KEY_NAME, KEYCLOAK_CERT, KEYCLOAK_KEY)
CA_VALID_DAYS = 3650
VALID_DAYS = 397          # browsers reject leaf certificates valid for longer
RENEW_WITHIN_DAYS = 30
ORG = "NAS FeedOps Sentinel (local)"
CA_NAME = x509.Name([
    x509.NameAttribute(NameOID.COMMON_NAME, "NAS FeedOps Sentinel Local CA"),
    x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORG),
])

# What the authority may sign for (the module docstring): exactly the names
# LEAVES carries, which tests/test_identity_tls.py holds the two to.
PERMITTED_DNS = ("localhost", "keycloak")
NAME_CONSTRAINTS = x509.NameConstraints(
    permitted_subtrees=[x509.DNSName(name) for name in PERMITTED_DNS]
    + [x509.IPAddress(ipaddress.ip_network(f"{LOOPBACK}/32"))],
    excluded_subtrees=[x509.IPAddress(ipaddress.ip_network("::/0"))],
)
CA_USAGE = x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH])


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _write_key(path: Path, key, mode: int = 0o600) -> None:
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    os.chmod(path, mode)


def _load_cert(path: Path) -> x509.Certificate | None:
    try:
        return x509.load_pem_x509_certificate(path.read_bytes())
    except (OSError, ValueError):
        return None


def _load_key(path: Path):
    try:
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError):
        return None


def _public_der(public_key) -> bytes:
    return public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def _is_constrained(ca: x509.Certificate) -> bool:
    """True if the authority can sign for exactly this stack's names, and for TLS servers only."""
    try:
        constraints = ca.extensions.get_extension_for_class(x509.NameConstraints)
        usage = ca.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound:
        return False
    return constraints.critical and constraints.value == NAME_CONSTRAINTS and usage == CA_USAGE


def _ca_problem(ca: x509.Certificate, key) -> str | None:
    """Why this module's authority must be replaced, or None to keep it."""
    if key is None or _public_der(key.public_key()) != _public_der(ca.public_key()):
        return "its key is missing or is not the key of its certificate"
    if ca.not_valid_after_utc - _now() <= dt.timedelta(days=RENEW_WITHIN_DAYS):
        return "it expires within 30 days"
    if not _is_constrained(ca):
        return "it was not limited to this stack's names (localhost, keycloak, 127.0.0.1)"
    return None


def _ensure_ca(directory: Path) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey | None, bool]:
    """The authority, its key (None for the operator's own authority), and whether it is new."""
    cert, key = _load_cert(directory / CA_CERT), _load_key(directory / CA_KEY)
    if cert is not None and cert.subject != CA_NAME:
        return cert, None, False              # an authority of the operator's (docs/runbooks.md)
    problem = "there was none" if cert is None else _ca_problem(cert, key)
    if problem is None:
        return cert, key, False
    old = cert

    key = ec.generate_private_key(ec.SECP256R1())
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (
        x509.CertificateBuilder()
        .subject_name(CA_NAME).issuer_name(CA_NAME)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - dt.timedelta(minutes=5))
        .not_valid_after(_now() + dt.timedelta(days=CA_VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
            key_encipherment=False, data_encipherment=False, key_agreement=False,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(NAME_CONSTRAINTS, critical=True)
        .add_extension(CA_USAGE, critical=False)
        .add_extension(ski, critical=False)
        .sign(key, hashes.SHA256())
    )
    # Written over the old key: nothing signs with it after this.
    _write_key(directory / CA_KEY, key)
    (directory / CA_CERT).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    if old is not None:
        print(f"replaced the local authority ({problem}); every server certificate is re-issued. "
              f"Trust the new {CA_CERT} once and remove the old one from the OS store "
              "(docs/runbooks.md, Certificates).")
    return cert, key, True


def _leaf_is_current(directory: Path, ca: x509.Certificate, cert_name: str, key_name: str,
                     dns_names: tuple[str, ...]) -> bool:
    leaf = _load_cert(directory / cert_name)
    if leaf is None or not (directory / key_name).exists():
        return False
    try:
        leaf.verify_directly_issued_by(ca)
    except (ValueError, TypeError, InvalidSignature):
        # The old self-signed certificate, or one from a replaced authority:
        # that has the current authority's name, but not its key.
        return False
    try:
        names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return False
    if set(names.get_values_for_type(x509.DNSName)) != set(dns_names):
        return False                          # issued for a different set of names
    return leaf.not_valid_after_utc - _now() > dt.timedelta(days=RENEW_WITHIN_DAYS)


def ensure_certificate(directory: Path) -> bool:
    """Ensure the authority and current server certificates exist. True if anything was written."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in MOUNT_DIRS:
        (directory / name).mkdir(exist_ok=True)
    ca, ca_key, ca_new = _ensure_ca(directory)
    wrote = ca_new
    for sub, cert_name, key_name, key_mode, dns_names in LEAVES:
        if ca_new or not _leaf_is_current(directory / sub, ca, cert_name, key_name, dns_names):
            _issue_leaf(directory / sub, ca, ca_key, cert_name, key_name, key_mode, dns_names)
            wrote = True
    ca_pem = (directory / CA_CERT).read_bytes()
    for sub in CA_PUBLISHED_IN:
        published = directory / sub / CA_CERT
        if not published.is_file() or published.read_bytes() != ca_pem:
            published.write_bytes(ca_pem)
            wrote = True
    _remove_flat_layout(directory)
    return wrote


def _remove_flat_layout(directory: Path) -> None:
    """Remove the earlier layout's server files, and the empty directories Docker made for them."""
    for name in FLAT_LAYOUT:
        path = directory / name
        try:
            if path.is_dir():
                path.rmdir()          # only if empty: anything else is not ours to delete
            elif path.exists():
                path.unlink()
        except OSError as exc:
            print(f"left {path} in place: {exc}", file=sys.stderr)


def _issue_leaf(directory: Path, ca: x509.Certificate, ca_key, cert_name: str, key_name: str,
                key_mode: int, dns_names: tuple[str, ...]) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, dns_names[0]),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORG),
        ]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - dt.timedelta(minutes=5))
        .not_valid_after(_now() + dt.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(
            [x509.DNSName(name) for name in dns_names]
            + [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
        ), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_cert_sign=False, crl_sign=False, content_commitment=False,
            key_encipherment=False, data_encipherment=False, key_agreement=True,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write_key(directory / key_name, key, key_mode)
    # Server certificate followed by the authority: the full chain served.
    (directory / cert_name).write_bytes(
        leaf.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM))


def main() -> None:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "/tls")
    wrote = ensure_certificate(target)
    print(f"{'issued' if wrote else 'kept existing'} {target / EDGE_DIR / CERT_NAME} and "
          f"{target / KEYCLOAK_DIR / KEYCLOAK_CERT} "
          f"(signed by {target / CA_CERT}; trust that once in the OS store)")


if __name__ == "__main__":
    main()
