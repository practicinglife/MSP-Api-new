"""OpenAPI 3.x and Swagger 2.0 → operations (JSON or YAML; pasted or by URL)."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

import yaml

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

METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
READ_METHODS = {"GET", "HEAD", "OPTIONS"}
_SCHEMA_KEYS = ("type", "format", "description", "enum", "default", "minimum", "maximum", "exclusiveMinimum",
                "exclusiveMaximum", "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems",
                "title", "const", "example", "readOnly", "writeOnly", "multipleOf")
MAX_DEPTH = 4
MAX_PROPERTIES = 150


class _Loader(yaml.SafeLoader):
    """Safe YAML that also accepts a bare ``=`` (YAML 1.1's "value" tag), which some published definitions
    contain (e.g. Zendesk's) and plain ``safe_load`` rejects."""


_Loader.add_constructor("tag:yaml.org,2002:value", lambda loader, node: loader.construct_scalar(node))


def parse_document(text: str) -> dict[str, Any]:
    try:
        doc = json.loads(text)
    except ValueError:
        try:
            doc = yaml.load(text, Loader=_Loader)  # noqa: S506 — a SafeLoader subclass
        except yaml.YAMLError as exc:
            raise SourceError(f"Not valid JSON or YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise SourceError("The definition is not a JSON/YAML object")
    return doc


class _Refs:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.external = 0

    def get(self, ref: str) -> Any:
        if not ref.startswith("#/"):
            self.external += 1
            return {}
        node: Any = self.spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                return {}
        return node

    def deref(self, node: Any) -> Any:
        seen = 0
        while isinstance(node, dict) and "$ref" in node and seen < 20:
            node = self.get(node["$ref"])
            seen += 1
        return node

    def schema(self, node: Any, depth: int = 0, stack: tuple[str, ...] = ()) -> dict[str, Any]:
        if not isinstance(node, dict):
            return {}
        if "$ref" in node:
            ref = str(node["$ref"])
            label = ref.rsplit("/", 1)[-1]
            if ref in stack or depth >= MAX_DEPTH:
                return {"type": "object", "description": f"{label} (nested object — see the API's documentation)"}
            out = self.schema(self.get(ref), depth + 1, (*stack, ref))
            if node.get("description"):
                out = {**out, "description": node["description"]}
            return out
        out: dict[str, Any] = {k: node[k] for k in _SCHEMA_KEYS if k in node}
        if out.get("type") == "file":  # Swagger 2
            out.update({"type": "string", "format": "binary"})
        if node.get("nullable") and isinstance(out.get("type"), str):
            out["type"] = [out["type"], "null"]
        if isinstance(node.get("properties"), dict):
            props = list(node["properties"].items())[:MAX_PROPERTIES]
            out["properties"] = {k: self.schema(v, depth + 1, stack) for k, v in props}
            out.setdefault("type", "object")
        if isinstance(node.get("required"), list) and "properties" in out:
            req = [r for r in node["required"] if r in out["properties"]]
            if req:
                out["required"] = req
        if "items" in node:
            out["items"] = self.schema(node["items"], depth + 1, stack)
        if isinstance(node.get("additionalProperties"), dict):
            out["additionalProperties"] = self.schema(node["additionalProperties"], depth + 1, stack)
        for combo in ("allOf", "oneOf", "anyOf"):
            if isinstance(node.get(combo), list):
                out[combo] = [self.schema(s, depth + 1, stack) for s in node[combo][:20]]
        return out


def _base_url(spec: dict[str, Any], source_url: str) -> str:
    if spec.get("swagger"):
        host = spec.get("host")
        if not host:
            return urljoin(source_url, spec.get("basePath", "/")) if source_url else ""
        schemes = spec.get("schemes") or ["https"]
        scheme = "https" if "https" in schemes else schemes[0]
        return f"{scheme}://{host}{spec.get('basePath', '')}".rstrip("/")
    servers = spec.get("servers") or []
    if not servers:
        return ""
    server = servers[0] if isinstance(servers[0], dict) else {}
    url = str(server.get("url") or "")
    for name, var in (server.get("variables") or {}).items():
        url = url.replace("{" + name + "}", str((var or {}).get("default", "")))
    if url and not url.startswith(("http://", "https://")) and source_url:
        url = urljoin(source_url, url)
    return url.rstrip("/")


def _auth_hint(spec: dict[str, Any], refs: _Refs) -> dict[str, Any] | None:
    schemes = (spec.get("components") or {}).get("securitySchemes") or spec.get("securityDefinitions") or {}
    if not schemes:
        return None
    preferred = [next(iter(req)) for req in spec.get("security") or [] if isinstance(req, dict) and req]
    order = [n for n in preferred if n in schemes] + [n for n in schemes if n not in preferred]
    for name in order:
        s = refs.deref(schemes[name]) or {}
        t = str(s.get("type", "")).lower()
        if t == "apikey" and s.get("in") in ("header", "query"):
            return {"auth_type": "api_key", "api_key_name": s.get("name"), "api_key_in": s["in"], "scheme": name}
        if t == "http" and str(s.get("scheme", "")).lower() == "bearer":
            return {"auth_type": "bearer", "scheme": name}
        if (t == "http" and str(s.get("scheme", "")).lower() == "basic") or t == "basic":
            return {"auth_type": "basic", "scheme": name}
        if t == "oauth2":
            flows = s.get("flows") or {}
            cc = flows.get("clientCredentials") or (s if s.get("flow") == "application" else None)
            if cc and cc.get("tokenUrl"):
                return {"auth_type": "oauth2_client_credentials", "token_url": cc["tokenUrl"],
                        "scope": " ".join((cc.get("scopes") or {}).keys()), "scheme": name}
            pw = flows.get("password") or (s if s.get("flow") == "password" else None)
            if pw and pw.get("tokenUrl"):
                return {"auth_type": "oauth2_password", "token_url": pw["tokenUrl"],
                        "scope": " ".join((pw.get("scopes") or {}).keys()), "scheme": name}
            return {"auth_type": "bearer", "scheme": name, "note": "OAuth2 (sign-in flow) — paste a token"}
        if t == "openidconnect":
            return {"auth_type": "bearer", "scheme": name}
    return None


_AUTH_HEADERS = {"authorization", "x-api-key", "api-key", "apikey"}


def _param_schema(p: dict[str, Any], refs: _Refs, is_v3: bool) -> dict[str, Any]:
    if is_v3:
        if "schema" in p:
            schema = refs.schema(p["schema"])
        elif isinstance(p.get("content"), dict) and p["content"]:
            schema = refs.schema(next(iter(p["content"].values())).get("schema", {}))
        else:
            schema = {"type": "string"}
    else:
        schema = refs.schema({k: v for k, v in p.items() if k not in ("name", "in", "required", "description")})
    if p.get("description"):
        schema["description"] = str(p["description"])[:1000]
    return schema or {"type": "string"}


def _pick_content(content: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(content, dict) or not content:
        return None
    for ctype in content:
        if "json" in ctype:
            return ctype, content[ctype] or {}
    for wanted in ("application/x-www-form-urlencoded", "multipart/form-data", "text/plain", "application/xml"):
        if wanted in content:
            return wanted, content[wanted] or {}
    ctype = next(iter(content))
    return ctype, content[ctype] or {}


def _output_schema(op: dict[str, Any], refs: _Refs, is_v3: bool) -> dict[str, Any] | None:
    responses = op.get("responses") or {}
    for code in ("200", "201", "202", "2XX", "default"):
        resp = refs.deref(responses.get(code))
        if not isinstance(resp, dict):
            continue
        if is_v3:
            picked = _pick_content(resp.get("content") or {})
            if picked and picked[1].get("schema"):
                return refs.schema(picked[1]["schema"], depth=1)
        elif resp.get("schema"):
            return refs.schema(resp["schema"], depth=1)
    return None


def build_operations(spec: dict[str, Any]) -> tuple[list[OperationDef], _Refs]:
    is_v3 = str(spec.get("openapi", "")).startswith("3")
    if not is_v3 and not str(spec.get("swagger", "")).startswith("2"):
        raise SourceError("Not an OpenAPI 3 or Swagger 2 document (missing 'openapi' / 'swagger' version)")
    refs = _Refs(spec)
    ops: list[OperationDef] = []
    for path, raw_item in (spec.get("paths") or {}).items():
        item = refs.deref(raw_item)
        if not isinstance(item, dict):
            continue
        shared = [refs.deref(p) for p in item.get("parameters") or []]
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            params_by_key: dict[tuple[str, str], dict[str, Any]] = {}
            for p in shared + [refs.deref(p) for p in op.get("parameters") or []]:
                if isinstance(p, dict) and p.get("name") and p.get("in"):
                    params_by_key[(p["in"], p["name"])] = p

            properties: dict[str, Any] = {}
            required: list[str] = []
            bparams: list[dict[str, Any]] = []
            form_props: dict[str, Any] = {}
            form_required: list[str] = []
            form_ctype = "application/x-www-form-urlencoded"
            body: dict[str, Any] | None = None
            for (where, pname), p in params_by_key.items():
                if where == "header" and pname.lower() in _AUTH_HEADERS:
                    continue
                if where == "body":  # Swagger 2 body parameter
                    properties["body"] = refs.schema(p.get("schema") or {})
                    if p.get("description"):
                        properties["body"]["description"] = str(p["description"])[:1000]
                    consumes = op.get("consumes") or spec.get("consumes") or ["application/json"]
                    body = {"arg": "body", "content_type": consumes[0]}
                    if p.get("required"):
                        required.append("body")
                    continue
                if where == "formData":
                    form_props[pname] = _param_schema(p, refs, is_v3)
                    if p.get("type") == "file":
                        form_ctype = "multipart/form-data"
                    if p.get("required"):
                        form_required.append(pname)
                    continue
                arg = pname if pname not in properties else f"{where}_{pname}"
                arg = re.sub(r"[^0-9A-Za-z_.\-]", "_", arg)
                properties[arg] = _param_schema(p, refs, is_v3)
                if p.get("required") or where == "path":
                    required.append(arg)
                bparams.append({"name": pname, "in": where, "arg": arg})
            if form_props:
                properties["body"] = object_schema(form_props, form_required)
                body = {"arg": "body", "content_type": form_ctype}
                if form_required:
                    required.append("body")
            rb = refs.deref(op.get("requestBody")) if is_v3 else None
            if isinstance(rb, dict):
                picked = _pick_content(rb.get("content") or {})
                if picked:
                    ctype, media = picked
                    arg = "body" if "body" not in properties else "request_body"
                    properties[arg] = refs.schema(media.get("schema") or {}) or {"type": "object"}
                    if rb.get("description"):
                        properties[arg]["description"] = str(rb["description"])[:1000]
                    body = {"arg": arg, "content_type": ctype}
                    if rb.get("required"):
                        required.append(arg)

            verb = method.upper()
            summary = str(op.get("summary") or "").strip()
            description = str(op.get("description") or "").strip()
            ops.append(OperationDef(
                key=f"{verb} {path}",
                name=tool_name(op["operationId"]) if op.get("operationId") else tool_name(method, path),
                title=summary or f"{verb} {path}",
                description=(description or summary)[:2000],
                tags=[str(t) for t in op.get("tags") or []][:10],
                method=verb,
                target=path,
                input_schema=object_schema(properties, required),
                output_schema=_output_schema(op, refs, is_v3),
                binding={"method": verb, "path": path, "params": bparams, "body": body},
                read_only=verb in READ_METHODS,
                deprecated=bool(op.get("deprecated")),
            ))
    return finalize(ops), refs


async def discover(ctx: ConnCtx, source: dict[str, Any]) -> ImportResult:
    spec = parse_document(await load_source(source, ctx))
    ops, refs = build_operations(spec)
    info = spec.get("info") or {}
    warnings = []
    if refs.external:
        warnings.append(f"{refs.external} external $ref(s) were not followed (only in-document references are)")
    if not ops:
        warnings.append("The document has no operations")
    return ImportResult(
        operations=ops, name=str(info.get("title") or ""), description=str(info.get("description") or "")[:2000],
        base_url=_base_url(spec, str(source.get("url") or "")), auth_hint=_auth_hint(spec, refs),
        warnings=warnings,
    )


execute = rest.execute
