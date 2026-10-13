"""MongoDB → schema, example and query tools. Read-only. Needs ``pymongo`` (requirements-optional.txt).

Connection string in ``secrets.dsn`` (``mongodb://user:pass@host:27017/db`` or ``mongodb+srv://…``); the database
comes from the string or ``settings.database``. Tools: ``list_collections``, ``sample_documents``, ``find``,
``count_documents`` and ``aggregate``. Pipelines may not write (``$out``, ``$merge``) or run server-side
JavaScript (``$where``, ``$function``, ``$accumulator``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from app.connections.base import (
    CallError,
    ImportResult,
    OperationDef,
    SourceError,
    finalize,
    object_schema,
)
from app.connections.http import ConnCtx
from app.connections.netguard import check_host

MAX_DOCS = 1000
_FORBIDDEN_KEYS = {"$out", "$merge", "$where", "$function", "$accumulator"}

# Tests (or embedding apps) may replace this with a factory returning a pymongo-compatible client.
_client_factory: Callable[[str, int], Any] | None = None


def _client(dsn: str, timeout_s: int) -> Any:
    if _client_factory is not None:
        return _client_factory(dsn, timeout_s)
    try:
        from pymongo import MongoClient
    except ModuleNotFoundError as exc:
        raise CallError("MongoDB support needs the pymongo package (see requirements-optional.txt)", 501) from exc
    return MongoClient(dsn, serverSelectionTimeoutMS=timeout_s * 1000, socketTimeoutMS=timeout_s * 1000,
                       readPreference="secondaryPreferred")


def _check_dsn(dsn: str) -> None:
    if not dsn.startswith(("mongodb://", "mongodb+srv://")):
        raise SourceError("Give a connection string like mongodb://user:pass@host:27017/database")
    hosts = urlsplit(dsn).netloc.rsplit("@", 1)[-1]
    for host in hosts.split(","):
        h = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        if h and not dsn.startswith("mongodb+srv://"):
            check_host(h)


def check_pipeline(value: Any) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if k in _FORBIDDEN_KEYS:
                raise CallError(f"'{k}' is not allowed (read-only connection)", 403)
            check_pipeline(v)
    elif isinstance(value, list):
        for v in value:
            check_pipeline(v)


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


async def _run(ctx: ConnCtx, fn: Callable[[Any], Any]) -> Any:
    dsn = str((ctx.secrets or {}).get("dsn", ""))
    _check_dsn(dsn)
    timeout = int((ctx.settings or {}).get("timeout_s", 30))

    def call() -> Any:
        cli = _client(dsn, timeout)
        try:
            db_name = (ctx.settings or {}).get("database") or urlsplit(dsn).path.lstrip("/").split("?")[0]
            if not db_name:
                raise CallError("Name the database in the connection string or settings.database", 400)
            return fn(cli[db_name])
        finally:
            close = getattr(cli, "close", None)
            if close:
                close()

    try:
        return await asyncio.wait_for(asyncio.to_thread(call), timeout=timeout + 5)
    except asyncio.TimeoutError as exc:
        raise CallError(f"MongoDB took longer than {timeout} s", 504) from exc
    except CallError:
        raise
    except Exception as exc:  # noqa: BLE001  (pymongo error hierarchy)
        raise CallError(f"MongoDB error: {exc}", 502) from exc


_COLL = {"collection": {"type": "string"}}
OPERATIONS = [
    OperationDef(key="list_collections", name="list_collections", title="List collections",
                 binding={"op": "list_collections"}),
    OperationDef(key="sample_documents", name="sample_documents", title="Example documents",
                 description="A few documents from a collection, to see their shape.",
                 input_schema=object_schema({**_COLL, "limit": {"type": "integer", "default": 5, "maximum": 50}},
                                            ["collection"]),
                 binding={"op": "sample"}),
    OperationDef(key="find", name="find", title="Find documents",
                 description="Query a collection with a MongoDB filter, optional projection and sort.",
                 input_schema=object_schema({**_COLL, "filter": {"type": "object"}, "projection": {"type": "object"},
                                             "sort": {"type": "object", "description": "e.g. {\"createdAt\": -1}"},
                                             "limit": {"type": "integer", "default": 50, "maximum": MAX_DOCS}},
                                            ["collection"]),
                 binding={"op": "find"}),
    OperationDef(key="count_documents", name="count_documents", title="Count documents",
                 input_schema=object_schema({**_COLL, "filter": {"type": "object"}}, ["collection"]),
                 binding={"op": "count"}),
    OperationDef(key="aggregate", name="aggregate", title="Aggregate (read-only pipeline)",
                 input_schema=object_schema({**_COLL, "pipeline": {"type": "array", "items": {"type": "object"}},
                                             "limit": {"type": "integer", "default": 200, "maximum": MAX_DOCS}},
                                            ["collection", "pipeline"]),
                 binding={"op": "aggregate"}),
]


async def discover(ctx: ConnCtx, source: dict[str, Any]) -> ImportResult:
    collections = await _run(ctx, lambda db: db.list_collection_names())
    return ImportResult(operations=finalize([OperationDef(**op.__dict__) for op in OPERATIONS]),
                        description=f"{len(collections)} collections",
                        warnings=["Use a database user with read-only permissions"])


async def execute(ctx: ConnCtx, binding: dict[str, Any], args: dict[str, Any]) -> Any:
    op = binding["op"]
    if op == "list_collections":
        return {"collections": await _run(ctx, lambda db: sorted(db.list_collection_names()))}
    coll = str(args.get("collection") or "")
    if not coll or coll.startswith("system."):
        raise CallError("Give a (non-system) collection name", 400)
    filt = args.get("filter") or {}
    check_pipeline(filt)
    if op == "sample":
        n = max(1, min(int(args.get("limit") or 5), 50))
        return {"documents": _jsonable(await _run(ctx, lambda db: list(db[coll].find({}, limit=n))))}
    if op == "find":
        n = max(1, min(int(args.get("limit") or 50), MAX_DOCS))
        sort = list((args.get("sort") or {}).items()) or None
        return {"documents": _jsonable(await _run(ctx, lambda db: list(
            db[coll].find(filt, args.get("projection") or None, sort=sort, limit=n))))}
    if op == "count":
        return {"count": await _run(ctx, lambda db: db[coll].count_documents(filt))}
    if op == "aggregate":
        pipeline = list(args.get("pipeline") or [])
        check_pipeline(pipeline)
        n = max(1, min(int(args.get("limit") or 200), MAX_DOCS))
        return {"documents": _jsonable(await _run(ctx, lambda db: list(db[coll].aggregate(pipeline + [{"$limit": n}]))))}
    raise CallError(f"Unknown MongoDB operation {op}", 400)
