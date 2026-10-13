"""Execute REST-shaped operations (OpenAPI, Postman, cURL all import into this binding).

Binding::

    {"method": "GET", "base": "" | "https://…" | "{{baseUrl}}", "path": "/pets/{petId}",
     "params": [{"name": "petId", "in": "path|query|header|cookie", "arg": "petId"}],
     "body": {"arg": "body", "content_type": "application/json"}            # structured body
           | {"template": "<raw text with {{vars}}>", "content_type": "…"}  # raw body
     "headers": {...static...}, "query": {...static...}}

``{{name}}`` placeholders resolve from the call's arguments, then the connection's ``settings.variables``.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

from app.connections.base import CallError, UpstreamError
from app.connections.http import ConnCtx, auth_headers_and_params, client, decode_body, join_url

_VAR = re.compile(r"\{\{\s*([^{}\s]+)\s*\}\}")


def _variables(ctx: ConnCtx, args: dict[str, Any]) -> dict[str, Any]:
    out = dict((ctx.settings or {}).get("variables") or {})
    out.update((ctx.secrets or {}).get("variables") or {})  # secret-looking collection variables (encrypted)
    out.update({k: v for k, v in args.items() if not isinstance(v, (dict, list))})
    return out


def render(template: str, variables: dict[str, Any], *, json_escape: bool = False) -> str:
    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in variables or variables[name] is None:
            raise CallError(f"Missing value for {{{{{name}}}}} — pass '{name}' or set it as a connection variable", 400)
        value = str(variables[name])
        return json.dumps(value)[1:-1] if json_escape else value

    return _VAR.sub(sub, template)


def _soft_render(template: str, variables: dict[str, Any]) -> str:
    """Like ``render`` but leaves unknown placeholders as they are (used for credentials)."""
    return _VAR.sub(lambda m: str(variables.get(m.group(1), m.group(0))), template)


def _encode_query_value(value: Any) -> Any:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (dict,)):
        return json.dumps(value)
    return value


async def execute(ctx: ConnCtx, binding: dict[str, Any], args: dict[str, Any]) -> Any:
    variables = _variables(ctx, args)
    method = str(binding.get("method") or "GET").upper()

    path = str(binding.get("path") or "")
    query: dict[str, Any] = {k: render(str(v), variables) for k, v in (binding.get("query") or {}).items()}
    headers: dict[str, str] = {k: render(str(v), variables) for k, v in (binding.get("headers") or {}).items()}
    cookies: dict[str, str] = {}
    for p in binding.get("params") or []:
        value = args.get(p.get("arg") or p["name"])
        if value is None:
            value = p.get("default")  # Postman / cURL: the collection's value unless the caller overrides it
        if value is None:
            continue
        where = p.get("in", "query")
        if where == "path":
            path = path.replace("{" + p["name"] + "}", quote(str(value), safe=""))
        elif where == "query":
            query[p["name"]] = [_encode_query_value(v) for v in value] if isinstance(value, list) \
                else _encode_query_value(value)
        elif where == "header":
            headers[p["name"]] = str(value)
        elif where == "cookie":
            cookies[p["name"]] = str(value)
    if re.search(r"\{[^{}/]+\}", path.replace("{{", "").replace("}}", "")):
        missing = re.findall(r"\{([^{}/]+)\}", path)
        raise CallError(f"Missing path parameter(s): {', '.join(missing)}", 400)

    base = render(str(binding.get("base") or ""), variables) if binding.get("base") else ctx.base_url
    url = join_url(base, render(path, variables))

    auth_h, auth_q = await auth_headers_and_params(ctx)
    # Postman keeps credentials as {{variables}} (e.g. an API key of "{{apiKey}}"): fill them in.
    headers = {**headers, **{k: _soft_render(v, variables) for k, v in auth_h.items()}}
    query.update({k: _soft_render(v, variables) for k, v in auth_q.items()})

    content: bytes | None = None
    json_body: Any = None
    form: dict[str, Any] | None = None
    body = binding.get("body") or None
    if body:
        ctype = body.get("content_type") or "application/json"
        if "template" in body:
            text = render(str(body["template"]), variables, json_escape="json" in ctype)
            content = text.encode()
            headers.setdefault("Content-Type", ctype)
        else:
            value = args.get(body.get("arg") or "body")
            if value is not None:
                if "x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype:
                    form = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                            for k, v in (value.items() if isinstance(value, dict) else [])}
                elif "json" in ctype:
                    json_body = value
                else:
                    content = (value if isinstance(value, str) else json.dumps(value)).encode()
                    headers.setdefault("Content-Type", ctype)
    headers.setdefault("Accept", "application/json, */*;q=0.5")

    async with client(cookies=cookies or None) as c:
        r = await c.request(method, url, params=query or None, headers=headers, json=json_body, data=form,
                            content=content)
    data = decode_body(r)
    if r.status_code >= 400:
        raise UpstreamError(f"{method} {path} returned HTTP {r.status_code}", r.status_code, data)
    out = {"status": r.status_code, "data": data}
    nxt = r.links.get("next", {}).get("url")  # RFC 8288 Link: <…>; rel="next" (used to page through syncs)
    if nxt:
        out["next_link"] = nxt
    return out
