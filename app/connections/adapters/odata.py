"""OData V2 and V4 (SAP Gateway included) → operations, from the service's ``$metadata``.

Per entity set: ``list_<set>`` ($filter/$select/$orderby/$top/$skip/$expand), ``get_<set>`` (by key) and — unless
SAP marks the set ``creatable/updatable/deletable="false"`` — ``create_``, ``update_`` and ``delete_``.
Function imports (V2) and function/action imports (V4) become tools too. SAP ``sap:label`` and V4
``Core.Description`` annotations become descriptions so the model sees business names, not technical ones.
Writes fetch an ``x-csrf-token`` first (SAP Gateway requires it; ``settings.csrf: false`` turns it off).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from defusedxml import ElementTree

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
from app.connections.http import (
    ConnCtx,
    auth_headers_and_params,
    client,
    decode_body,
    join_url,
    load_source,
)

_EDM_SCHEMA: dict[str, dict[str, Any]] = {
    "Edm.String": {"type": "string"}, "Edm.Boolean": {"type": "boolean"},
    "Edm.Byte": {"type": "integer"}, "Edm.SByte": {"type": "integer"}, "Edm.Int16": {"type": "integer"},
    "Edm.Int32": {"type": "integer"}, "Edm.Int64": {"type": ["integer", "string"]},
    "Edm.Decimal": {"type": ["number", "string"]}, "Edm.Double": {"type": "number"}, "Edm.Single": {"type": "number"},
    "Edm.Guid": {"type": "string", "format": "uuid"}, "Edm.DateTime": {"type": "string", "format": "date-time"},
    "Edm.DateTimeOffset": {"type": "string", "format": "date-time"}, "Edm.Date": {"type": "string", "format": "date"},
    "Edm.Time": {"type": "string"}, "Edm.TimeOfDay": {"type": "string"}, "Edm.Binary": {"type": "string"},
}
MAX_PROPS = 120


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(el: Any, name: str) -> str | None:
    """Attribute by local name (namespaced SAP / metadata attributes included)."""
    for k, v in el.attrib.items():
        if _local(k) == name:
            return v
    return None


def _children(el: Any, name: str) -> list[Any]:
    return [c for c in el if _local(c.tag) == name]


def _description(el: Any) -> str:
    label = _attr(el, "label") or _attr(el, "quickinfo") or ""
    for ann in _children(el, "Annotation"):
        if (ann.get("Term") or "").endswith(("Core.Description", "Common.Label")):
            label = label or ann.get("String") or ""
    for doc in _children(el, "Documentation"):
        for s in doc:
            label = label or (s.text or "").strip()
    return label.strip()[:500]


def _schema_for(type_name: str, label: str = "") -> dict[str, Any]:
    if type_name.startswith("Collection("):
        out: dict[str, Any] = {"type": "array", "items": _schema_for(type_name[11:-1])}
    else:
        out = dict(_EDM_SCHEMA.get(type_name, {"type": "object", "description": type_name}))
    if label:
        out["description"] = label
    return out


def parse_metadata(xml_text: str) -> dict[str, Any]:
    try:
        root = ElementTree.fromstring(xml_text.encode() if isinstance(xml_text, str) else xml_text)
    except Exception as exc:  # noqa: BLE001  (defusedxml raises several types)
        raise SourceError(f"Not valid OData $metadata XML: {exc}") from exc
    if _local(root.tag) != "Edmx":
        raise SourceError("Not an OData $metadata document (root element is not edmx:Edmx)")
    version = "4" if (root.get("Version") or "").startswith("4") else "2"
    types: dict[str, dict[str, Any]] = {}
    functions: dict[str, dict[str, Any]] = {}
    sets: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    for schema in root.iter():
        if _local(schema.tag) != "Schema":
            continue
        ns = schema.get("Namespace", "")
        alias = schema.get("Alias")
        for et in _children(schema, "EntityType"):
            keys = [pr.get("Name") for k in _children(et, "Key") for pr in _children(k, "PropertyRef")]
            props = {p.get("Name"): {"type": p.get("Type", "Edm.String"), "label": _description(p),
                                     "nullable": p.get("Nullable", "true") != "false"}
                     for p in _children(et, "Property")}
            entry = {"keys": keys, "props": props, "label": _description(et)}
            types[f"{ns}.{et.get('Name')}"] = entry
            if alias:
                types[f"{alias}.{et.get('Name')}"] = entry
        for fn in _children(schema, "Function") + _children(schema, "Action"):
            if fn.get("IsBound") == "true":
                continue
            entry = {"kind": _local(fn.tag), "params": [(p.get("Name"), p.get("Type", "Edm.String"))
                                                        for p in _children(fn, "Parameter")],
                     "label": _description(fn)}
            functions[f"{ns}.{fn.get('Name')}"] = entry
            if alias:
                functions[f"{alias}.{fn.get('Name')}"] = entry
        for container in _children(schema, "EntityContainer"):
            for es in _children(container, "EntitySet"):
                sets.append({"name": es.get("Name"), "type": es.get("EntityType"), "label": _description(es),
                             "creatable": _attr(es, "creatable") != "false",
                             "updatable": _attr(es, "updatable") != "false",
                             "deletable": _attr(es, "deletable") != "false"})
            for fi in _children(container, "FunctionImport"):
                imports.append({"name": fi.get("Name"), "kind": "v2" if version == "2" else "Function",
                                "ref": fi.get("Function"),
                                "http_method": (_attr(fi, "HttpMethod") or "GET").upper(),
                                "params": [(p.get("Name"), p.get("Type", "Edm.String")) for p in _children(fi, "Parameter")],
                                "label": _description(fi)})
            for ai in _children(container, "ActionImport"):
                imports.append({"name": ai.get("Name"), "kind": "Action", "ref": ai.get("Action"),
                                "http_method": "POST", "params": [], "label": _description(ai)})
    return {"version": version, "types": types, "functions": functions, "sets": sets, "imports": imports}


def build(meta: dict[str, Any]) -> list[OperationDef]:
    version = meta["version"]
    ops: list[OperationDef] = []
    for es in meta["sets"]:
        et = meta["types"].get(es["type"] or "")
        if not et:
            continue
        name, label = es["name"], es["label"] or et["label"]
        keys = [{"name": k, "type": et["props"].get(k, {}).get("type", "Edm.String")} for k in et["keys"]]
        key_props = {k["name"]: _schema_for(k["type"], et["props"].get(k["name"], {}).get("label", "")) for k in keys}
        fields = {p: _schema_for(v["type"], v["label"]) for p, v in list(et["props"].items())[:MAX_PROPS]}
        field_list = ", ".join(list(et["props"])[:40])
        base = {"entity_set": name, "keys": keys, "version": version}
        what = f"{label} ({name})" if label else name

        ops.append(OperationDef(
            key=f"list {name}", name=tool_name("list", name), title=f"List {what}",
            description=f"Query {what}. Fields: {field_list}", tags=[name], method="GET", target=name,
            input_schema=object_schema({
                "filter": {"type": "string", "description": "OData $filter, e.g. Status eq 'Open'"},
                "select": {"type": "string", "description": "Comma-separated fields ($select)"},
                "orderby": {"type": "string", "description": "$orderby, e.g. CreatedAt desc"},
                "top": {"type": "integer", "default": 50, "minimum": 1, "maximum": 1000},
                "skip": {"type": "integer", "minimum": 0},
                "expand": {"type": "string", "description": "Navigation properties to expand ($expand)"},
                **({"search": {"type": "string", "description": "Free-text $search"}} if version == "4" else {}),
            }),
            output_schema={"type": "array", "items": object_schema(fields)},
            binding={**base, "op": "list"}, read_only=True))
        if keys:
            ops.append(OperationDef(
                key=f"get {name}", name=tool_name("get", name), title=f"Get one {what}",
                description=f"Read one {what} by key ({', '.join(k['name'] for k in keys)}).", tags=[name],
                method="GET", target=name,
                input_schema=object_schema({**key_props, "select": {"type": "string"},
                                            "expand": {"type": "string"}}, [k["name"] for k in keys]),
                output_schema=object_schema(fields), binding={**base, "op": "get"}, read_only=True))
        if es["creatable"]:
            ops.append(OperationDef(
                key=f"create {name}", name=tool_name("create", name), title=f"Create {what}", tags=[name],
                method="POST", target=name, input_schema=object_schema({"body": object_schema(fields)}, ["body"]),
                binding={**base, "op": "create"}, read_only=False))
        if es["updatable"] and keys:
            ops.append(OperationDef(
                key=f"update {name}", name=tool_name("update", name), title=f"Update {what}", tags=[name],
                method="PATCH", target=name,
                input_schema=object_schema({**key_props, "body": object_schema(fields)},
                                           [k["name"] for k in keys] + ["body"]),
                binding={**base, "op": "update"}, read_only=False))
        if es["deletable"] and keys:
            ops.append(OperationDef(
                key=f"delete {name}", name=tool_name("delete", name), title=f"Delete {what}", tags=[name],
                method="DELETE", target=name, input_schema=object_schema(key_props, [k["name"] for k in keys]),
                binding={**base, "op": "delete"}, read_only=False))
    for fi in meta["imports"]:
        params = fi["params"]
        fn = meta["functions"].get(fi.get("ref") or "")
        if fn:
            params = fn["params"]
        is_action = fi["kind"] == "Action" or (fn or {}).get("kind") == "Action"
        method = "POST" if is_action else fi["http_method"]
        ops.append(OperationDef(
            key=f"function {fi['name']}", name=tool_name(fi["name"]), title=fi["name"],
            description=fi["label"] or (fn or {}).get("label", ""), method=method, target=fi["name"],
            input_schema=object_schema({p: _schema_for(t) for p, t in params}, [p for p, _ in params]),
            binding={"op": "function", "function": fi["name"], "params": params, "version": version,
                     "action": is_action, "http_method": method},
            read_only=method == "GET"))
    return finalize(ops)


def literal(value: Any, edm_type: str, version: str) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool) or edm_type == "Edm.Boolean":
        return str(value).lower()
    s = str(value)
    if edm_type == "Edm.String":
        return "'" + s.replace("'", "''") + "'"
    if edm_type == "Edm.Guid":
        return f"guid'{s}'" if version == "2" else s
    if edm_type == "Edm.DateTime" and version == "2":
        return f"datetime'{s}'"
    if edm_type == "Edm.DateTimeOffset" and version == "2":
        return f"datetimeoffset'{s}'"
    if edm_type == "Edm.Int64" and version == "2":
        return f"{s}L"
    if edm_type == "Edm.Decimal" and version == "2":
        return f"{s}M"
    return s


def _key_predicate(binding: dict[str, Any], args: dict[str, Any]) -> str:
    keys = binding.get("keys") or []
    version = binding.get("version", "4")
    missing = [k["name"] for k in keys if args.get(k["name"]) is None]
    if missing:
        raise CallError(f"Missing key value(s): {', '.join(missing)}", 400)
    if len(keys) == 1:
        inner = literal(args[keys[0]["name"]], keys[0]["type"], version)
    else:
        inner = ",".join(f"{k['name']}={literal(args[k['name']], k['type'], version)}" for k in keys)
    return "(" + quote(inner, safe="'=,():") + ")"


def _unwrap(body: Any) -> Any:
    if isinstance(body, dict):
        if "d" in body:  # V2 verbose JSON
            d = body["d"]
            return d.get("results", d) if isinstance(d, dict) else d
        if "value" in body:  # V4
            return body["value"]
    return body


async def discover(ctx: ConnCtx, source: dict[str, Any]) -> ImportResult:
    src = dict(source)
    service_root = ctx.base_url
    if not src.get("text"):
        url = str(src.get("url") or ctx.base_url or "").strip()
        if not url:
            raise SourceError("Give the OData service URL (its $metadata is read) or paste the $metadata XML")
        if url.rstrip("/").endswith("$metadata"):
            service_root = url.rstrip("/")[: -len("$metadata")]
        else:
            service_root = url
            url = url.rstrip("/") + "/$metadata"
        src["url"] = url
    meta = parse_metadata(await load_source(src, ctx))
    ops = build(meta)
    warnings = [] if ops else ["No entity sets or function imports found in $metadata"]
    return ImportResult(operations=ops, base_url=(service_root or "").rstrip("/") + "/" if service_root else "",
                        settings={"odata_version": meta["version"]}, warnings=warnings)


async def execute(ctx: ConnCtx, binding: dict[str, Any], args: dict[str, Any]) -> Any:
    version = binding.get("version", "4")
    op = binding["op"]
    headers, params = await auth_headers_and_params(ctx)
    headers = {**headers, "Accept": "application/json"}
    headers["DataServiceVersion" if version == "2" else "OData-Version"] = "2.0" if version == "2" else "4.0"
    query: dict[str, Any] = dict(params)
    if version == "2":
        query["$format"] = "json"
    method, body = "GET", None

    if op == "function":
        name = binding["function"]
        fparams = binding.get("params") or []
        if version == "2" or binding.get("action"):
            path = name
            if binding.get("action"):
                body = {p: args.get(p) for p, _ in fparams if p in args}
            else:
                for p, t in fparams:
                    if p in args:
                        query[p] = literal(args[p], t, version)
        else:
            inner = ",".join(f"{p}={literal(args.get(p), t, version)}" for p, t in fparams if p in args)
            path = f"{name}({quote(inner, safe=chr(39) + '=,():')})"
        method = binding.get("http_method", "GET")
    else:
        path = binding["entity_set"]
        if op in ("get", "update", "delete"):
            path += _key_predicate(binding, args)
        if op == "list":
            for arg, opt in (("filter", "$filter"), ("select", "$select"), ("orderby", "$orderby"),
                             ("top", "$top"), ("skip", "$skip"), ("expand", "$expand"), ("search", "$search")):
                if args.get(arg) not in (None, ""):
                    query[opt] = args[arg]
            query.setdefault("$top", 50)
        elif op == "get":
            for arg, opt in (("select", "$select"), ("expand", "$expand")):
                if args.get(arg):
                    query[opt] = args[arg]
        elif op == "create":
            method, body = "POST", args.get("body") or {}
        elif op == "update":
            method, body = ("MERGE" if version == "2" else "PATCH"), args.get("body") or {}
        elif op == "delete":
            method = "DELETE"

    url = join_url(ctx.base_url, path)
    async with client() as c:
        if method != "GET" and (ctx.settings or {}).get("csrf", True):
            pre = await c.get(join_url(ctx.base_url, ""), headers={**headers, "x-csrf-token": "Fetch"},
                              params=params or None)
            token = pre.headers.get("x-csrf-token")
            if token and token.lower() != "required":
                headers["x-csrf-token"] = token
        r = await c.request(method, url, params=query or None, headers=headers, json=body)
    data = decode_body(r)
    if r.status_code >= 400:
        raise UpstreamError(f"OData {method} {path} returned HTTP {r.status_code}", r.status_code, data)
    return {"status": r.status_code, "data": _unwrap(data)}
