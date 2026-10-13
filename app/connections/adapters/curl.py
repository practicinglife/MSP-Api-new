"""cURL command(s) → operations. Paste one or several ``curl …`` commands (line continuations allowed).

* A comment line ``# name: Create ticket`` right before a command names it.
* Placeholders ``{{id}}``, ``<id>``, ``$ID`` / ``${ID}`` become arguments.
* Query parameters become optional arguments (their value is the default).
* Credentials (``-u``, ``Authorization:``, ``X-API-Key:``) are lifted into the connection's encrypted auth,
  never stored on the operation.
"""

from __future__ import annotations

import re
import shlex
from collections import Counter
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from app.connections.adapters import rest
from app.connections.base import (
    ImportResult,
    OperationDef,
    SourceError,
    finalize,
    object_schema,
    tool_name,
)
from app.connections.http import ConnCtx, load_source

# $NAME only in upper case (shell-variable style) so OData keys like $filter / $top are left alone.
_PLACEHOLDERS = [re.compile(r"<([A-Za-z_][\w-]*)>"), re.compile(r"\$\{([A-Za-z_]\w*)\}"),
                 re.compile(r"\$([A-Z_][A-Z0-9_]*)\b")]
_VAR = re.compile(r"\{\{\s*([^{}\s]+)\s*\}\}")
_IGNORE_FLAGS = {"-k", "--insecure", "-s", "--silent", "-S", "--show-error", "-L", "--location", "-v", "--verbose",
                 "--compressed", "-i", "--include", "-f", "--fail", "-g", "--globoff", "--http1.1", "--http2"}
_IGNORE_WITH_VALUE = {"-o", "--output", "-m", "--max-time", "--connect-timeout", "-w", "--write-out", "-x",
                      "--proxy", "--retry", "-e", "--referer", "--cacert", "--cert", "--key"}
_DATA_FLAGS = {"-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode"}


def _normalize_placeholders(text: str) -> str:
    for rx in _PLACEHOLDERS:
        text = rx.sub(lambda m: "{{" + m.group(1) + "}}", text)
    return text


def _commands(text: str) -> list[tuple[str, str]]:
    """Split pasted text into (name, command) pairs."""
    text = re.sub(r"\\\r?\n", " ", text)       # bash line continuation
    text = re.sub(r"\^\r?\n", " ", text)       # cmd.exe line continuation
    text = re.sub(r"`\r?\n", " ", text)        # PowerShell line continuation
    out: list[tuple[str, str]] = []
    pending_name = ""
    for line in text.splitlines():
        stripped = line.strip()
        m = re.match(r"^#\s*name\s*:\s*(.+)$", stripped, re.I)
        if m:
            pending_name = m.group(1).strip()
        elif stripped.startswith("curl ") or stripped == "curl":
            out.append((pending_name, stripped))
            pending_name = ""
        elif out and stripped and not stripped.startswith("#"):
            out[-1] = (out[-1][0], out[-1][1] + " " + stripped)
    return out


def parse_command(cmd: str) -> dict[str, Any]:
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError as exc:
        raise SourceError(f"Could not parse cURL command: {exc}") from exc
    if not tokens or tokens[0] != "curl":
        raise SourceError("Each command must start with 'curl'")
    out: dict[str, Any] = {"method": None, "url": "", "headers": [], "data": [], "form": [], "user": None,
                           "get": False, "json": False}
    i = 1
    while i < len(tokens):
        t = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None

        def take(t: str = t, nxt: str | None = nxt) -> str:
            nonlocal i
            i += 1
            if nxt is None:
                raise SourceError(f"{t} needs a value")
            return nxt

        if t in ("-X", "--request"):
            out["method"] = take().upper()
        elif t in ("-H", "--header"):
            out["headers"].append(take())
        elif t in _DATA_FLAGS:
            out["data"].append(take())
        elif t == "--json":
            out["data"].append(take())
            out["json"] = True
        elif t in ("-F", "--form"):
            out["form"].append(take())
        elif t in ("-u", "--user"):
            out["user"] = take()
        elif t in ("-G", "--get"):
            out["get"] = True
        elif t in ("-b", "--cookie"):
            out["headers"].append(f"Cookie: {take()}")
        elif t in ("-A", "--user-agent"):
            out["headers"].append(f"User-Agent: {take()}")
        elif t == "--url":
            out["url"] = take()
        elif t in _IGNORE_WITH_VALUE:
            take()
        elif t in _IGNORE_FLAGS or (t.startswith("-") and not t.startswith("--") and len(t) > 2 and set(t[1:]) <= set("ksSLvifg")):
            pass
        elif not t.startswith("-") and not out["url"]:
            out["url"] = t
        i += 1
    if not out["url"]:
        raise SourceError("cURL command has no URL")
    return out


