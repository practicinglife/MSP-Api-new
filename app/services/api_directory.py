"""Public API directory: search the APIs.guru catalog (~2,500 public OpenAPI definitions) to add an API that has no
connection template.

The list is downloaded on first use and cached in ``data/api-directory.json`` for a day. Set
``MSP_API_DIRECTORY=off`` to turn it off (e.g. on servers without internet access).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import structlog
from rapidfuzz import fuzz

from app.runtime import data_dir

log = structlog.get_logger(__name__)

LIST_URL = "https://api.apis.guru/v2/list.json"
CACHE_SECONDS = 24 * 3600
_memory: tuple[float, list[dict[str, Any]]] | None = None


class DirectoryUnavailable(RuntimeError):
    """The directory is turned off or couldn't be downloaded."""


def enabled() -> bool:
    return os.environ.get("MSP_API_DIRECTORY", "on").strip().lower() not in ("off", "0", "false", "no")


def _entries(raw: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for api_id, api in raw.items():
        version = (api.get("versions") or {}).get(api.get("preferred") or "") or {}
        info = version.get("info") or {}
        spec_url = version.get("swaggerUrl") or ""
        if not spec_url:
            continue
        provider, _, service = api_id.partition(":")
        out.append({
            "id": api_id, "title": str(info.get("title") or api_id), "provider": provider, "service": service,
            "description": " ".join(str(info.get("description") or "").split())[:300],
            "categories": list(info.get("x-apisguru-categories") or []),
            "logo": str((info.get("x-logo") or {}).get("url") or ""), "spec_url": spec_url,
            "openapi_version": str(version.get("openapiVer") or ""),
        })
    return out


async def _download() -> dict[str, Any]:
    from app.connections.http import client
    from app.connections.netguard import check_url

    check_url(LIST_URL)
    async with client() as c:
        r = await c.get(LIST_URL, timeout=60)
    r.raise_for_status()
    return r.json()


async def entries() -> list[dict[str, Any]]:
    """The directory, from memory, the cache file, or a fresh download (in that order)."""
    global _memory
    if not enabled():
        raise DirectoryUnavailable("The public API directory is turned off on this server (MSP_API_DIRECTORY=off)")
    now = time.time()
    if _memory and now - _memory[0] < CACHE_SECONDS:
        return _memory[1]
    cache = data_dir() / "api-directory.json"
    try:
        if cache.exists() and now - cache.stat().st_mtime < CACHE_SECONDS:
            _memory = (cache.stat().st_mtime, json.loads(cache.read_text(encoding="utf-8")))
            return _memory[1]
    except (OSError, ValueError):
        pass
    try:
        items = _entries(await _download())
    except Exception as exc:  # noqa: BLE001
        if cache.exists():  # stale beats nothing
            log.warning("api_directory.refresh_failed_using_cache", error=str(exc))
            _memory = (now, json.loads(cache.read_text(encoding="utf-8")))
            return _memory[1]
        raise DirectoryUnavailable(f"Couldn't download the public API directory ({exc}). Is this server online?") \
            from exc
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(items), encoding="utf-8")
    except OSError:
        pass
    _memory = (now, items)
    log.info("api_directory.loaded", apis=len(items))
    return items


async def search(q: str, limit: int = 30) -> list[dict[str, Any]]:
    q = q.strip().lower()
    items = await entries()
    if not q:
        return items[:limit]
    scored = []
    for e in items:
        title, provider = e["title"].lower(), e["provider"].lower()
        if q in provider or q in title:
            score = 100 + (20 if provider.startswith(q) or title.startswith(q) else 0)
        else:
            score = max(fuzz.partial_ratio(q, title), fuzz.partial_ratio(q, provider),
                        fuzz.partial_ratio(q, e["description"].lower()) - 15)
        if score >= 70:
            scored.append((score, e))
    scored.sort(key=lambda s: (-s[0], s[1]["title"]))
    return [e for _, e in scored[:limit]]
