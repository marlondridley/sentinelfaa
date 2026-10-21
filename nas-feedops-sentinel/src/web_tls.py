"""
TLS for the edge proxy and the identity service, from the web TLS branch of
the offline root: `python -m src.web_tls <web-ca dir> <trust dir> <tls dir>`.

The web TLS CA (instraclustr/certs/web/web-ca.crt and web-ca.key) comes from
the root ceremony (instraclustr/certs/README.md), so the root's key never
touches this host. Its name constraints let it issue for localhost, keycloak
and 127.0.0.1 only, and for TLS servers only
(instraclustr/certs/policy/signing.cnf, web_tls_ext). This signs two server
certificates with it, one directory per container that uses them:

  edge/edge.crt          nginx's certificate + the web TLS CA, for localhost
                         and 127.0.0.1
  edge/edge.key          its key (owner-only)
  keycloak/keycloak.crt  Keycloak's certificate + the web TLS CA, for keycloak
                         (the compose service name the dashboard and the edge
                         use), localhost and 127.0.0.1 (the admin console on
                         the host, and the health check)
  keycloak/keycloak.key  its key: owner and group read, because Keycloak runs
                         as uid 1000 in group 0 and the file is created by root

Every client trusts the root alone (instraclustr/certs/trust/root-ca.crt,
mounted read-only), so a new leaf or a new web TLS CA needs nothing trusted
again.

Each directory is mounted whole, never file by file. `docker compose up`
creates every container before edge-certs has run, and Docker creates a
missing bind-mount source as an empty directory -- which for a file mount
leaves a directory where the key should be, and issuing then fails. A missing
directory mount is created as a directory, which is what it should be.

Before anything under <tls dir> is created or changed, the web TLS CA must be
present and readable, in date, signed by the root, matched by its key, and
carry the web policy exactly. Otherwise this refuses, names the ceremony to
run, and issues nothing: there is no other way to get a signing key.

A leaf is kept while it was issued by the current web TLS CA for the right
names, with its own key and its chain, and has more than 30 days left;
otherwise it is re-issued. Running containers keep the certificates they
loaded at start, so after a run that issued, recreate edge, keycloak,
dashboard and cloudflared (docs/runbooks.md, "Certificates").
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import os
import sys
from pathlib import Path
from typing import TypeVar

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

WEB_CA_CERT, WEB_CA_KEY, ROOT_CERT = "web-ca.crt", "web-ca.key", "root-ca.crt"
EDGE_DIR, KEYCLOAK_DIR = "edge", "keycloak"

# One directory per container, each mounted whole (see the module docstring):
# (directory, certificate file, key file, key file mode, DNS names). Every
# server certificate also carries 127.0.0.1.
LEAVES = (
    (EDGE_DIR, "edge.crt", "edge.key", 0o600, ("localhost",)),
    # Group-readable: Keycloak runs as uid 1000 in group 0, and root writes the key.
    (KEYCLOAK_DIR, "keycloak.crt", "keycloak.key", 0o640, ("keycloak", "localhost")),
)
LEAF_IP = ipaddress.ip_address("127.0.0.1")
VALID_DAYS = 397          # browsers reject leaf certificates valid for longer
RENEW_WITHIN_DAYS = 30
ORG = "sentinelfaa"

# The web policy (signing.cnf, web_tls_ext). Name constraints are compared as
# sets: their order carries no meaning.
PERMITTED = {
    x509.DNSName("localhost"),
    x509.DNSName("keycloak"),
    x509.IPAddress(ipaddress.ip_network("127.0.0.1/32")),
    x509.UniformResourceIdentifier("invalid"),
}
EXCLUDED = {x509.IPAddress(ipaddress.ip_network("::/0"))}
KEY_USAGES = ("digital_signature", "content_commitment", "key_encipherment", "data_encipherment",
              "key_agreement", "key_cert_sign", "crl_sign")

CEREMONY = (r"Run the ceremony (instraclustr/certs/README.md): .\instraclustr\scripts\pki.ps1 request, then "
            r".\instraclustr\scripts\pki.ps1 sign -RootDir <root USB folder>, then "
            r".\instraclustr\scripts\pki.ps1 install.")

E = TypeVar("E", bound=x509.ExtensionType)


class WebCaRefused(Exception):
    """The web TLS CA cannot be used, so nothing was issued."""

    def __init__(self, reason: str, path: Path) -> None:
        super().__init__(f"{reason}: {path}. Nothing was issued. {CEREMONY}")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise WebCaRefused(f"missing or unreadable ({exc.strerror})", path) from exc


def _public_der(key) -> bytes:
    return key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def _extension(cert: x509.Certificate, kind: type[E]) -> x509.Extension[E] | None:
    try:
        return cert.extensions.get_extension_for_class(kind)
    except x509.ExtensionNotFound:
        return None


def _policy_differences(ca: x509.Certificate) -> list[str]:
    """What in the web TLS CA differs from the web policy; empty when it carries it exactly."""
    basic = _extension(ca, x509.BasicConstraints)
    usage = _extension(ca, x509.KeyUsage)
    purpose = _extension(ca, x509.ExtendedKeyUsage)
    names = _extension(ca, x509.NameConstraints)
    wrong = []
    if not (basic and basic.critical and basic.value.ca and basic.value.path_length == 0):
        wrong.append("basic constraints (critical, CA, path length 0)")
    if not (usage and usage.critical
            and {name for name in KEY_USAGES if getattr(usage.value, name)} == {"key_cert_sign", "crl_sign"}):
        wrong.append("key usage (critical, certificate and CRL signing only)")
    if not (purpose and list(purpose.value) == [ExtendedKeyUsageOID.SERVER_AUTH]):
        wrong.append("extended key usage (TLS server only)")
    if not (names and names.critical
            and set(names.value.permitted_subtrees or ()) == PERMITTED
            and set(names.value.excluded_subtrees or ()) == EXCLUDED):
        wrong.append("name constraints (critical; permitted DNS localhost, DNS keycloak, IP 127.0.0.1/32, "
                     "URI invalid; excluded IP ::/0)")
    return wrong


def _load_web_ca(web_ca_dir: Path, trust_dir: Path) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    """The web TLS CA and its key, or WebCaRefused saying why not."""
    cert_path, key_path, root_path = web_ca_dir / WEB_CA_CERT, web_ca_dir / WEB_CA_KEY, trust_dir / ROOT_CERT
    files = {path: _read(path) for path in (cert_path, key_path, root_path)}
    try:
        web_ca = x509.load_pem_x509_certificate(files[cert_path])
    except ValueError as exc:
        raise WebCaRefused("not a PEM certificate", cert_path) from exc
    try:
        root = x509.load_pem_x509_certificate(files[root_path])
    except ValueError as exc:
        raise WebCaRefused("not a PEM certificate", root_path) from exc
    try:
        key = serialization.load_pem_private_key(files[key_path], password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise WebCaRefused("not an unencrypted PEM private key", key_path) from exc

    now = _now()
    for cert, path in ((root, root_path), (web_ca, cert_path)):
        if not cert.not_valid_before_utc <= now <= cert.not_valid_after_utc:
            raise WebCaRefused(f"outside its validity period ({cert.not_valid_before_utc:%Y-%m-%d} to "
                               f"{cert.not_valid_after_utc:%Y-%m-%d}: expired or not yet valid)", path)
    try:
        web_ca.verify_directly_issued_by(root)
    except (ValueError, TypeError, InvalidSignature) as exc:
        raise WebCaRefused(f"not signed by the root in {root_path}", cert_path) from exc
    if not (isinstance(key, ec.EllipticCurvePrivateKey)
            and _public_der(key.public_key()) == _public_der(web_ca.public_key())):
        raise WebCaRefused(f"not the key of {cert_path}", key_path)
    wrong = _policy_differences(web_ca)
    if wrong:
        raise WebCaRefused(f"does not carry the web TLS policy (signing.cnf, web_tls_ext): {'; '.join(wrong)}",
                           cert_path)
    return web_ca, key


def _write_key(path: Path, key: ec.EllipticCurvePrivateKey, mode: int) -> None:
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    os.chmod(path, mode)


def _leaf_is_current(directory: Path, web_ca: x509.Certificate, cert_name: str, key_name: str,
                     dns_names: tuple[str, ...]) -> bool:
    try:
        chain = x509.load_pem_x509_certificates((directory / cert_name).read_bytes())
        key = serialization.load_pem_private_key((directory / key_name).read_bytes(), password=None)
        leaf = chain[0]
        # A renewed web TLS CA always has a new key, so its leaves fail here and
        # are re-issued; so does a leaf from any other authority.
        leaf.verify_directly_issued_by(web_ca)
        names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except (OSError, ValueError, TypeError, UnsupportedAlgorithm, InvalidSignature, x509.ExtensionNotFound):
        return False
    return (chain[1:] == [web_ca]
            and _public_der(key.public_key()) == _public_der(leaf.public_key())
            and set(names.get_values_for_type(x509.DNSName)) == set(dns_names)
            and set(names.get_values_for_type(x509.IPAddress)) == {LEAF_IP}
            and leaf.not_valid_after_utc - _now() > dt.timedelta(days=RENEW_WITHIN_DAYS))


def _issue_leaf(directory: Path, web_ca: x509.Certificate, ca_key: ec.EllipticCurvePrivateKey, cert_name: str,
                key_name: str, key_mode: int, dns_names: tuple[str, ...]) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    now = _now()
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, dns_names[0]),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORG),
        ]))
        .issuer_name(web_ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(
            [x509.DNSName(name) for name in dns_names] + [x509.IPAddress(LEAF_IP)]
        ), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_cert_sign=False, crl_sign=False, content_commitment=False,
            key_encipherment=False, data_encipherment=False, key_agreement=True,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA384())
    )
    _write_key(directory / key_name, key, key_mode)
    # Server certificate followed by the web TLS CA, never the root: the chain
    # served, which every client completes with the root it already holds.
    (directory / cert_name).write_bytes(
        leaf.public_bytes(serialization.Encoding.PEM) + web_ca.public_bytes(serialization.Encoding.PEM))


def ensure_certificates(web_ca_dir: Path, trust_dir: Path, tls_dir: Path) -> bool:
    """Issue the edge and Keycloak certificates that are missing or due. True if anything was written."""
    web_ca, ca_key = _load_web_ca(web_ca_dir, trust_dir)     # refuses before tls_dir is touched
    wrote = False
    for sub, cert_name, key_name, key_mode, dns_names in LEAVES:
        directory = tls_dir / sub
        directory.mkdir(parents=True, exist_ok=True)
        if not _leaf_is_current(directory, web_ca, cert_name, key_name, dns_names):
            _issue_leaf(directory, web_ca, ca_key, cert_name, key_name, key_mode, dns_names)
            wrote = True
    return wrote


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        print("usage: python -m src.web_tls <web-ca dir> <trust dir> <tls dir>", file=sys.stderr)
        return 2
    web_ca_dir, trust_dir, tls_dir = (Path(arg) for arg in args)
    try:
        wrote = ensure_certificates(web_ca_dir, trust_dir, tls_dir)
    except WebCaRefused as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    leaves = " and ".join(str(tls_dir / sub / cert_name) for sub, cert_name, *_ in LEAVES)
    if wrote:
        print(f"issued {leaves} (signed by {web_ca_dir / WEB_CA_CERT}). Running containers keep the old "
              "certificates: recreate edge, keycloak, dashboard and cloudflared (docs/runbooks.md, \"Certificates\")")
    else:
        print(f"kept existing {leaves} (signed by {web_ca_dir / WEB_CA_CERT})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
