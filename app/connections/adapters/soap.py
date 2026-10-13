"""SOAP / WSDL → operations, via zeep.

Every operation of every service/port becomes a tool. zeep builds the envelopes (document/literal, RPC,
SOAP 1.1/1.2), keeps parameter order and namespaces right, and parses responses and faults. WCF services:
WS-Addressing is added for SOAP 1.2 bindings (or ``settings.ws_addressing: true``); WS-Security
UsernameToken via auth type ``wsse``. Read-only is guessed from the name (Get…/List…/Find… are reads);
admins can override per operation.
"""

from __future__ import annotations

import asyncio
import contextlib
import decimal
import json
import os
import tempfile
from datetime import date, datetime, time
from typing import Any

import httpx

from app.connections.base import (
    CallError,
    ImportResult,
    OperationDef,
    SourceError,
    UpstreamError,
    finalize,
    looks_read_only,
    object_schema,
    tool_name,
)
from app.connections.http import TIMEOUT, ConnCtx, auth_headers_and_params, client, load_source
from app.connections.netguard import check_url

MAX_DEPTH = 5
_documents: dict[str, Any] = {}  # parsed WSDL per connection (id + source fingerprint)


def _zeep() -> Any:
    import zeep  # noqa: F401  (imported lazily: heavy, and only needed for SOAP connections)

    return zeep


def _simple_schema(t: Any) -> dict[str, Any]:
    accepted = getattr(t, "accepted_types", None) or []
    py = accepted[0] if accepted else str
    if py is bool:
        return {"type": "boolean"}
    if py is int:
        return {"type": "integer"}
    if py in (float, decimal.Decimal):
        return {"type": "number"}
    if py is datetime:
        return {"type": "string", "format": "date-time"}
    if py is date:
        return {"type": "string", "format": "date"}
    if py is time:
        return {"type": "string", "format": "time"}
    return {"type": "string"}


def _type_schema(t: Any, depth: int) -> dict[str, Any]:
    from zeep.xsd import ComplexType

    if isinstance(t, ComplexType):
        if depth >= MAX_DEPTH:
            return {"type": "object", "description": f"{getattr(t, 'name', '') or 'nested'} (nested)"}
        props, req = {}, []
        for name, child in t.elements:
            if not name:
                continue
            props[name] = _element_schema(child, depth + 1)
            if getattr(child, "min_occurs", 0) and not getattr(child, "nillable", False):
                req.append(name)
        return object_schema(props, req)
    return _simple_schema(t)


def _element_schema(el: Any, depth: int = 0) -> dict[str, Any]:
    schema = _type_schema(el.type, depth)
    max_occurs = getattr(el, "max_occurs", 1)
    if max_occurs == "unbounded" or (isinstance(max_occurs, int) and max_occurs > 1):
        return {"type": "array", "items": schema}
    return schema


def _input_schema(operation: Any) -> dict[str, Any]:
    from zeep.xsd import ComplexType

    body = getattr(operation.input, "body", None)
    if body is None:
        return object_schema({})
    if isinstance(body.type, ComplexType):
        return _type_schema(body.type, 0)  # wrapped document/literal and RPC: the children are the arguments
    return object_schema({body.name: _element_schema(body)}, [body.name])


def _docs_from_xml(text: str) -> dict[str, str]:
    """``<wsdl:documentation>`` per portType operation (zeep doesn't keep it)."""
    from defusedxml import ElementTree

    try:
        root = ElementTree.fromstring(text.encode())
    except Exception:  # noqa: BLE001
        return {}
    local = lambda tag: tag.rsplit("}", 1)[-1]  # noqa: E731
    docs: dict[str, str] = {}
    for el in root.iter():
        if local(el.tag) != "portType":
            continue
        for op in el:
            if local(op.tag) == "operation" and op.get("name"):
                for child in op:
                    if local(child.tag) == "documentation" and (child.text or "").strip():
                        docs.setdefault(op.get("name"), child.text.strip()[:2000])
    return docs


def _load_document(location: str, ctx: ConnCtx) -> Any:
    zeep = _zeep()
    from zeep.transports import Transport

    transport = Transport(timeout=60, operation_timeout=60)
    if ctx.auth_type == "basic":
        from requests.auth import HTTPBasicAuth

        transport.session.auth = HTTPBasicAuth(ctx.secrets.get("username", ""), ctx.secrets.get("password", ""))
    return zeep.wsdl.Document(location, transport, settings=zeep.Settings(strict=False, xml_huge_tree=False))


