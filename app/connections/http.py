"""HTTP plumbing shared by every HTTP-based adapter: authentication profiles, the SSRF guard, limits."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import httpx

from app.connections.base import CallError
from app.connections.netguard import check_url

TIMEOUT = httpx.Timeout(60.0, connect=10.0)
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_SOURCE_BYTES = 60 * 1024 * 1024

# auth_type → secret fields it needs (kept in Connection.secrets, encrypted) and non-secret settings
AUTH_TYPES: dict[str, dict[str, list[str]]] = {
    "none": {"secrets": [], "settings": []},
    "api_key": {"secrets": ["api_key"], "settings": ["api_key_name", "api_key_in"]},   # in: header | query
    "basic": {"secrets": ["username", "password"], "settings": []},
    "bearer": {"secrets": ["token"], "settings": []},
    "headers": {"secrets": ["headers"], "settings": []},                               # {"X-Custom": "…"}
    "oauth2_client_credentials": {"secrets": ["client_id", "client_secret"],
                                  "settings": ["token_url", "scope", "audience", "client_auth"]},
    "wsse": {"secrets": ["username", "password"], "settings": []},                     # SOAP WS-Security
}


@dataclass
class ConnCtx:
    """What an adapter needs to know about a connection (decoupled from the ORM row)."""

    id: str
    slug: str
    kind: str
    base_url: str = ""
    auth_type: str = "none"
    secrets: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)


_token_cache: dict[str, tuple[float, str]] = {}


async def _oauth2_token(ctx: ConnCtx) -> str:
    cache_key = f"{ctx.id}:{ctx.settings.get('token_url')}:{ctx.secrets.get('client_id')}"
    hit = _token_cache.get(cache_key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    token_url = ctx.settings.get("token_url") or ""
    check_url(token_url)
    data = {"grant_type": "client_credentials"}
    if ctx.settings.get("scope"):
        data["scope"] = ctx.settings["scope"]
    if ctx.settings.get("audience"):
        data["audience"] = ctx.settings["audience"]
    headers = {"Accept": "application/json"}
    cid, secret = str(ctx.secrets.get("client_id", "")), str(ctx.secrets.get("client_secret", ""))
    if ctx.settings.get("client_auth", "basic") == "body":
        data.update({"client_id": cid, "client_secret": secret})
    else:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode()
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(token_url, data=data, headers=headers)
    if r.status_code != 200:
        raise CallError(f"OAuth2 token request failed (HTTP {r.status_code})", 502, r.text[:500])
    body = r.json()
    token = body.get("access_token")
    if not token:
        raise CallError("OAuth2 token response had no access_token", 502)
    _token_cache[cache_key] = (time.monotonic() + max(30, int(body.get("expires_in", 3600)) - 60), token)
    return str(token)


async def auth_headers_and_params(ctx: ConnCtx) -> tuple[dict[str, str], dict[str, str]]:
    """Headers and query parameters that authenticate a request for this connection."""
    t, s = ctx.auth_type or "none", ctx.secrets or {}
    if t == "api_key":
        name = ctx.settings.get("api_key_name") or "X-API-Key"
        if ctx.settings.get("api_key_in", "header") == "query":
            return {}, {name: str(s.get("api_key", ""))}
        return {name: str(s.get("api_key", ""))}, {}
    if t == "basic":  # (wsse is applied inside the SOAP envelope by the SOAP adapter)
        raw = f"{s.get('username', '')}:{s.get('password', '')}"
        return {"Authorization": "Basic " + base64.b64encode(raw.encode()).decode()}, {}
    if t == "bearer":
        return {"Authorization": f"Bearer {s.get('token', '')}"}, {}
    if t == "headers":
        hdrs = s.get("headers") or {}
        return {str(k): str(v) for k, v in hdrs.items()} if isinstance(hdrs, dict) else {}, {}
    if t == "oauth2_client_credentials":
        return {"Authorization": f"Bearer {await _oauth2_token(ctx)}"}, {}
    return {}, {}


async def _guard_request(request: httpx.Request) -> None:
    check_url(str(request.url))  # runs for every hop, redirects included


def client(**kwargs: Any) -> httpx.AsyncClient:
    """An httpx client with the SSRF guard on every request (and redirect)."""
    hooks = kwargs.pop("event_hooks", {})
    hooks.setdefault("request", []).append(_guard_request)
    return httpx.AsyncClient(timeout=kwargs.pop("timeout", TIMEOUT), follow_redirects=True,
                             max_redirects=5, event_hooks=hooks, **kwargs)


def join_url(base: str, path: str) -> str:
    if path.startswith(("http://", "https://")):
        return path
    if not base:
        raise CallError("This connection has no base URL — set one in the connection settings", 400)
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def decode_body(response: httpx.Response) -> Any:
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise CallError(f"Response is larger than {MAX_RESPONSE_BYTES // (1024 * 1024)} MB", 502)
    ctype = response.headers.get("content-type", "")
    if not response.content:
        return None
    if "json" in ctype:
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError):
            pass
    try:
        return response.json()
    except ValueError:
        return response.text


async def load_source(source: dict[str, Any], ctx: ConnCtx | None = None) -> str:
    """The text of a definition: pasted (``text``) or fetched (``url``, with the connection's auth)."""
    if source.get("text"):
        text = str(source["text"])
        if len(text.encode()) > MAX_SOURCE_BYTES:
            raise CallError("Definition is too large", 413)
        return text
    url = str(source.get("url") or "").strip()
    if not url:
        raise CallError("Provide the definition as text or a URL", 400)
    check_url(url)
    headers, params = await auth_headers_and_params(ctx) if ctx and source.get("use_auth", True) else ({}, {})
    async with client() as c:
        r = await c.get(url, headers=headers, params=params)
    if r.status_code >= 400:
        raise CallError(f"Fetching {url} failed (HTTP {r.status_code})", 502, r.text[:500])
    if len(r.content) > MAX_SOURCE_BYTES:
        raise CallError("Definition is too large", 413)
    return r.text
