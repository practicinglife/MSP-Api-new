"""OpenAPI / Swagger specification fetcher and parser.

Fetches spec JSON or YAML from a URL **or loads from a local file**,
parses endpoints, methods, parameters, and response schemas.
Handles both OpenAPI 3.x and Swagger 2.0.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import structlog
import yaml

log = structlog.get_logger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def load_local_spec(path: str | Path) -> dict[str, Any]:
    """Load an OpenAPI/Swagger spec from a local JSON or YAML file.

    Returns the parsed dict.
    Raises ``FileNotFoundError`` if the file does not exist.
    """
    filepath = Path(path)
    if not filepath.exists():
        raise FileNotFoundError(f"Local spec file not found: {filepath}")

    raw = filepath.read_text(encoding="utf-8")
    is_yaml = filepath.suffix.lower() in (".yaml", ".yml")

    if is_yaml:
        spec = yaml.safe_load(raw)
    else:
        try:
            spec = json.loads(raw)
        except json.JSONDecodeError:
            spec = yaml.safe_load(raw)

    log.info(
        "openapi.local_loaded",
        path=str(filepath),
        title=spec.get("info", {}).get("title", ""),
    )
    return spec


async def fetch_openapi_spec(
    url: str,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Fetch an OpenAPI/Swagger spec (JSON or YAML) from a URL.

    Returns the parsed dict.
    Raises ``httpx.HTTPStatusError`` on non-2xx responses.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT, verify=False) as client:  # noqa: S501
        resp = await client.get(url, headers=headers or {})
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        # Detect YAML by content-type or URL extension
        is_yaml = (
            "yaml" in content_type
            or "x-yaml" in content_type
            or url.rstrip("?").split("?")[0].endswith((".yaml", ".yml"))
        )

        if is_yaml:
            return yaml.safe_load(resp.text)
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            # Last-resort: try YAML parse for ambiguous content types
            return yaml.safe_load(resp.text)


def _resolve_ref(spec: dict, ref: str) -> dict:
    """Resolve a simple ``$ref`` pointer like ``#/components/schemas/Foo``."""
    parts = ref.lstrip("#/").split("/")
    node: Any = spec
    for part in parts:
        if isinstance(node, dict):
            node = node.get(part, {})
        else:
            return {}
    return node if isinstance(node, dict) else {}


def _extract_schema_summary(schema: dict, spec: dict, depth: int = 0) -> dict:
    """Return a compact summary of a JSON schema (resolve one $ref level)."""
    if depth > 2:
        return schema
    if "$ref" in schema:
        resolved = _resolve_ref(spec, schema["$ref"])
        return _extract_schema_summary(resolved, spec, depth + 1)
    return schema


def _parse_parameters_v3(operation: dict, spec: dict) -> list[dict]:
    """Extract parameters from an OpenAPI 3.x operation object."""
    params = []
    for p in operation.get("parameters", []):
        if "$ref" in p:
            p = _resolve_ref(spec, p["$ref"])
        params.append({
            "name": p.get("name", ""),
            "in": p.get("in", ""),
            "required": p.get("required", False),
            "type": p.get("schema", {}).get("type", ""),
            "description": p.get("description", ""),
        })
    return params


def _parse_parameters_v2(operation: dict, spec: dict) -> list[dict]:
    """Extract parameters from a Swagger 2.0 operation object."""
    params = []
    for p in operation.get("parameters", []):
        if "$ref" in p:
            p = _resolve_ref(spec, p["$ref"])
        params.append({
            "name": p.get("name", ""),
            "in": p.get("in", ""),
            "required": p.get("required", False),
            "type": p.get("type", p.get("schema", {}).get("type", "")),
            "description": p.get("description", ""),
        })
    return params


def _parse_request_body_v3(operation: dict, spec: dict) -> dict | None:
    """Extract request body schema from an OpenAPI 3.x operation."""
    rb = operation.get("requestBody")
    if not rb:
        return None
    if "$ref" in rb:
        rb = _resolve_ref(spec, rb["$ref"])
    content = rb.get("content", {})
    for media_type in ("application/json", "text/json", "*/*"):
        if media_type in content:
            schema = content[media_type].get("schema", {})
            return _extract_schema_summary(schema, spec)
    # fallback: first content type
    if content:
        first = next(iter(content.values()))
        return _extract_schema_summary(first.get("schema", {}), spec)
    return None


def _parse_response_schema(operation: dict, spec: dict, is_v3: bool) -> dict | None:
    """Extract the primary success response schema."""
    responses = operation.get("responses", {})
    for code in ("200", "201", "default"):
        resp = responses.get(code)
        if not resp:
            continue
        if "$ref" in resp:
            resp = _resolve_ref(spec, resp["$ref"])
        if is_v3:
            content = resp.get("content", {})
            for media_type in ("application/json", "text/json", "*/*"):
                if media_type in content:
                    schema = content[media_type].get("schema", {})
                    return _extract_schema_summary(schema, spec)
        else:
            schema = resp.get("schema", {})
            if schema:
                return _extract_schema_summary(schema, spec)
    return None


def parse_openapi_spec(spec: dict) -> list[dict[str, Any]]:
    """Parse an OpenAPI / Swagger spec into a flat list of endpoint dicts.

    Each dict contains: path, method, summary, description, tags,
    parameters, request_body_schema, response_schema.
    """
    is_v3 = spec.get("openapi", "").startswith("3")
    paths = spec.get("paths", {})
    endpoints: list[dict[str, Any]] = []

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method in ("get", "post", "put", "patch", "delete", "head", "options"):
            operation = path_item.get(method)
            if not operation or not isinstance(operation, dict):
                continue

            if is_v3:
                params = _parse_parameters_v3(operation, spec)
                request_body = _parse_request_body_v3(operation, spec)
            else:
                params = _parse_parameters_v2(operation, spec)
                # In Swagger 2.0, body params are in the parameters list
                body_params = [p for p in operation.get("parameters", []) if p.get("in") == "body"]
                request_body = _extract_schema_summary(body_params[0].get("schema", {}), spec) if body_params else None

            response_schema = _parse_response_schema(operation, spec, is_v3)

            endpoints.append({
                "path": path,
                "method": method.upper(),
                "summary": operation.get("summary", ""),
                "description": operation.get("description", ""),
                "tags": operation.get("tags", []),
                "parameters": params,
                "request_body_schema": request_body,
                "response_schema": response_schema,
            })

    log.info(
        "openapi.parsed",
        title=spec.get("info", {}).get("title", ""),
        version=spec.get("info", {}).get("version", ""),
        endpoint_count=len(endpoints),
    )
    return endpoints