async def _document(ctx: ConnCtx, source: dict[str, Any]) -> Any:
    fingerprint = f"{ctx.id}:{hash(source.get('url') or source.get('text') or '')}"
    if fingerprint in _documents:
        return _documents[fingerprint]
    if source.get("text"):
        fd, path = tempfile.mkstemp(suffix=".wsdl")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(str(source["text"]))
            doc = await asyncio.to_thread(_load_document, path, ctx)
        finally:
            with contextlib.suppress(OSError):
                os.remove(path)
    elif source.get("url"):
        check_url(str(source["url"]))
        doc = await asyncio.to_thread(_load_document, str(source["url"]), ctx)
    else:
        raise SourceError("Give the WSDL URL or paste the WSDL")
    _documents[fingerprint] = doc
    return doc


def forget(connection_id: str) -> None:
    for k in [k for k in _documents if k.startswith(f"{connection_id}:")]:
        _documents.pop(k, None)


async def discover(ctx: ConnCtx, source: dict[str, Any]) -> ImportResult:
    try:
        doc = await _document(ctx, source)
    except (SourceError, CallError):
        raise
    except Exception as exc:  # noqa: BLE001  (zeep/lxml raise many types)
        raise SourceError(f"Could not read the WSDL: {exc}") from exc
    try:
        docs = _docs_from_xml(str(source["text"]) if source.get("text") else await load_source(source, ctx))
    except CallError:
        docs = {}
    ops: list[OperationDef] = []
    base_url = ""
    soap12 = False
    for service in doc.services.values():
        for port in service.ports.values():
            binding = port.binding
            address = (port.binding_options or {}).get("address", "")
            base_url = base_url or address
            soap12 = soap12 or "soap12" in type(binding).__name__.lower() or "Soap12" in type(binding).__name__
            for op_name, op in binding.all().items():
                ops.append(OperationDef(
                    key=f"{service.name}.{port.name}.{op_name}",
                    name=tool_name(op_name), title=op_name, description=docs.get(op_name, ""),
                    tags=[service.name, port.name], method="CALL", target=op_name,
                    input_schema=_input_schema(op),
                    binding={"service": service.name, "port": port.name, "binding": str(binding.name),
                             "operation": op_name, "address": address},
                    read_only=looks_read_only(op_name),
                ))
    if not ops:
        raise SourceError("The WSDL defines no operations")
    return ImportResult(operations=finalize(ops), base_url=base_url,
                        settings={"ws_addressing": True} if soap12 else {},
                        warnings=["Read-only was guessed from operation names — review write operations"])


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=lambda o: str(o) if not isinstance(o, bytes) else o.hex()))


async def execute(ctx: ConnCtx, binding: dict[str, Any], args: dict[str, Any]) -> Any:
    zeep = _zeep()
    from zeep.exceptions import Fault, TransportError
    from zeep.helpers import serialize_object
    from zeep.transports import AsyncTransport

    doc = await _document(ctx, ctx.source or {})
    headers, _ = await auth_headers_and_params(ctx)
    plugins = []
    if (ctx.settings or {}).get("ws_addressing"):
        from zeep.wsa import WsAddressingPlugin

        plugins.append(WsAddressingPlugin())
    wsse = None
    if ctx.auth_type == "wsse":
        from zeep.wsse.username import UsernameToken

        wsse = UsernameToken(ctx.secrets.get("username", ""), ctx.secrets.get("password", ""))
    address = ctx.base_url or binding.get("address") or ""
    check_url(address)
    async with client(headers=headers) as http:
        transport = AsyncTransport(client=http, wsdl_client=httpx.Client(timeout=TIMEOUT))
        soap = zeep.AsyncClient(doc, wsse=wsse, transport=transport, plugins=plugins,
                                settings=zeep.Settings(strict=False))
        from zeep.proxy import AsyncServiceProxy

        wsdl_binding = next((b for k, b in soap.wsdl.bindings.items() if str(k) == binding["binding"]), None)
        if wsdl_binding is None:
            raise CallError(f"Binding {binding['binding']} is no longer in the WSDL — re-import the connection", 409)
        # (zeep's AsyncClient.create_service returns the *sync* proxy; build the async one directly)
        service = AsyncServiceProxy(soap, wsdl_binding, address=address)
        try:
            result = await service[binding["operation"]](**args)
        except Fault as fault:
            raise UpstreamError(f"SOAP fault: {fault.message}", 500,
                            {"code": str(fault.code), "detail": str(fault.detail) if fault.detail is not None else None}) from fault
        except TransportError as exc:
            raise UpstreamError(f"SOAP endpoint returned HTTP {exc.status_code}", exc.status_code or 502,
                            (exc.content or b"")[:2000].decode("utf-8", "replace")) from exc
        except TypeError as exc:  # zeep's argument checking
            raise CallError(f"Invalid arguments: {exc}", 400) from exc
        finally:
            transport.wsdl_client.close()
    return {"data": _jsonable(serialize_object(result, target_cls=dict))}
