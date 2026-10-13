"""One module per connection kind. Each exposes ``discover(ctx, source) -> ImportResult`` and
``execute(ctx, op_binding, args) -> Any`` (REST-shaped kinds share ``rest.execute``)."""

from __future__ import annotations

from types import ModuleType


def get_adapter(kind: str) -> ModuleType:
    from app.connections.adapters import (
        curl,
        graphql,
        mcp,
        mongodb,
        odata,
        openapi,
        postman,
        soap,
        sql,
    )

    adapters: dict[str, ModuleType] = {
        "openapi": openapi, "postman": postman, "curl": curl, "soap": soap, "graphql": graphql,
        "odata": odata, "sql": sql, "mongodb": mongodb, "mcp": mcp,
    }
    if kind not in adapters:
        raise ValueError(f"Unknown connection kind '{kind}'. Use one of: {', '.join(sorted(adapters))}")
    return adapters[kind]


KINDS = ("openapi", "postman", "curl", "soap", "graphql", "odata", "sql", "mongodb", "mcp")