def _to_op(name: str, parsed: dict[str, Any], auth: dict[str, Any]) -> tuple[OperationDef, str]:
    url = _normalize_placeholders(parsed["url"])
    if "://" not in url and not url.startswith("{{"):
        url = "https://" + url
    if url.startswith("{{"):
        m = re.match(r"^(\{\{[^}]+\}\}[^/?]*)(/[^?]*)?(\?.*)?$", url)
        origin, path, query = (m.group(1), m.group(2) or "/", (m.group(3) or "")[1:]) if m else (url, "/", "")
    else:
        parts = urlsplit(url)
        origin, path, query = f"{parts.scheme}://{parts.netloc}", parts.path or "/", parts.query

    properties: dict[str, Any] = {}
    required: list[str] = []
    params: list[dict[str, Any]] = []

    def template_args(text: str) -> None:
        for v in _VAR.findall(text):
            if v not in properties:
                properties[v] = {"type": "string"}
                required.append(v)

    template_args(origin)
    template_args(path)
    static_query: dict[str, str] = {}
    data = [_normalize_placeholders(d) for d in parsed["data"]]
    query_pairs = parse_qsl(query, keep_blank_values=True)
    if parsed["get"] and data:
        query_pairs += parse_qsl("&".join(data), keep_blank_values=True)
        data = []
    for k, v in query_pairs:
        v = _normalize_placeholders(v)
        if _VAR.search(v):
            static_query[k] = v
            template_args(v)
        elif k not in properties:
            properties[k] = {"type": "string", **({"default": v} if v else {})}
            params.append({"name": k, "in": "query", "arg": k, **({"default": v} if v else {})})

    headers: dict[str, str] = {}
    for h in parsed["headers"]:
        if ":" not in h:
            continue
        k, v = (s.strip() for s in h.split(":", 1))
        v = _normalize_placeholders(v)
        lk = k.lower()
        if lk == "authorization":
            if v.lower().startswith("bearer ") and not _VAR.search(v):
                auth.setdefault("auth_type", "bearer")
                auth.setdefault("secrets", {}).setdefault("token", v[7:].strip())
            elif v.lower().startswith("bearer "):
                auth.setdefault("auth_type", "bearer")
            continue
        if lk in ("x-api-key", "api-key", "apikey") and not _VAR.search(v):
            auth.setdefault("auth_type", "api_key")
            auth.setdefault("api_key_name", k)
            auth.setdefault("secrets", {}).setdefault("api_key", v)
            continue
        headers[k] = v
        template_args(v)
    if parsed["user"] and ":" in parsed["user"] and not _VAR.search(parsed["user"]):
        user, pw = parsed["user"].split(":", 1)
        auth.setdefault("auth_type", "basic")
        auth.setdefault("secrets", {}).update({"username": user, "password": pw})

    method = parsed["method"] or ("POST" if data or parsed["form"] else "GET")
    body: dict[str, Any] | None = None
    if parsed["form"]:
        fields = {}
        for f in parsed["form"]:
            k, _, v = f.partition("=")
            if not v.startswith("@"):
                fields[k] = {"type": "string", **({"default": v} if v else {})}
        properties["body"] = object_schema(fields)
        body = {"arg": "body", "content_type": "multipart/form-data"}
    elif data:
        raw = "&".join(data)
        ctype = headers.pop("Content-Type", None) or headers.pop("content-type", None)
        if not ctype:
            ctype = "application/json" if parsed["json"] or raw.lstrip().startswith(("{", "[")) \
                else "application/x-www-form-urlencoded"
        body = {"template": raw, "content_type": ctype}
        template_args(raw)

    title = name or f"{method} {path}"
    op = OperationDef(
        key=f"{method} {path}" + (f" ({name})" if name else ""),
        name=tool_name(name) if name else tool_name(method, path),
        title=title, method=method, target=path,
        input_schema=object_schema(properties, required),
        binding={"method": method, "base": origin, "path": path, "params": params, "query": static_query,
                 "headers": headers, "body": body},
        read_only=method in ("GET", "HEAD", "OPTIONS"),
    )
    return op, origin


async def discover(ctx: ConnCtx, source: dict[str, Any]) -> ImportResult:
    text = await load_source(source, ctx)
    commands = _commands(text)
    if not commands:
        raise SourceError("No cURL commands found — each must start with 'curl'")
    auth: dict[str, Any] = {}
    ops: list[OperationDef] = []
    origins: Counter[str] = Counter()
    for name, cmd in commands:
        op, origin = _to_op(name, parse_command(cmd), auth)
        ops.append(op)
        origins[origin] += 1
    # The most common literal origin becomes the connection's base URL (editable later).
    base_url = next((o for o, _ in origins.most_common() if not o.startswith("{{")), "")
    for op in ops:
        if op.binding["base"] == base_url:
            op.binding["base"] = ""
    warnings = []
    if "secrets" in auth:
        warnings.append("Credentials found in the command(s) were moved into the connection's encrypted auth")
    return ImportResult(operations=finalize(ops), base_url=base_url, auth_hint=auth or None, warnings=warnings)


execute = rest.execute
