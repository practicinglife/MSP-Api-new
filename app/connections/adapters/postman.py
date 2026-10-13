"""Postman collection (v2.0 / v2.1) → operations. Every request becomes a tool.

* Folders become tags; request names become tool names.
* ``:id`` path segments become path parameters; ``{{var}}`` placeholders that aren't collection variables
  become arguments; query parameters become optional arguments with the collection's value as default.
* Body modes: raw (JSON/XML/text — kept as a template), urlencoded and form-data (an object argument),
  GraphQL (query + variables).
* Collection variables go to the connection: secret-looking ones (token, key, password, secret) encrypted.
* Collection/folder auth becomes the connection's auth suggestion.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any
from urllib.parse import urlsplit

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

_VAR = re.compile(r"\{\{\s*([^{}\s]+)\s*\}\}")
_SECRETISH = re.compile(r"(token|secret|password|passwd|api[_-]?key|apikey|auth|credential|private)", re.I)
_LANG_CTYPE = {"json": "application/json", "xml": "application/xml", "html": "text/html",
               "text": "text/plain", "javascript": "application/javascript"}


def _kv(items: Any) -> list[tuple[str, str]]:
    out = []
    for it in items or []:
        if isinstance(it, dict) and it.get("key") and not it.get("disabled"):
            out.append((str(it["key"]), "" if it.get("value") is None else str(it["value"])))
    return out


def _description(d: Any) -> str:
    if isinstance(d, dict):
        d = d.get("content")
    return str(d or "").strip()[:2000]


def _raw_url(url: Any) -> str:
    if isinstance(url, str):
        return url
    if not isinstance(url, dict):
        return ""
    if url.get("raw"):
        return str(url["raw"])
    host = url.get("host") or []
    host = ".".join(host) if isinstance(host, list) else str(host)
    path = url.get("path") or []
    path = "/".join(str(p) if not isinstance(p, dict) else str(p.get("value", "")) for p in path) \
        if isinstance(path, list) else str(path)
    proto = f"{url['protocol']}://" if url.get("protocol") else ""
    return f"{proto}{host}/{path}"


def _split(raw: str) -> tuple[str, str]:
    """``{{baseUrl}}/pets/:id?x=1`` → (``{{baseUrl}}``, ``/pets/:id``); ``https://h/p`` → (``https://h``, ``/p``)."""
    raw = raw.split("#", 1)[0].split("?", 1)[0].strip()
    m = re.match(r"^(\{\{[^}]+\}\}[^/]*)(/.*)?$", raw)
    if m:
        return m.group(1), m.group(2) or "/"
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    return f"{parts.scheme}://{parts.netloc}", parts.path or "/"


def _auth_hint(auth: Any) -> dict[str, Any] | None:
    if not isinstance(auth, dict) or not auth.get("type") or auth["type"] == "noauth":
        return None
    t = auth["type"]
    raw = auth.get(t)
    vals = dict(_kv(raw)) if isinstance(raw, list) else (raw if isinstance(raw, dict) else {})
    if t == "bearer":
        return {"auth_type": "bearer", "secrets": {"token": vals.get("token", "")}}
    if t == "basic":
        return {"auth_type": "basic", "secrets": {"username": vals.get("username", ""), "password": vals.get("password", "")}}
    if t == "apikey":
        return {"auth_type": "api_key", "api_key_name": vals.get("key", "X-API-Key"),
                "api_key_in": "query" if vals.get("in") == "query" else "header",
                "secrets": {"api_key": vals.get("value", "")}}
    if t == "oauth2":
        return {"auth_type": "bearer", "note": "OAuth 2.0 in Postman — paste an access token or configure client credentials",
                "secrets": {"token": vals.get("accessToken", "")}}
    return {"auth_type": "none", "note": f"Postman auth type '{t}' is not imported — set auth on the connection"}


def _walk(items: list[Any], folders: list[str], out: list[tuple[list[str], dict[str, Any]]]) -> None:
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if isinstance(it.get("item"), list):
            _walk(it["item"], [*folders, str(it.get("name", ""))], out)
        elif isinstance(it.get("request"), (dict, str)):
            out.append((folders, it))


def _request(item: dict[str, Any]) -> dict[str, Any]:
    req = item["request"]
    return req if isinstance(req, dict) else {"url": req, "method": "GET"}


