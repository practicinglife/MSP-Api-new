"""HTTPS certificate for this server: Let's Encrypt (DNS-01, a TXT record you add) or one you upload.

Let's Encrypt, in three calls (the order is kept in memory between them):

1. :func:`acme_start` — account (key kept in ``data/tls/``), order for the domain, and the TXT record to add:
   ``_acme-challenge.<domain>`` = ``<value>``. DNS-01 works for internal servers the internet can't reach.
2. :func:`dns_check` — is the record visible yet (asked of public DNS-over-HTTPS resolvers)?
3. :func:`acme_finish` — tell Let's Encrypt to check, wait for it, download the certificate, install it.

Or :func:`install_uploaded` takes a certificate + key (PEM) or a .pfx from any authority.

Installed certificates live in ``data/tls/`` and are recorded in ``data/settings.json`` (``tls``); the local runner
serves them (and the address becomes ``https://<domain>``) after the in-place restart that installing asks for.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import structlog
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtensionOID, NameOID

from app.runtime import ALL_NETWORKS, THIS_COMPUTER, data_dir, load_settings, save_settings

log = structlog.get_logger(__name__)

DIRECTORIES = {
    "production": "https://acme-v02.api.letsencrypt.org/directory",
    "staging": "https://acme-staging-v02.api.letsencrypt.org/directory",
}
TERMS_URL = "https://letsencrypt.org/repository/"
DOH_RESOLVERS = ("https://cloudflare-dns.com/dns-query", "https://dns.google/resolve")
RENEW_DAYS = 30


class TlsError(ValueError):
    """Something the person can fix: a bad domain, a missing DNS record, a key that doesn't match …"""


def tls_dir() -> Path:
    path = data_dir() / "tls"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ── What's installed ─────────────────────────────────────────────────────────────────────────────────────────


def describe(cert: x509.Certificate) -> dict[str, Any]:
    try:
        names = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
        dns_names = names.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        dns_names = []
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    issuer = cert.issuer.get_attributes_for_oid(NameOID.ORGANIZATION_NAME) or \
        cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
    expires = cert.not_valid_after_utc
    return {"names": dns_names or [c.value for c in cn], "issuer": issuer[0].value if issuer else "",
            "expires": expires.isoformat(), "days_left": (expires - dt.datetime.now(dt.UTC)).days}


def status() -> dict[str, Any]:
    """The certificate the local runner serves, where it came from, and when it needs renewing."""
    tls = load_settings().get("tls") or {}
    path = Path(tls["cert"]) if tls.get("cert") else Path("certs") / "server.crt"
    source = tls.get("source") or "local"
    try:
        info = describe(x509.load_pem_x509_certificate(path.read_bytes()))
    except (OSError, ValueError):
        return {"source": source, "installed": False}
    return {"source": source, "installed": True, "domain": tls.get("domain", ""), **info,
            "renew_soon": source != "local" and info["days_left"] <= RENEW_DAYS}


def _install(domain: str, cert_pem: bytes, key_pem: bytes, source: str, listen_on_network: bool) -> dict[str, Any]:
    folder = tls_dir()
    cert_path, key_path = folder / "server.crt", folder / "server.key"
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(key_pem)
    try:
        key_path.chmod(0o600)
    except OSError:
        pass
    info = describe(x509.load_pem_x509_certificate(cert_pem))
    port = int(load_settings().get("port") or 443)
    save_settings(tls={"source": source, "domain": domain, "cert": str(cert_path), "key": str(key_path),
                       "expires": info["expires"]},
                  external_url=f"https://{domain}" + ("" if port == 443 else f":{port}"),
                  listen=ALL_NETWORKS if listen_on_network else THIS_COMPUTER)
    log.info("tls.installed", domain=domain, source=source, expires=info["expires"])
    return {"domain": domain, "source": source, **info}


# ── Upload (any authority) ───────────────────────────────────────────────────────────────────────────────────


