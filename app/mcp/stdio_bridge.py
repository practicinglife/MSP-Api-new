#!/usr/bin/env python3
"""Stdio <-> HTTP bridge for MCP clients that only start local (stdio) servers, e.g. Claude Desktop chat.

    python mcp-bridge.py https://tools.example.com/mcp/<connection>

Credential: the MSP_API_KEY environment variable, or the first line of ~/.msp-tools/key.txt. It is sent as
X-API-Key on every request; the server checks it exactly as for any other client (roles, scopes, write mode).
Only https:// URLs are accepted, except for localhost and private LAN addresses (MSP_BRIDGE_ALLOW_HTTP=1
overrides). Standard library only; nothing else to install.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

TIMEOUT_S = 300


def _fail(message: str) -> None:
    sys.stderr.write(f"mcp-bridge: {message}\n")
    sys.exit(2)


def _key() -> str:
    key = os.environ.get("MSP_API_KEY", "").strip()
    if not key:
        f = Path.home() / ".msp-tools" / "key.txt"
        if f.is_file():
            key = f.read_text(encoding="utf-8").strip().splitlines()[0].strip() if f.stat().st_size else ""
    if not key:
        _fail("no connection key: set MSP_API_KEY or put it in ~/.msp-tools/key.txt")
    return key


def _local_host(host: str) -> bool:
    if host in ("localhost",) or host.endswith(".localhost"):
        return True
    try:
        addrs = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    try:
        return all(ipaddress.ip_address(a.split("%")[0]).is_private for a in addrs)
    except ValueError:
        return False


def _check_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        return url
    if parsed.scheme == "http" and parsed.hostname and (
            os.environ.get("MSP_BRIDGE_ALLOW_HTTP") == "1" or _local_host(parsed.hostname)):
        return url
    _fail(f"refusing {url!r}: use https:// (plain http only for localhost / LAN addresses)")
    return ""


def _ids(line: str) -> list[object]:
    try:
        msg = json.loads(line)
    except ValueError:
        return [None]
    msgs = msg if isinstance(msg, list) else [msg]
    return [m["id"] for m in msgs if isinstance(m, dict) and "id" in m]


def _emit(obj: object) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _errors(line: str, message: str) -> None:
    for mid in _ids(line):
        _emit({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": message}})


def main() -> None:
    url = _check_url(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MSP_MCP_URL", ""))
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "X-API-Key": _key(), "User-Agent": "mcp-stdio-bridge/1"}
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        req = urllib.request.Request(url, data=line.encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 — scheme checked above
                status, ctype = resp.status, resp.headers.get("Content-Type", "")
                body = resp.read().decode("utf-8", "replace")
                if resp.headers.get("Mcp-Session-Id"):
                    headers["Mcp-Session-Id"] = resp.headers["Mcp-Session-Id"]
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            with contextlib.suppress(ValueError, AttributeError):
                detail = json.loads(detail).get("detail") or detail
            hint = " (check the connection key)" if exc.code in (401, 403) else ""
            _errors(line, f"Server answered HTTP {exc.code}: {detail}{hint}")
            continue
        except (urllib.error.URLError, OSError) as exc:
            _errors(line, f"Server unreachable ({url}): {exc}")
            continue
        if status == 202 or not body.strip():
            continue
        if ctype.startswith("text/event-stream"):
            for ev in body.splitlines():
                if ev.startswith("data:") and ev[5:].strip():
                    _emit(json.loads(ev[5:].strip()))
            continue
        try:
            _emit(json.loads(body))
        except ValueError:
            _errors(line, f"Server sent something that is not JSON: {body[:200]}")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()
