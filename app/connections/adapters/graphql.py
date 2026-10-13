"""GraphQL → operations.

Three ways in:
1. **Introspection** — leave the definition empty (or give the endpoint URL): the endpoint is asked for its
   schema; every Query field becomes a read tool and every Mutation field a write tool.
2. **Schema text** — paste SDL (``type Query { … }``) or an introspection JSON result.
3. **Your own operations** — paste documents (``query OpenTickets($n: Int) { … }``); each named operation
   becomes a tool with its variables as arguments.

Generated documents select scalar fields of the result type and nested objects up to
``settings.selection_depth`` (default 2).
"""

from __future__ import annotations

import json
from typing import Any

from graphql import (
    GraphQLEnumType,
    GraphQLInputObjectType,
    GraphQLInterfaceType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    GraphQLUnionType,
    OperationDefinitionNode,
    build_client_schema,
    build_schema,
    get_introspection_query,
    parse,
    print_ast,
)
from graphql.error import GraphQLError
from graphql.language import FragmentDefinitionNode, ListTypeNode, NonNullTypeNode
from graphql.pyutils import Undefined

from app.connections.base import (
    CallError,
    ImportResult,
    OperationDef,
    SourceError,
    UpstreamError,
    finalize,
    object_schema,
    tool_name,
)
from app.connections.http import ConnCtx, auth_headers_and_params, client, decode_body

_SCALARS = {"Int": {"type": "integer"}, "Float": {"type": "number"}, "Boolean": {"type": "boolean"},
            "String": {"type": "string"}, "ID": {"type": "string"}}
MAX_INPUT_DEPTH = 4
MAX_FIELDS = 60


def _named(t: Any) -> Any:
    while isinstance(t, (GraphQLNonNull, GraphQLList)):
        t = t.of_type
    return t


def _input_schema(t: Any, depth: int = 0) -> dict[str, Any]:
    if isinstance(t, GraphQLNonNull):
        return _input_schema(t.of_type, depth)
    if isinstance(t, GraphQLList):
        return {"type": "array", "items": _input_schema(t.of_type, depth + 1)}
    if isinstance(t, GraphQLEnumType):
        return {"type": "string", "enum": list(t.values.keys())}
    if isinstance(t, GraphQLScalarType):
        return dict(_SCALARS.get(t.name, {"description": f"{t.name} (custom scalar)"}))
    if isinstance(t, GraphQLInputObjectType):
        if depth >= MAX_INPUT_DEPTH:
            return {"type": "object", "description": f"{t.name} (nested input)"}
        props, req = {}, []
        for name, f in list(t.fields.items())[:MAX_FIELDS]:
            props[name] = _input_schema(f.type, depth + 1)
            if f.description:
                props[name]["description"] = f.description[:500]
            if isinstance(f.type, GraphQLNonNull) and f.default_value is Undefined:
                req.append(name)
        return object_schema(props, req)
    return {}


def _selection(t: Any, depth: int) -> str:
    named = _named(t)
    if isinstance(named, (GraphQLScalarType, GraphQLEnumType)):
        return ""
    if isinstance(named, GraphQLUnionType):
        parts = ["__typename"]
        if depth > 0:
            for member in list(named.types)[:10]:
                inner = _selection(member, depth - 1)
                if inner:
                    parts.append(f"... on {member.name} {inner}")
        return "{ " + " ".join(parts) + " }"
    if isinstance(named, (GraphQLObjectType, GraphQLInterfaceType)):
        parts = []
        for fname, f in list(named.fields.items())[:MAX_FIELDS]:
            if any(isinstance(a.type, GraphQLNonNull) and a.default_value is Undefined for a in f.args.values()):
                continue  # needs arguments we don't have
            sub = _named(f.type)
            if isinstance(sub, (GraphQLScalarType, GraphQLEnumType)):
                parts.append(fname)
            elif depth > 0:
                inner = _selection(f.type, depth - 1)
                if inner:
                    parts.append(f"{fname} {inner}")
        return "{ " + " ".join(parts or ["__typename"]) + " }"
    return ""


def _ops_from_schema(schema: GraphQLSchema, selection_depth: int) -> list[OperationDef]:
    ops: list[OperationDef] = []
    for kind, root, read_only in (("query", schema.query_type, True), ("mutation", schema.mutation_type, False)):
        if root is None:
            continue
        for fname, field in root.fields.items():
            op_name = tool_name(f"{kind}_{fname}" if kind == "mutation" else fname)
            graphql_name = "".join(p.capitalize() for p in op_name.split("_")) or "Op"
            var_defs = ", ".join(f"${a}: {arg.type}" for a, arg in field.args.items())
            call_args = ", ".join(f"{a}: ${a}" for a in field.args)
            doc = (f"{kind} {graphql_name}{f'({var_defs})' if var_defs else ''} "
                   f"{{ {fname}{f'({call_args})' if call_args else ''} {_selection(field.type, selection_depth)} }}")
            props, req = {}, []
            for a, arg in field.args.items():
                props[a] = _input_schema(arg.type)
                if arg.description:
                    props[a]["description"] = arg.description[:500]
                if isinstance(arg.type, GraphQLNonNull) and arg.default_value is Undefined:
                    req.append(a)
            ops.append(OperationDef(
                key=f"{kind.upper()} {fname}", name=op_name, title=f"{kind} {fname}",
                description=(field.description or "")[:2000], method=kind.upper(), target=fname,
                input_schema=object_schema(props, req),
                binding={"document": doc, "operation_name": graphql_name, "field": fname},
                read_only=read_only, deprecated=bool(field.deprecation_reason),
            ))
    return ops


