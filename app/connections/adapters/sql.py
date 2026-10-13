"""SQL databases → schema, example and query tools. Read-only.

Built in: PostgreSQL (``postgresql://``), SQLite (``sqlite:///path``). With optional drivers
(``requirements-optional.txt``): MySQL / MariaDB, SQL Server, Oracle, SAP HANA.

The connection string is a secret (``secrets.dsn``). Tools: ``list_tables``, ``describe_table``,
``sample_rows`` and ``run_query``; paste named queries as the definition (YAML/JSON list of
``{name, sql, description}``, parameters written ``:name``) to get one tool per query.

Read-only is enforced three ways: a statement check (single SELECT/WITH/EXPLAIN/SHOW, no write keywords or
file/remote-execution functions), a read-only transaction where the engine supports it, and a rollback.
Still: give the connection a database user that can only read.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any

import yaml
from sqlalchemy import create_engine, inspect, select, table, text
from sqlalchemy.engine import Connection as SyncConnection
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, NoSuchModuleError, SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.connections.base import (
    CallError,
    ImportResult,
    OperationDef,
    SourceError,
    finalize,
    object_schema,
    tool_name,
)
from app.connections.http import ConnCtx, load_source
from app.connections.netguard import check_host

_SCHEMES = {
    "postgres": "postgresql+asyncpg", "postgresql": "postgresql+asyncpg",
    "sqlite": "sqlite+aiosqlite",
    "mysql": "mysql+pymysql", "mariadb": "mariadb+pymysql",
    "mssql": "mssql+pyodbc", "sqlserver": "mssql+pyodbc",
    "oracle": "oracle+oracledb",
    "hana": "hana",
}
_ASYNC_DRIVERS = {"asyncpg", "aiosqlite", "aiomysql", "asyncmy", "aioodbc", "psycopg_async"}
_ALLOWED_FIRST = {"select", "with", "show", "describe", "desc", "explain", "values", "table"}
# Write keywords anywhere (also inside CTEs: WITH x AS (DELETE … RETURNING …)), plus functions that read
# server files, run remote/OS commands, change settings or stall the server.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|upsert|drop|alter|create|truncate|grant|revoke|exec|execute|call|copy|into|"
    r"rename|attach|detach|vacuum|commit|rollback|outfile|dumpfile|waitfor)\b"
    r"|\b(pg_read_file|pg_read_binary_file|pg_ls_dir|pg_stat_file|lo_import|lo_export|dblink\w*|"
    r"pg_terminate_backend|pg_cancel_backend|pg_sleep|set_config|xp_\w+|sp_\w+|openrowset|opendatasource|"
    r"openquery|load_file|sleep|benchmark|utl_\w+|dbms_\w+)\s*\(", re.I)
MAX_ROWS = 5000


def normalize_dsn(dsn: str) -> tuple[str, bool]:
    """Return (sqlalchemy URL, is_async)."""
    dsn = (dsn or "").strip()
    if not dsn or "://" not in dsn:
        raise SourceError("Give a connection string like postgresql://user:pass@host:5432/db")
    scheme, rest = dsn.split("://", 1)
    if "+" not in scheme:
        if scheme.lower() not in _SCHEMES:
            raise SourceError(f"Unsupported database '{scheme}'. Use one of: {', '.join(sorted(_SCHEMES))}")
        scheme = _SCHEMES[scheme.lower()]
    url = f"{scheme}://{rest}"
    try:
        parsed = make_url(url)
    except ArgumentError as exc:
        raise SourceError(f"Invalid connection string: {exc}") from exc
    if parsed.get_backend_name() != "sqlite" and parsed.host:
        check_host(parsed.host)
    return url, (parsed.get_driver_name() in _ASYNC_DRIVERS)


def _strip(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)      # string literals can't smuggle keywords
    sql = re.sub(r"\$(\w*)\$.*?\$\1\$", "''", sql, flags=re.S)  # Postgres dollar-quoted strings
    return sql


def check_read_only(sql: str) -> None:
    body = _strip(sql).strip().rstrip(";").strip()
    if not body:
        raise CallError("Empty query", 400)
    if ";" in body:
        raise CallError("One statement at a time", 400)
    first = body.split(None, 1)[0].lower().lstrip("(")
    if first not in _ALLOWED_FIRST:
        raise CallError(f"Only read queries are allowed (SELECT / WITH / EXPLAIN / SHOW), not '{first.upper()}'", 403)
    bad = _FORBIDDEN.search(body)
    if bad:
        raise CallError(f"'{bad.group(0)}' is not allowed in a read-only query", 403)


def _begin_read_only(conn: SyncConnection, timeout_s: int) -> None:
    dialect = conn.dialect.name
    try:
        if dialect == "postgresql":
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(timeout_s) * 1000}")
        elif dialect in ("mysql", "mariadb"):
            conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
        elif dialect == "sqlite":
            conn.exec_driver_sql("PRAGMA query_only = ON")
        elif dialect == "oracle":
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
    except SQLAlchemyError:
        pass  # the statement check still applies


def _jsonable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return json.loads(json.dumps(rows, default=lambda o: o.hex() if isinstance(o, (bytes, bytearray)) else str(o)))


async def _run(ctx: ConnCtx, fn: Callable[[SyncConnection], Any]) -> Any:
    url, is_async = normalize_dsn(str((ctx.secrets or {}).get("dsn", "")))
    timeout = int((ctx.settings or {}).get("timeout_s", 30))
    connect_args = {"timeout": timeout} if url.startswith("sqlite") else {}

    def wrapped(conn: SyncConnection) -> Any:
        _begin_read_only(conn, timeout)
        try:
            return fn(conn)
        finally:
            conn.rollback()

    try:
        if is_async:
            engine = create_async_engine(url, poolclass=NullPool, connect_args=connect_args)
            try:
                async with engine.connect() as aconn:
                    return await asyncio.wait_for(aconn.run_sync(wrapped), timeout=timeout + 5)
            finally:
                await engine.dispose()

        def sync_call() -> Any:
            engine = create_engine(url, poolclass=NullPool)
            try:
                with engine.connect() as conn:
                    return wrapped(conn)
            finally:
                engine.dispose()

        return await asyncio.wait_for(asyncio.to_thread(sync_call), timeout=timeout + 5)
    except NoSuchModuleError as exc:
        raise CallError(f"The database driver is not installed: {exc} (see requirements-optional.txt)", 501) from exc
    except ModuleNotFoundError as exc:
        raise CallError(f"The database driver is not installed: {exc.name} (see requirements-optional.txt)", 501) from exc
    except asyncio.TimeoutError as exc:
        raise CallError(f"Query took longer than {timeout} s", 504) from exc
    except SQLAlchemyError as exc:
        raise CallError(f"Database error: {getattr(exc, 'orig', exc)}", 502) from exc


def _query(sql: str, params: dict[str, Any], limit: int) -> Callable[[SyncConnection], Any]:
    def fn(conn: SyncConnection) -> Any:
        result = conn.execute(text(sql), params)
        rows = [dict(r) for r in result.mappings().fetchmany(limit + 1)]
        return {"rows": _jsonable(rows[:limit]), "row_count": min(len(rows), limit), "truncated": len(rows) > limit}

    return fn


BUILTIN = [
    OperationDef(key="list_tables", name="list_tables", title="List tables and views",
                 description="Tables and views in the database (optionally in one schema).",
                 input_schema=object_schema({"schema": {"type": "string"}}),
                 binding={"op": "list_tables"}),
    OperationDef(key="describe_table", name="describe_table", title="Describe a table",
                 description="Columns, types, primary key, foreign keys and indexes of a table.",
                 input_schema=object_schema({"table": {"type": "string"}, "schema": {"type": "string"}}, ["table"]),
                 binding={"op": "describe_table"}),
    OperationDef(key="sample_rows", name="sample_rows", title="Example rows",
                 description="A few example rows from a table, to see what the data looks like.",
                 input_schema=object_schema({"table": {"type": "string"}, "schema": {"type": "string"},
                                             "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 50}},
                                            ["table"]),
                 binding={"op": "sample_rows"}),
    OperationDef(key="run_query", name="run_query", title="Run a read-only SQL query",
                 description="Run one SELECT / WITH query. Use :name placeholders with 'params' for values.",
                 input_schema=object_schema({"sql": {"type": "string"},
                                             "params": {"type": "object", "description": "Values for :name placeholders"},
                                             "limit": {"type": "integer", "default": 200, "minimum": 1, "maximum": MAX_ROWS}},
                                            ["sql"]),
                 binding={"op": "run_query"}),
]


def _saved_queries(text_: str) -> list[OperationDef]:
    try:
        doc = yaml.safe_load(text_)
    except yaml.YAMLError as exc:
        raise SourceError(f"Saved queries must be YAML or JSON: {exc}") from exc
    if isinstance(doc, dict):
        doc = doc.get("queries", [doc])
    if not isinstance(doc, list):
        raise SourceError("Saved queries: a list of {name, sql, description}")
    ops = []
    for q in doc:
        if not isinstance(q, dict) or not q.get("name") or not q.get("sql"):
            raise SourceError("Each saved query needs a name and sql")
        check_read_only(str(q["sql"]))
        params = sorted(set(re.findall(r"(?<!:):([A-Za-z_]\w*)", _strip(str(q["sql"])))))
        ops.append(OperationDef(
            key=f"query {q['name']}", name=tool_name(str(q["name"])), title=str(q["name"]),
            description=str(q.get("description") or "")[:2000], method="QUERY", target=str(q["name"]),
            input_schema=object_schema({p: {"type": "string"} for p in params}
                                       | {"limit": {"type": "integer", "default": 200, "maximum": MAX_ROWS}}, params),
            binding={"op": "saved", "sql": str(q["sql"])}))
    return ops


async def discover(ctx: ConnCtx, source: dict[str, Any]) -> ImportResult:
    normalize_dsn(str((ctx.secrets or {}).get("dsn", "")))  # validates the string + target host
    ops = [OperationDef(**{**op.__dict__}) for op in BUILTIN]
    if source.get("text") or source.get("url"):
        ops += _saved_queries(await load_source(source, ctx))
    tables = await _run(ctx, lambda c: inspect(c).get_table_names())  # proves the connection works
    return ImportResult(operations=finalize(ops), description=f"{len(tables)} tables",
                        warnings=["Use a database user with read-only permissions"])


async def execute(ctx: ConnCtx, binding: dict[str, Any], args: dict[str, Any]) -> Any:
    op = binding["op"]
    row_limit = min(int((ctx.settings or {}).get("row_limit", 200)), MAX_ROWS)
    if op == "list_tables":
        schema = args.get("schema")
        return await _run(ctx, lambda c: {"tables": inspect(c).get_table_names(schema=schema),
                                          "views": inspect(c).get_view_names(schema=schema)})
    if op == "describe_table":
        name, schema = args["table"], args.get("schema")

        def describe(c: SyncConnection) -> Any:
            insp = inspect(c)
            cols = [{"name": col["name"], "type": str(col["type"]), "nullable": col.get("nullable"),
                     "default": None if col.get("default") is None else str(col["default"])}
                    for col in insp.get_columns(name, schema=schema)]
            return {"table": name, "columns": cols,
                    "primary_key": insp.get_pk_constraint(name, schema=schema).get("constrained_columns"),
                    "foreign_keys": [{"columns": fk["constrained_columns"], "references":
                                      f"{fk['referred_table']}({', '.join(fk['referred_columns'])})"}
                                     for fk in insp.get_foreign_keys(name, schema=schema)],
                    "indexes": [{"name": ix["name"], "columns": ix["column_names"], "unique": ix.get("unique")}
                                for ix in insp.get_indexes(name, schema=schema)]}

        return await _run(ctx, describe)
    if op == "sample_rows":
        limit = max(1, min(int(args.get("limit") or 5), 50))
        tbl = table(args["table"], schema=args.get("schema"))
        stmt = select(text("*")).select_from(tbl).limit(limit)
        return await _run(ctx, lambda c: {"rows": _jsonable([dict(r) for r in c.execute(stmt).mappings().fetchmany(limit)])})
    if op in ("run_query", "saved"):
        sql = binding["sql"] if op == "saved" else str(args.get("sql") or "")
        check_read_only(sql)
        params = dict(args.get("params") or {}) if op == "run_query" else {k: v for k, v in args.items() if k != "limit"}
        limit = max(1, min(int(args.get("limit") or row_limit), MAX_ROWS))
        return await _run(ctx, _query(sql, params, limit))
    raise CallError(f"Unknown SQL operation {op}", 400)