def build(collection: dict[str, Any]) -> ImportResult:
    if not isinstance(collection.get("item"), list):
        raise SourceError("Not a Postman collection (no 'item' list) — export it as Collection v2.1")
    info = collection.get("info") or {}
    variables = dict(_kv(collection.get("variable")))
    plain_vars = {k: v for k, v in variables.items() if not _SECRETISH.search(k)}
    secret_vars = {k: v for k, v in variables.items() if _SECRETISH.search(k)}

    requests: list[tuple[list[str], dict[str, Any]]] = []
    _walk(collection["item"], [], requests)

    origins = Counter(_split(_raw_url(_request(item).get("url")))[0] for _, item in requests)
    common_origin = origins.most_common(1)[0][0] if origins else ""
    base_url = ""
    if common_origin.startswith("{{"):
        var = _VAR.match(common_origin)
        if var and variables.get(var.group(1), "").startswith("http"):
            base_url = variables[var.group(1)].rstrip("/") + common_origin[var.end():]
    elif common_origin:
        base_url = common_origin

    warnings: list[str] = []
    ops: list[OperationDef] = []
    for folders, item in requests:
        req = _request(item)
        method = str(req.get("method") or "GET").upper()
        url_obj = req.get("url")
        origin, path = _split(_raw_url(url_obj))
        base = "" if origin == common_origin and base_url else origin

        properties: dict[str, Any] = {}
        required: list[str] = []
        params: list[dict[str, Any]] = []
        path_vars = dict(_kv(url_obj.get("variable"))) if isinstance(url_obj, dict) else {}

        def add_template_args(text: str, properties: dict[str, Any] = properties, required: list[str] = required) -> None:
            for name in _VAR.findall(text):
                if name not in variables and name not in properties:
                    properties[name] = {"type": "string", "description": f"Value for {{{{{name}}}}}"}
                    required.append(name)

        segments = []
        for seg in path.split("/"):
            if seg.startswith(":") and len(seg) > 1:
                pname = seg[1:]
                segments.append("{" + pname + "}")
                properties[pname] = {"type": "string"}
                if path_vars.get(pname):
                    properties[pname]["default"] = path_vars[pname]
                required.append(pname)
                params.append({"name": pname, "in": "path", "arg": pname})
            else:
                segments.append(seg)
        path = "/".join(segments)
        add_template_args(path)
        add_template_args(base)

        static_query: dict[str, str] = {}
        query_items = _kv(url_obj.get("query")) if isinstance(url_obj, dict) else []
        if not query_items and "?" in _raw_url(url_obj):
            query_items = [tuple(p.split("=", 1)) if "=" in p else (p, "")  # type: ignore[misc]
                           for p in _raw_url(url_obj).split("?", 1)[1].split("&") if p]
        for key, value in query_items:
            if _VAR.search(value):
                static_query[key] = value
                add_template_args(value)
            elif key not in properties:
                properties[key] = {"type": "string", **({"default": value} if value else {})}
                params.append({"name": key, "in": "query", "arg": key, **({"default": value} if value else {})})

        headers: dict[str, str] = {}
        for key, value in _kv(req.get("header")):
            if key.lower() == "authorization":
                continue
            headers[key] = value
            add_template_args(value)

        body_binding: dict[str, Any] | None = None
        body = req.get("body") if isinstance(req.get("body"), dict) else {}
        mode = body.get("mode") if body and not body.get("disabled") else None
        if mode == "raw" and body.get("raw"):
            lang = ((body.get("options") or {}).get("raw") or {}).get("language", "")
            ctype = headers.pop("Content-Type", None) or headers.pop("content-type", None) \
                or _LANG_CTYPE.get(lang, "application/json" if str(body["raw"]).lstrip().startswith(("{", "[")) else "text/plain")
            body_binding = {"template": str(body["raw"]), "content_type": ctype}
            add_template_args(str(body["raw"]))
        elif mode in ("urlencoded", "formdata"):
            fields = {}
            for f in body.get(mode) or []:
                if not isinstance(f, dict) or f.get("disabled") or not f.get("key"):
                    continue
                if f.get("type") == "file":
                    warnings.append(f"'{item.get('name')}': file field '{f['key']}' is not supported and was skipped")
                    continue
                fields[str(f["key"])] = {"type": "string", **({"default": str(f["value"])} if f.get("value") else {})}
            properties["body"] = object_schema(fields)
            body_binding = {"arg": "body", "content_type": "application/x-www-form-urlencoded" if mode == "urlencoded"
                            else "multipart/form-data"}
        elif mode == "graphql":
            gql = body.get("graphql") or {}
            template = json.dumps({"query": gql.get("query", ""),
                                   "variables": json.loads(gql["variables"]) if gql.get("variables") else {}})
            body_binding = {"template": template, "content_type": "application/json"}
            add_template_args(template)

        name = str(item.get("name") or f"{method} {path}")
        ops.append(OperationDef(
            key="/".join([*folders, name]) + f" [{method}]",
            name=tool_name(name),
            title=name,
            description=_description(req.get("description") or item.get("description")),
            tags=[f for f in folders if f][:10],
            method=method,
            target=path,
            input_schema=object_schema(properties, required),
            binding={"method": method, "base": base, "path": path, "params": params, "query": static_query,
                     "headers": headers, "body": body_binding},
            read_only=method in ("GET", "HEAD", "OPTIONS"),
        ))

    hint = _auth_hint(collection.get("auth"))
    settings: dict[str, Any] = {"variables": plain_vars} if plain_vars else {}
    if secret_vars:
        hint = {**(hint or {"auth_type": "none"}),
                "secrets": {**((hint or {}).get("secrets") or {}), "variables": secret_vars}}
    return ImportResult(operations=finalize(ops), name=str(info.get("name") or ""),
                        description=_description(info.get("description")), base_url=base_url,
                        settings=settings, auth_hint=hint, warnings=warnings)


async def discover(ctx: ConnCtx, source: dict[str, Any]) -> ImportResult:
    text = await load_source(source, ctx)
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise SourceError(f"A Postman collection is JSON: {exc}") from exc
    if isinstance(doc, dict) and isinstance(doc.get("collection"), dict):  # Postman API export wrapper
        doc = doc["collection"]
    if not isinstance(doc, dict):
        raise SourceError("Not a Postman collection")
    return build(doc)


execute = rest.execute
