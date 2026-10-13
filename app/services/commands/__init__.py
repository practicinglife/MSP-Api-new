"""Command catalog: named, validated write operations that produce change-request plans.

A command never writes. ``plan(params)`` returns the exact steps (and the current "before" state where it
can be read). Steps run only after an Editor submits and the Approver gives final approval
(``app/services/changes.py``).

Step shapes:
* ``{"kind": "http", "platform", "method", "path", "params", "body", "description",
     "before": <snapshot>, "before_ref": {"platform", "path", "params"}, "compare_keys": [...]}``
* ``{"kind": "action", "action": <name in actions.ACTIONS>, "args": {...}, "description",
     "before": <snapshot>, "before_ref": {"action": <reader>, "args": {...}}}``
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


class CommandError(ValueError):
    """Invalid parameters or a plan that can't be built (shown to the caller)."""


@dataclass
class Plan:
    title: str
    steps: list[dict[str, Any]]
    risk: str = "update"  # create | update | delete
    client_id: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class Command:
    name: str
    title: str
    description: str
    risk: str
    properties: dict[str, Any]
    required: list[str]
    plan: Callable[[dict[str, Any]], Awaitable[Plan]]
    platforms: list[str] = field(default_factory=list)

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": self.properties, "required": self.required,
                "additionalProperties": False}

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title, "description": self.description, "risk": self.risk,
                "platforms": self.platforms, "params": self.schema()}


REGISTRY: dict[str, Command] = {}


def register(cmd: Command) -> Command:
    REGISTRY[cmd.name] = cmd
    return cmd


def validate_params(cmd: Command, params: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise CommandError("params must be an object")
    missing = [k for k in cmd.required if params.get(k) in (None, "", [])]
    if missing:
        raise CommandError(f"Missing: {', '.join(missing)}")
    unknown = [k for k in params if k not in cmd.properties]
    if unknown:
        raise CommandError(f"Unknown parameter(s): {', '.join(unknown)}")
    types = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list}
    for k, v in params.items():
        spec = cmd.properties[k]
        t = spec.get("type")
        if t and t in types and v is not None:
            if t == "integer" and isinstance(v, bool):
                raise CommandError(f"{k} must be an integer")
            if not isinstance(v, types[t]):
                raise CommandError(f"{k} must be {t}")
        if "enum" in spec and v not in spec["enum"]:
            raise CommandError(f"{k} must be one of: {', '.join(map(str, spec['enum']))}")
        if t == "string" and isinstance(v, str) and len(v) > spec.get("maxLength", 4000):
            raise CommandError(f"{k} is too long")
    return params


_LOADED = False


def _load() -> None:
    # Import for side effects (each module registers its commands). Tracked by a flag, not by REGISTRY being
    # empty: a module imported elsewhere first (e.g. onboarding by its API) would otherwise skip the rest.
    # The core only has connection_call (approval-mode writes); the MSP pack adds its vendor commands.
    global _LOADED
    from app import packs
    from app.services.commands import connections  # noqa: F401

    if packs.is_active("msp"):
        from app.services.commands import (  # noqa: F401
            av, builtin, discrepancy, helpdesk, library, offboarding, onboarding, playbooks,
        )
    _LOADED = True


def get_command(name: str) -> Command:
    if not _LOADED:
        _load()
    cmd = REGISTRY.get(name)
    if not cmd:
        raise CommandError(f"Unknown command: {name}")
    return cmd


def list_commands() -> list[dict[str, Any]]:
    if not _LOADED:
        _load()
    return [c.describe() for c in sorted(REGISTRY.values(), key=lambda c: c.name)]
