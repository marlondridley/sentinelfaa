"""
Local TLS for the edge proxy: `python -m src.devcert /tls`.

Creates a small local certificate authority and a server certificate for
localhost / 127.0.0.1 signed by it:

  edge-ca.crt   the authority's certificate -- trust this ONCE in the OS store
  edge-ca.key   the authority's key (owner-only; never leaves this machine)
  edge.crt      server certificate + CA chain, served by nginx
  edge.key      server key (owner-only)

Why an authority rather than a bare self-signed certificate: browsers reject a
self-signed server certificate (NET::ERR_CERT_AUTHORITY_INVALID) unless that
exact certificate is trusted, and every renewal would need trusting again.
With an authority, the operator trusts edge-ca.crt once; server certificates
it signs -- including renewals -- are trusted automatically.

    Import-Certificate -FilePath nas-feedops-sentinel\\tls\\edge-ca.crt `
        -CertStoreLocation Cert:\\CurrentUser\\Root

Idempotent: the authority is kept for its lifetime; the server certificate is
re-issued only when missing, near expiry, or not signed by the current
authority. This is for one machine. Anything shared needs a certificate from a
real CA (an internal PKI).
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import os
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_CERT, CA_KEY = "edge-ca.crt", "edge-ca.key"
CERT_NAME, KEY_NAME = "edge.crt", "edge.key"
CA_VALID_DAYS = 3650
VALID_DAYS = 397          # browsers reject leaf certificates valid for longer
RENEW_WITHIN_DAYS = 30
ORG = "NAS FeedOps Sentinel (local)"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _write_key(path: Path, key) -> None:
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    os.chmod(path, 0o600)


def _load_cert(path: Path) -> x509.Certificate | None:
    try:
        return x509.load_pem_x509_certificate(path.read_bytes())
    except (OSError, ValueError):
        return None


def _ensure_ca(directory: Path) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey, bool]:
    cert, key_path = _load_cert(directory / CA_CERT), directory / CA_KEY
    if cert and key_path.exists() and cert.not_valid_after_utc - _now() > dt.timedelta(days=RENEW_WITHIN_DAYS):
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        return cert, key, False

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "NAS FeedOps Sentinel Local CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORG),
    ])
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - dt.timedelta(minutes=5))
        .not_valid_after(_now() + dt.timedelta(days=CA_VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
            key_encipherment=False, data_encipherment=False, key_agreement=False,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(ski, critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_key(key_path, key)
    (directory / CA_CERT).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert, key, True


def _leaf_is_current(directory: Path, ca: x509.Certificate) -> bool:
    leaf = _load_cert(directory / CERT_NAME)
    if leaf is None or not (directory / KEY_NAME).exists():
        return False
    if leaf.issuer != ca.subject:
        return False                          # e.g. the old self-signed certificate
    return leaf.not_valid_after_utc - _now() > dt.timedelta(days=RENEW_WITHIN_DAYS)


def ensure_certificate(directory: Path) -> bool:
    """Ensure the authority and a current server certificate exist. True if anything was written."""
    directory.mkdir(parents=True, exist_ok=True)
    ca, ca_key, ca_new = _ensure_ca(directory)
    if not ca_new and _leaf_is_current(directory, ca):
        return False

    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORG),
        ]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - dt.timedelta(minutes=5))
        .not_valid_after(_now() + dt.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_cert_sign=False, crl_sign=False, content_commitment=False,
            key_encipherment=False, data_encipherment=False, key_agreement=True,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write_key(directory / KEY_NAME, key)
    # Server certificate followed by the authority: the full chain nginx serves.
    (directory / CERT_NAME).write_bytes(
        leaf.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM))
    return True


def main() -> None:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "/tls")
    wrote = ensure_certificate(target)
    print(f"{'issued' if wrote else 'kept existing'} {target / CERT_NAME} "
          f"(signed by {target / CA_CERT}; trust that once in the OS store)")


if __name__ == "__main__":
    main()
