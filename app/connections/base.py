"""Types shared by every adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


class SourceError(ValueError):
    """The definition (spec, collection, WSDL, schema, …) could not be read — shown to the person importing."""


class CallError(Exception):
    """Calling an operation failed in a way worth reporting to the caller (not a bug)."""

    def __init__(self, message: str, status: int = 502, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


class UpstreamError(CallError):
    """The remote API / database answered with an error (``status`` is the remote's status code).

    Reported to callers as 502 with the remote status alongside, so e.g. a remote 401 is never mistaken
    for "you are not signed in here"."""


@dataclass
class OperationDef:
    key: str                           # stable within the connection (survives re-imports)
    name: str                          # tool-safe snake_case; made unique by ``finalize``
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    method: str = ""
    target: str = ""
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    output_schema: dict[str, Any] | None = None
    binding: dict[str, Any] = field(default_factory=dict)
    read_only: bool = True
    deprecated: bool = False

    def search_text(self) -> str:
        props = " ".join((self.input_schema or {}).get("properties", {}).keys())
        return " ".join(filter(None, [self.name, self.title, self.description, " ".join(self.tags),
                                      self.method, self.target, props])).lower()[:4000]


@dataclass
class ImportResult:
    operations: list[OperationDef]
    name: str = ""
    description: str = ""
    base_url: str = ""
    settings: dict[str, Any] = field(default_factory=dict)       # merged into the connection's settings
    auth_hint: dict[str, Any] | None = None                       # what the source says auth looks like
    warnings: list[str] = field(default_factory=list)


_NON_WORD = re.compile(r"[^0-9a-zA-Z]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def tool_name(*parts: str, max_len: int = 64) -> str:
    """``listPets`` / ``GET /pets/{id}`` → ``list_pets`` / ``get_pets_id`` (MCP tool-name safe)."""
    raw = "_".join(p for p in parts if p)
    raw = _CAMEL.sub("_", raw)
    name = _NON_WORD.sub("_", raw).strip("_").lower() or "operation"
    if name[0].isdigit():
        name = f"op_{name}"
    return name[:max_len].rstrip("_")


_READ_PREFIXES = ("get", "list", "find", "search", "query", "read", "fetch", "lookup", "check", "validate",
                  "count", "describe", "show", "view", "export", "download", "retrieve", "is_", "has_")


def looks_read_only(name: str) -> bool:
    """Guess for protocols without HTTP verbs (SOAP, JSON-RPC): read verbs → read-only, anything else → write."""
    n = tool_name(name)
    return n.startswith(_READ_PREFIXES)


def finalize(ops: list[OperationDef]) -> list[OperationDef]:
    """Drop duplicate keys and make names unique (``name``, ``name_2`` …)."""
    seen_keys: set[str] = set()
    used: set[str] = set()
    out: list[OperationDef] = []
    for op in ops:
        if op.key in seen_keys:
            continue
        seen_keys.add(op.key)
        base = op.name or "operation"
        name, n = base, 2
        while name in used:
            suffix = f"_{n}"
            name = base[: 64 - len(suffix)] + suffix
            n += 1
        used.add(name)
        op.name = name
        out.append(op)
    return out


def object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    req = [r for r in (required or []) if r in properties]
    if req:
        schema["required"] = req
    return schema