def _type_node_schema(node: Any) -> dict[str, Any]:
    if isinstance(node, NonNullTypeNode):
        return _type_node_schema(node.type)
    if isinstance(node, ListTypeNode):
        return {"type": "array", "items": _type_node_schema(node.type)}
    return dict(_SCALARS.get(node.name.value, {"description": f"{node.name.value}"}))


def _ops_from_documents(text: str) -> list[OperationDef]:
    try:
        document = parse(text)
    except GraphQLError as exc:
        raise SourceError(f"Not valid GraphQL: {exc.message}") from exc
    fragments = [d for d in document.definitions if isinstance(d, FragmentDefinitionNode)]
    ops: list[OperationDef] = []
    for d in document.definitions:
        if not isinstance(d, OperationDefinitionNode):
            continue
        if not d.name:
            raise SourceError("Give every operation a name, e.g. 'query OpenTickets { … }'")
        kind = d.operation.value
        if kind == "subscription":
            continue
        props, req = {}, []
        for v in d.variable_definitions or []:
            name = v.variable.name.value
            props[name] = _type_node_schema(v.type)
            if isinstance(v.type, NonNullTypeNode) and v.default_value is None:  # AST: absent default is None
                req.append(name)
        doc = "\n".join([print_ast(d)] + [print_ast(f) for f in fragments])
        ops.append(OperationDef(
            key=f"{kind.upper()} {d.name.value}", name=tool_name(d.name.value), title=d.name.value,
            method=kind.upper(), target=d.name.value, input_schema=object_schema(props, req),
            binding={"document": doc, "operation_name": d.name.value}, read_only=kind == "query",
        ))
    return ops


async def _post(ctx: ConnCtx, url: str, payload: dict[str, Any]) -> Any:
    headers, params = await auth_headers_and_params(ctx)
    async with client() as c:
        r = await c.post(url, json=payload, headers={**headers, "Accept": "application/json"}, params=params or None)
    body = decode_body(r)
    if r.status_code >= 400 and not isinstance(body, dict):
        raise UpstreamError(f"GraphQL endpoint returned HTTP {r.status_code}", r.status_code, body)
    return body


async def discover(ctx: ConnCtx, source: dict[str, Any]) -> ImportResult:
    text = str(source.get("text") or "").strip()
    endpoint = str(source.get("url") or ctx.base_url or "").strip()
    depth = int((ctx.settings or {}).get("selection_depth", 2))
    if text:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            schema_json = data.get("data", data)
            if "__schema" not in schema_json:
                raise SourceError("JSON given, but it is not an introspection result (no __schema)")
            schema = build_client_schema(schema_json)
            ops = _ops_from_schema(schema, depth)
        elif "type " in text and ("type Query" in text or "schema " in text or "type Mutation" in text):
            try:
                schema = build_schema(text)
            except GraphQLError as exc:
                raise SourceError(f"Not a valid GraphQL schema: {exc.message}") from exc
            ops = _ops_from_schema(schema, depth)
        else:
            ops = _ops_from_documents(text)
        return ImportResult(operations=finalize(ops), base_url=ctx.base_url or "")
    if not endpoint:
        raise SourceError("Give the GraphQL endpoint URL (to introspect) or paste a schema / operations")
    body = await _post(ctx, endpoint, {"query": get_introspection_query(descriptions=True)})
    if not isinstance(body, dict) or not isinstance(body.get("data"), dict) or "__schema" not in body["data"]:
        raise SourceError("Introspection failed — the endpoint may have it disabled; paste the schema instead")
    ops = _ops_from_schema(build_client_schema(body["data"]), depth)
    return ImportResult(operations=finalize(ops), base_url=endpoint)


async def execute(ctx: ConnCtx, binding: dict[str, Any], args: dict[str, Any]) -> Any:
    if not ctx.base_url:
        raise CallError("This GraphQL connection has no endpoint URL", 400)
    payload = {"query": binding["document"], "variables": args or {},
               "operationName": binding.get("operation_name")}
    body = await _post(ctx, ctx.base_url, payload)
    if not isinstance(body, dict):
        raise CallError("GraphQL endpoint did not return JSON", 502, body)
    errors = body.get("errors")
    data = body.get("data")
    if errors and not data:
        raise UpstreamError("GraphQL returned errors", 200, errors)
    field = binding.get("field")
    result = data.get(field) if field and isinstance(data, dict) else data
    return {"data": result, **({"errors": errors} if errors else {})}
