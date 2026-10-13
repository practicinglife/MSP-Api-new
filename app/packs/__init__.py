"""Optional feature packs.

The **core** is what every install gets: connections to any API, one MCP server per connection plus the hub,
local copies, search, sign-in, keys / OAuth, write modes, change approvals and the audit log.

A **pack** adds a whole domain on top. Packs are off unless listed in ``MSP_PACKS`` (comma separated), e.g.
``MSP_PACKS=msp``, or chosen in the Get started wizard (saved in ``data/settings.json``; ``MSP_PACKS`` wins).
A fresh install starts with none.

Each pack is a module with:

* ``TITLE`` / ``DESCRIPTION``
* ``register(app)`` — include its routers and pages (called once while the app is built)
* ``async startup(app)`` / ``async shutdown(app)`` — start / stop its background work (optional)
* ``NAV`` — links for the UI: ``[{"href": "/", "label": "Dashboard"}]`` (optional)
"""

from __future__ import annotations

import importlib
import os
from types import ModuleType
from typing import Any

import structlog

log = structlog.get_logger(__name__)

AVAILABLE: dict[str, str] = {"msp": "app.packs.msp"}

_active: set[str] = set()


def from_env() -> bool:
    """True when MSP_PACKS is set (even empty): the environment decides, not the Get started wizard."""
    return "MSP_PACKS" in os.environ


def chosen() -> bool:
    """Has this install decided which packs it uses (in .env, or in the Get started wizard)?"""
    from app.runtime import load_settings

    return from_env() or "packs" in load_settings()


def configured() -> list[str]:
    """Packs named in MSP_PACKS, else chosen in the Get started wizard (unknown names are logged and ignored)."""
    if from_env():
        raw = os.environ.get("MSP_PACKS", "")
    else:
        from app.runtime import load_settings

        raw = ",".join(load_settings().get("packs") or [])
    names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    for n in names:
        if n not in AVAILABLE and n not in ("none", "off"):
            log.warning("packs.unknown", pack=n, available=sorted(AVAILABLE))
    return [n for n in dict.fromkeys(names) if n in AVAILABLE]


def set_active(names: list[str] | set[str]) -> None:
    _active.clear()
    _active.update(names)


def active() -> set[str]:
    return set(_active)


def is_active(name: str) -> bool:
    return name in _active


def module(name: str) -> ModuleType:
    return importlib.import_module(AVAILABLE[name])


def describe() -> dict[str, Any]:
    out = []
    for name in AVAILABLE:
        try:
            m = module(name)
            title, desc, nav = getattr(m, "TITLE", name), getattr(m, "DESCRIPTION", ""), getattr(m, "NAV", [])
        except Exception as exc:  # noqa: BLE001
            title, desc, nav = name, f"Unavailable: {exc}", []
        out.append({"name": name, "title": title, "description": desc, "enabled": name in _active,
                    "nav": nav if name in _active else []})
    return {"enabled": sorted(_active), "packs": out, "chosen": chosen(), "from_env": from_env(),
            "how_to_enable": "Choose in Get started (/setup), or set MSP_PACKS=<name>[,<name>] in .env and restart."}
