"""Connection templates: well-known APIs that connect with only your credentials.

The catalog is ``app/connection_templates/templates.yaml`` (format described at its top); starter definitions for
vendors that don't publish one live next to it in ``specs/``. :func:`build_body` turns a template plus the values a
person typed into the exact body :func:`app.connections.service.create_connection` takes, so templates go through the
same preview, validation, encryption and audit as any other connection.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import yaml

APP_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = APP_DIR / "connection_templates"
CATEGORIES = {"msp": "MSP / IT", "business": "Business", "developer": "Developer", "data": "Databases & generic"}
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")


class TemplateError(ValueError):
    """A template is unknown, or required values are missing."""


@functools.lru_cache(maxsize=1)
def load() -> tuple[dict[str, Any], ...]:
    raw = yaml.safe_load((TEMPLATES_DIR / "templates.yaml").read_text(encoding="utf-8")) or []
    seen: set[str] = set()
    for t in raw:
        if t["id"] in seen:
            raise ValueError(f"Duplicate connection template id {t['id']!r}")
        seen.add(t["id"])
    return tuple(raw)


def get(template_id: str) -> dict[str, Any]:
    for t in load():
        if t["id"] == template_id:
            return t
    raise TemplateError(f"No connection template {template_id!r}")


def summary(t: dict[str, Any]) -> dict[str, Any]:
    """What the gallery shows. Never includes the auth templates (they say where secrets go, not secrets)."""
    return {
        "id": t["id"], "name": t["name"], "category": t["category"],
        "category_label": CATEGORIES.get(t["category"], t["category"]), "description": t.get("description", ""),
        "kind": t["kind"], "starter": str((t.get("source") or {}).get("file", "")).startswith("specs/"),
        "aliases": list(t.get("aliases") or []), "builtin": t.get("builtin", ""),
        "detect_auth": (t.get("auth") or {}).get("type") == "detect",
        "fields": [{"key": f["key"], "label": f["label"], "secret": bool(f.get("secret")),
                    "required": f.get("required", True), "default": f.get("default", ""),
                    "placeholder": f.get("placeholder", ""), "help": f.get("help", ""), "docs": f.get("docs", "")}
                   for f in t.get("fields") or []],
        "suggested": list(t.get("suggested") or []),
    }


def search(q: str = "", category: str = "") -> list[dict[str, Any]]:
    words = [w for w in q.lower().split() if w]
    out = []
    for t in load():
        if category and t["category"] != category:
            continue
        hay = f"{t['name']} {' '.join(t.get('aliases') or [])} {t.get('description', '')} {t['id']}".lower()
        if all(w in hay for w in words):
            out.append(summary(t))
    return out


def _fill(value: Any, values: dict[str, str], encode: bool = False) -> Any:
    if isinstance(value, dict):
        return {k: _fill(v, values, encode) for k, v in value.items()}
    if not isinstance(value, str):
        return value
    return _PLACEHOLDER.sub(lambda m: quote(values.get(m.group(1), ""), safe="") if encode
                            else values.get(m.group(1), m.group(0)), value)


def _refs(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(_refs(v) for v in value.values())) if value else set()
    return set(_PLACEHOLDER.findall(value)) if isinstance(value, str) else set()


def build_body(template_id: str, values: dict[str, Any], name: str = "",
               auth_override: dict[str, Any] | None = None) -> dict[str, Any]:
    """The ``create_connection`` body for a template. ``auth_override`` is used by templates whose sign-in is read
    from the definition (``auth: {type: detect}``), after a preview showed what it needs."""
    t = get(template_id)
    fields = t.get("fields") or []
    vals = {f["key"]: str(values.get(f["key"]) or f.get("default") or "").strip() for f in fields}
    missing = [f["label"] for f in fields if f.get("required", True) and not vals[f["key"]]]
    if missing:
        raise TemplateError(f"Fill in: {', '.join(missing)}")

    base_url = str(_fill(t.get("base_url", ""), vals)).rstrip("/")
    source: dict[str, Any] = {}
    src = t.get("source") or {}
    if "file" in src:
        source = {"text": definition_file(t).read_text(encoding="utf-8-sig")}  # some vendor files start with a BOM
    elif "url" in src:
        url = str(_fill(src["url"], vals))
        # Never send the connection's credentials to a definition hosted somewhere else (e.g. GitHub, apis.guru)
        same_host = urlparse(url).hostname and urlparse(url).hostname == urlparse(base_url).hostname
        source = {"url": url, "use_auth": bool(src.get("use_auth", same_host))}

    auth_t = dict(t.get("auth") or {"type": "none"})
    if auth_t.get("type") == "detect":
        auth = dict(auth_override or {"type": "none"})
    else:
        optional_empty = {f["key"] for f in fields if not f.get("required", True) and not vals[f["key"]]}
        refs = _refs({k: v for k, v in auth_t.items() if k != "type"})
        if refs and refs <= optional_empty:  # every value it would use was left empty → no sign-in
            auth = {"type": "none"}
        else:
            auth = {"type": auth_t["type"], **{k: _fill(v, vals) for k, v in auth_t.items() if k != "type"}}
    if t.get("dsn"):
        auth = {"type": "none", "dsn": _fill(t["dsn"], vals, encode=t["dsn"] != "{connection_string}")}
        if t["dsn"] == "{connection_string}":
            auth["dsn"] = vals.get("connection_string", "")

    settings: dict[str, Any] = {"template": template_id}
    extra = {k: v for k, v in _fill(t.get("extra_headers") or {}, vals).items() if v and not _PLACEHOLDER.search(v)}
    if extra:
        settings["extra_headers"] = extra
    name = (name or t["name"]).strip()
    body = {"kind": t["kind"], "name": name, "base_url": base_url, "source": source, "auth": auth,
            "settings": settings}
    if t.get("slug") and name == t["name"]:
        body["slug"] = t["slug"]
    return body


def definition_file(t: dict[str, Any]) -> Path:
    """A template's bundled definition (``specs/…`` here, or ``../specs/…`` — the vendor files the MSP pack uses)."""
    path = (TEMPLATES_DIR / t["source"]["file"]).resolve()
    if not path.is_relative_to(APP_DIR):
        raise TemplateError(f"Template {t['id']!r} points outside the app")
    return path


def _pairs(items: list[str]) -> list[tuple[str, str]]:
    out = []
    for s in items:
        method, _, path = s.partition(" ")
        out.append((method.upper(), path.strip()))
    return out


def suggested(template_id: str) -> list[tuple[str, str]]:
    """(METHOD, /path) pairs to pin as their own tools after saving."""
    return _pairs(get(template_id).get("suggested") or [])


def read_only(template_id: str) -> list[tuple[str, str]]:
    """(METHOD, /path) pairs that only read although they aren't GET (marked read-only after saving)."""
    return _pairs(get(template_id).get("read_only") or [])
