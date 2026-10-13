"""Local HTTPS certificates.

``python -m app.cli make-certs`` creates a private certificate authority (once) and a server
certificate it signs for ``localhost``, ``127.0.0.1``, ``::1`` and any extra names given. Trust the
CA once on the machine (``ca.crt``) and browsers accept every server certificate it issues.
Re-running keeps the CA and re-issues the server certificate, so adding a name never means
trusting a new CA.

Files (default directory ``certs/``, git-ignored):
    ca.crt / ca.key          the authority — keep ca.key private
    server.crt / server.key  what uvicorn serves (--ssl-certfile / --ssl-keyfile)
"""

from __future__ import annotations

import contextlib
import datetime as dt
import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DEFAULT_DIR = Path("certs")
DEFAULT_NAMES = ("localhost", "127.0.0.1", "::1")
CA_DAYS = 3650
# Browsers reject leaf certificates valid for more than 398 days.
SERVER_DAYS = 397


@dataclass
class CertPaths:
    ca_cert: Path
    ca_key: Path
    server_cert: Path
    server_key: Path
    ca_created: bool


def _new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _write_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    with contextlib.suppress(OSError):  # Windows: ACLs, not mode bits
        os.chmod(path, 0o600)


def _san(names: list[str]) -> x509.SubjectAlternativeName:
    entries: list[x509.GeneralName] = []
    for name in names:
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            entries.append(x509.DNSName(name))
    return x509.SubjectAlternativeName(entries)


def _load_or_create_ca(
    directory: Path,
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey, bool]:
    cert_path, key_path = directory / "ca.crt", directory / "ca.key"
    if cert_path.exists() and key_path.exists():
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        assert isinstance(key, ec.EllipticCurvePrivateKey)
        return cert, key, False
    key = _new_key()
    now = dt.datetime.now(dt.UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "API Tools Local CA"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "API Tools (local)")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                     content_commitment=False, key_encipherment=False,
                                     data_encipherment=False, key_agreement=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_key(key_path, key)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert, key, True


def make_certs(directory: Path = DEFAULT_DIR, extra_names: list[str] | None = None) -> CertPaths:
    """Create the CA if missing, then (re)issue the server cert for localhost + ``extra_names``."""
    directory.mkdir(parents=True, exist_ok=True)
    ca_cert, ca_key, ca_created = _load_or_create_ca(directory)
    names = list(dict.fromkeys([*DEFAULT_NAMES, *(extra_names or [])]))
    key = _new_key()
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=SERVER_DAYS))
        .add_extension(_san(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False,
                                     key_cert_sign=False, crl_sign=False, content_commitment=False,
                                     data_encipherment=False, key_agreement=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                       critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    server_cert, server_key = directory / "server.crt", directory / "server.key"
    _write_key(server_key, key)
    # Full chain, so clients that don't have the CA cached can still build the path.
    server_cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM)
                            + ca_cert.public_bytes(serialization.Encoding.PEM))
    return CertPaths(directory / "ca.crt", directory / "ca.key", server_cert, server_key,
                     ca_created)
