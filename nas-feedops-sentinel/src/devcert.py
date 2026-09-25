"""
Local TLS certificate for the edge proxy: `python -m src.devcert /tls`.

Writes a self-signed certificate and key for localhost / 127.0.0.1 into the
given directory, once: an existing, unexpired pair is left alone so a restart
does not invalidate a certificate the operator has already trusted. The key is
created with owner-only permissions.

This is for a single-machine deployment. Anything shared needs a certificate
from a real CA (an internal PKI, or mkcert for a developer's machine).
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

CERT_NAME, KEY_NAME = "edge.crt", "edge.key"
VALID_DAYS = 397          # browsers reject leaf certificates valid for longer
RENEW_WITHIN_DAYS = 30


def _still_valid(cert_path: Path) -> bool:
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (OSError, ValueError):
        return False
    return cert.not_valid_after_utc - dt.datetime.now(dt.timezone.utc) > dt.timedelta(days=RENEW_WITHIN_DAYS)


def ensure_certificate(directory: Path) -> bool:
    """Create the pair if missing or near expiry. Returns True if it wrote one."""
    directory.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = directory / CERT_NAME, directory / KEY_NAME
    if key_path.exists() and _still_valid(cert_path):
        return False

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "NAS FeedOps Sentinel (local)"),
    ])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    os.chmod(key_path, 0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return True


def main() -> None:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "/tls")
    wrote = ensure_certificate(target)
    print(f"{'created' if wrote else 'kept existing'} {target / CERT_NAME}")


if __name__ == "__main__":
    main()
