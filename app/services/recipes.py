"""Recipes: saved multi-step READ workflows in app/org_recipes/*.yaml, run server-side by the org MCP.

A recipe chains the org MCP's read-only tools (the same ones Claude calls) so a complicated question is
answered the same way every time, for everyone. Recipes never write — only tools on the read allowlist
can be used, and every call goes through the tool's normal argument validation.

Format::

    name: client-health
    title: Health check for {{client}}
    description: One-stop status for a client — links, missing tools, uncovered machines, open tickets.
    params:
      client: {type: string, description: Client id or code}
    steps:
      - tool: get_client
        args: {client: "{{client}}"}
        as: client
        pick: [client_code, standard_name, compliance]        # optional: keep only these paths
      - tool: search_tickets
        args: {conditions: "closedFlag=false and company/id={{steps.client.psa_company_id}}", limit: 25}
        as: open_tickets
        when: steps.client.psa_company_id                      # optional: skip unless truthy
        optional: true                                         # optional: a failure is reported, not fatal
    present: |                                                 # how Claude should summarise the result
      Lead with what's missing…

``pick`` paths use dots; ``*`` maps over a list (``clients.*.client_code``).
Files are read on every call, so an approved save is live immediately.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import structlog
import yaml

log = structlog.get_logger(__name__)

RECIPE_DIR = Path(__file__).resolve().parent.parent / "org_recipes"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")
_TPL = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")
MAX_STEPS = 12

CallTool = Callable[[str, dict[str, Any]], Awaitable[tuple[bool, Any]]]


def load_recipes() -> dict[str, dict[str, Any]]:
    """name → spec for every valid recipe file (invalid files are logged and skipped)."""
    out: dict[str, dict[str, Any]] = {}
    if not RECIPE_DIR.is_dir():
        return out
    for f in sorted(RECIPE_DIR.glob("*.yaml")):
        try:
            spec = yaml.safe_load(f.read_text(encoding="utf-8"))
            if isinstance(spec, dict) and spec.get("name"):
                out[spec["name"]] = spec
        except Exception as exc:  # noqa: BLE001
            log.error("recipe.load_failed", file=f.name, error=str(exc))
    return out


def validate_recipe(spec: Any, allowed_tools: set[str], name: str | None = None) -> list[str]:
    """Problems with a recipe spec (empty list = valid)."""
    if not isinstance(spec, dict):
        return ["recipe must be a YAML mapping"]
    problems: list[str] = []
    if not NAME_RE.match(str(spec.get("name", ""))):
        problems.append("name must be kebab-case (a-z, 0-9, -), 2–49 chars")
    if name and spec.get("name") != name:
        problems.append(f"name in the file ({spec.get('name')!r}) must match {name!r}")
    if not spec.get("description"):
        problems.append("description is required (it's how people and Claude find the recipe)")
    params = spec.get("params") or {}
    if not isinstance(params, dict):
        problems.append("params must be a mapping")
        params = {}
    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps:
        return problems + ["steps must be a non-empty list"]
    if len(steps) > MAX_STEPS:
        problems.append(f"at most {MAX_STEPS} steps")
    seen: set[str] = set()
    for i, st in enumerate(steps, 1):
        if not isinstance(st, dict):
            problems.append(f"step {i} must be a mapping")
            continue
        tool = st.get("tool")
        if tool not in allowed_tools:
            problems.append(f"step {i}: tool {tool!r} is not a read tool recipes may use")
        key = st.get("as") or tool
        if key in seen:
            problems.append(f"step {i}: duplicate result name {key!r} — set a unique 'as'")
        if not isinstance(st.get("args", {}), dict):
            problems.append(f"step {i}: args must be a mapping")
        refs = _TPL.findall(yaml.safe_dump(st.get("args", {})))
        if st.get("when"):
            refs.append(str(st["when"]).strip("{} "))
        for ref in refs:
            head = ref.split(".")[0]
            if head == "steps":
                if len(ref.split(".")) < 2 or ref.split(".")[1] not in seen:
                    problems.append(f"step {i}: {{{{{ref}}}}} refers to a step that hasn't run yet")
            elif head not in params:
                problems.append(f"step {i}: {{{{{ref}}}}} is not a declared param")
        seen.add(str(key))
    return problems


def _lookup(ctx: dict[str, Any], path: str) -> Any:
    value: Any = ctx
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def _render(value: Any, ctx: dict[str, Any]) -> Any:
    if isinstance(value, str):
        m = _TPL.fullmatch(value.strip())
        if m:
            return _lookup(ctx, m.group(1))
        return _TPL.sub(lambda mm: "" if _lookup(ctx, mm.group(1)) is None else str(_lookup(ctx, mm.group(1))), value)
    if isinstance(value, dict):
        return {k: _render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, ctx) for v in value]
    return value


def _project(src: Any, parts: list[str], dst: Any) -> Any:
    if not parts:
        return src
    head, rest = parts[0], parts[1:]
    if head == "*" and isinstance(src, list):
        dst = dst if isinstance(dst, list) and len(dst) == len(src) else [None] * len(src)
        return [_project(item, rest, dst[i]) for i, item in enumerate(src)]
    if isinstance(src, dict) and head in src:
        dst = dst if isinstance(dst, dict) else {}
        dst[head] = _project(src[head], rest, dst.get(head))
        return dst
    return dst


def pick(data: Any, paths: list[str]) -> Any:
    out: Any = None
    for p in paths:
        out = _project(data, p.split("."), out)
    return out


async def run_recipe(spec: dict[str, Any], params: dict[str, Any], call_tool: CallTool) -> dict[str, Any]:
    """Run every step in order; later steps can use earlier results via {{steps.<as>.<path>}}."""
    declared = spec.get("params") or {}
    missing = [k for k, v in declared.items() if (v or {}).get("required", True) and params.get(k) in (None, "")]
    if missing:
        return {"ok": False, "error": f"Missing param(s): {', '.join(missing)}",
                "params": {k: (v or {}).get("description", "") for k, v in declared.items()}}
    unknown = [k for k in params if k not in declared]
    if unknown:
        return {"ok": False, "error": f"Unknown param(s): {', '.join(unknown)}"}

    ctx: dict[str, Any] = {**params, "steps": {}}
    results: dict[str, Any] = {}
    skipped: list[str] = []
    errors: list[str] = []
    for st in spec.get("steps", []):
        key = st.get("as") or st["tool"]
        if st.get("when") and not _lookup(ctx, str(st["when"]).strip("{} ")):
            skipped.append(f"{key}: condition '{st['when']}' not met")
            continue
        args = {k: v for k, v in _render(st.get("args") or {}, ctx).items() if v not in (None, "")}
        ok, data = await call_tool(st["tool"], args)
        if not ok:
            if st.get("optional"):
                errors.append(f"{key}: {data}")
                continue
            return {"ok": False, "recipe": spec["name"], "failed_step": key, "error": data, "results": results}
        ctx["steps"][key] = data
        results[key] = pick(data, st["pick"]) if st.get("pick") else data
    return {
        "ok": True,
        "recipe": spec["name"],
        "title": _render(spec.get("title", spec["name"]), ctx),
        "results": results,
        **({"skipped": skipped} if skipped else {}),
        **({"step_errors": errors} if errors else {}),
        **({"how_to_present": spec["present"]} if spec.get("present") else {}),
    }
