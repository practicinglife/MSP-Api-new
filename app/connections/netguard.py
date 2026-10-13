"""Outbound-request guard (SSRF): connections may point anywhere an admin chooses — internet APIs and on-prem
servers alike — but never at cloud metadata / link-local addresses, unspecified or multicast addresses.

``MSP_BLOCK_PRIVATE_TARGETS=1`` additionally refuses private and loopback ranges (for internet-only installs).
Host names are resolved and every address checked, so a DNS name pointing at 169.254.169.254 is refused too.
``MSP_NETGUARD_RESOLVE=0`` skips resolution (only for offline test runs).
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

from app.connections.base import CallError

_ALLOWED_SCHEMES = {"http", "https"}


def _bad_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if ip.is_link_local:
        return "link-local / cloud metadata address"
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return "unspecified, multicast or reserved address"
    if os.environ.get("MSP_BLOCK_PRIVATE_TARGETS", "").strip() in ("1", "true", "yes") \
            and (ip.is_private or ip.is_loopback):
        return "private or loopback address (MSP_BLOCK_PRIVATE_TARGETS is on)"
    return None


def check_host(host: str) -> None:
    host = (host or "").strip().strip("[]")
    if not host:
        raise CallError("Target has no host name", 400)
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        if os.environ.get("MSP_NETGUARD_RESOLVE", "1").strip() in ("0", "false", "no"):
            return
        try:
            ips = [ipaddress.ip_address(info[4][0].split("%")[0])
                   for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)]
        except socket.gaierror as exc:
            raise CallError(f"Cannot resolve {host}: {exc}", 502) from exc
    for ip in ips:
        reason = _bad_ip(ip)
        if reason:
            raise CallError(f"Refusing to connect to {host} ({ip}): {reason}", 400)


def check_url(url: str) -> None:
    parsed = urlparse(url or "")
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise CallError(f"Only http/https URLs are allowed (got '{parsed.scheme or url}')", 400)
    if parsed.username or parsed.password:
        raise CallError("Put credentials in the connection's auth settings, not in the URL", 400)
    check_host(parsed.hostname or "")