def install_uploaded(*, cert_pem: str = "", key_pem: str = "", pfx_b64: str = "", pfx_password: str = "",
                     domain: str = "", listen_on_network: bool = True) -> dict[str, Any]:
    """Install a certificate + private key (PEM, the chain after the certificate) or a .pfx/.p12."""
    if pfx_b64:
        try:
            key, cert, extra = pkcs12.load_key_and_certificates(base64.b64decode(pfx_b64),
                                                                pfx_password.encode() or None)
        except ValueError as exc:
            raise TlsError("Couldn't open the .pfx: wrong password, or not a PKCS#12 file") from exc
        if key is None or cert is None:
            raise TlsError("The .pfx has no private key or no certificate")
        chain = [cert, *(extra or [])]
    else:
        try:
            chain = x509.load_pem_x509_certificates(cert_pem.encode())
            key = serialization.load_pem_private_key(key_pem.encode(), password=None)
        except ValueError as exc:
            raise TlsError("Couldn't read the certificate or key: paste PEM text "
                           "(-----BEGIN CERTIFICATE----- / -----BEGIN PRIVATE KEY-----)") from exc
        cert = chain[0]
    pub = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if pub != cert.public_key().public_bytes(serialization.Encoding.DER,
                                             serialization.PublicFormat.SubjectPublicKeyInfo):
        raise TlsError("The private key doesn't belong to this certificate")
    info = describe(cert)
    if info["days_left"] < 0:
        raise TlsError(f"This certificate expired on {info['expires'][:10]}")
    domain = (domain or (info["names"][0] if info["names"] else "")).strip().lower()
    if not domain:
        raise TlsError("Say which address this certificate is for")
    if not any(_covers(n, domain) for n in info["names"]):
        raise TlsError(f"The certificate is for {', '.join(info['names'])}, not {domain}")
    cert_out = b"".join(c.public_bytes(serialization.Encoding.PEM) for c in chain)
    key_out = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return _install(domain, cert_out, key_out, "uploaded", listen_on_network)


def _covers(name: str, domain: str) -> bool:
    name = name.lower()
    if name.startswith("*."):
        return domain.endswith(name[1:]) and domain.count(".") == name.count(".")
    return name == domain


# ── Let's Encrypt (DNS-01) ───────────────────────────────────────────────────────────────────────────────────


@dataclass
class _Pending:
    domain: str
    environment: str
    record_name: str
    record_value: str
    key_pem: bytes
    started: float = field(default_factory=time.time)
    order: Any = None
    challenge: Any = None
    client: Any = None
    account_key: Any = None
    listen_on_network: bool = True


_pending: dict[str, _Pending] = {}


def _account_key(environment: str) -> Any:
    import josepy as jose

    path = tls_dir() / f"acme-account-{environment}.json"
    if path.exists():
        return jose.JWKRSA.json_loads(path.read_text(encoding="utf-8"))
    key = jose.JWKRSA(key=rsa.generate_private_key(public_exponent=65537, key_size=2048))
    path.write_text(key.json_dumps(), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


def _client(environment: str, email: str) -> tuple[Any, Any]:
    from acme import client, errors, messages

    key = _account_key(environment)
    net = client.ClientNetwork(key, user_agent="api-tools")
    directory = client.ClientV2.get_directory(DIRECTORIES[environment], net)
    acme = client.ClientV2(directory, net=net)
    try:
        regr = acme.new_account(messages.NewRegistration.from_data(email=email or None,
                                                                    terms_of_service_agreed=True))
    except errors.ConflictError as exc:  # this key already has an account: use it
        regr = acme.query_registration(messages.RegistrationResource(uri=exc.location, body=messages.Registration()))
    net.account = regr
    return acme, key


def _start_sync(domain: str, email: str, environment: str, listen_on_network: bool) -> dict[str, Any]:
    from acme import challenges, crypto_util

    acme, account_key = _client(environment, email)
    key = ec.generate_private_key(ec.SECP256R1())
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    order = acme.new_order(crypto_util.make_csr(key_pem, [domain]))
    for authz in order.authorizations:
        for chall in authz.body.challenges:
            if isinstance(chall.chall, challenges.DNS01):
                pending = _Pending(domain=domain, environment=environment,
                                   record_name=chall.chall.validation_domain_name(domain),
                                   record_value=chall.chall.validation(account_key), key_pem=key_pem, order=order,
                                   challenge=chall, client=acme, account_key=account_key,
                                   listen_on_network=listen_on_network)
                _pending[domain] = pending
                return {"domain": domain, "record_type": "TXT", "record_name": pending.record_name,
                        "record_value": pending.record_value, "environment": environment}
    raise TlsError("Let's Encrypt didn't offer a DNS challenge for this domain")


def _check_domain(domain: str) -> str:
    domain = domain.strip().lower().rstrip(".")
    if not domain or "." not in domain or "*" in domain or "/" in domain or ":" in domain:
        raise TlsError("Enter the server's full name, e.g. tools.yourcompany.com")
    if domain.endswith((".local", ".localhost", ".lan", ".internal", ".test")) or domain == "localhost":
        raise TlsError(f"Let's Encrypt only issues certificates for public domain names, not {domain}. "
                       "Use a name under a domain you own (it can point to an internal address), or upload a "
                       "certificate from your own authority.")
    return domain


async def acme_start(domain: str, email: str, *, agree: bool, staging: bool = False,
                     listen_on_network: bool = True) -> dict[str, Any]:
    if not agree:
        raise TlsError(f"Accept the Let's Encrypt Subscriber Agreement first ({TERMS_URL})")
    domain = _check_domain(domain)
    try:
        return await asyncio.to_thread(_start_sync, domain, email.strip(), "staging" if staging else "production",
                                       listen_on_network)
    except TlsError:
        raise
    except Exception as exc:  # noqa: BLE001 — ACME errors are shown to the person as they are
        raise TlsError(f"Let's Encrypt said: {exc}") from exc


async def dns_check(domain: str) -> dict[str, Any]:
    """Ask public resolvers whether the TXT record is there yet."""
    pending = _pending.get(domain.strip().lower())
    if pending is None:
        raise TlsError("Start again: no certificate request is waiting for this domain")
    seen: set[str] = set()
    async with httpx.AsyncClient(timeout=10) as client:
        for resolver in DOH_RESOLVERS:
            try:
                r = await client.get(resolver, params={"name": pending.record_name, "type": "TXT"},
                                     headers={"accept": "application/dns-json"})
                for answer in r.json().get("Answer") or []:
                    seen.add(str(answer.get("data", "")).strip('"'))
            except (httpx.HTTPError, ValueError):
                continue
    return {"record_name": pending.record_name, "expected": pending.record_value,
            "found": sorted(seen), "ready": pending.record_value in seen}


def _finish_sync(pending: _Pending) -> bytes:
    deadline = dt.datetime.now() + dt.timedelta(seconds=120)
    pending.client.answer_challenge(pending.challenge, pending.challenge.chall.response(pending.account_key))
    order = pending.client.poll_and_finalize(pending.order, deadline=deadline)
    return order.fullchain_pem.encode()


async def acme_finish(domain: str) -> dict[str, Any]:
    pending = _pending.get(domain.strip().lower())
    if pending is None:
        raise TlsError("Start again: no certificate request is waiting for this domain")
    try:
        fullchain = await asyncio.to_thread(_finish_sync, pending)
    except Exception as exc:  # noqa: BLE001
        raise TlsError(f"Let's Encrypt couldn't verify the record yet: {exc}. "
                       "Check it with 'Check the record', wait a few minutes for DNS, and try again.") from exc
    _pending.pop(pending.domain, None)
    source = "letsencrypt" if pending.environment == "production" else "letsencrypt-staging"
    return _install(pending.domain, fullchain, pending.key_pem, source, pending.listen_on_network)


def _fingerprint(cert_pem: bytes) -> str:
    return x509.load_pem_x509_certificate(cert_pem).fingerprint(hashes.SHA256()).hex()


def pending_requests() -> list[dict[str, Any]]:
    return [{"domain": p.domain, "record_name": p.record_name, "record_value": p.record_value,
             "environment": p.environment, "started": p.started} for p in _pending.values()]


__all__ = ["TlsError", "acme_finish", "acme_start", "dns_check", "install_uploaded", "pending_requests", "status",
           "json"]
