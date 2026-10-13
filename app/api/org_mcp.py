"""Company-wide MCP server + skill/plugin distribution, served by the app itself.

* ``POST /msp/mcp`` — MCP (streamable HTTP, stateless JSON responses). Curated, read-only
  tools backed by the static database (client registry, stored devices, snapshots) plus live
  PSA ticket reads. Org skills are also exposed as MCP prompts. Implemented directly on
  JSON-RPC so it does not depend on the ``mcp`` / ``fastapi-mcp`` package versions. Every tool call needs the
  scope ``*:read`` (``*:write`` for tools that need the Editor role; see ``app/packs/msp``) — tools/list doesn't.
* ``/msp/plugin/*`` — Claude Code plugin (marketplace zip + installers), per-skill zips for
  claude.ai org upload, and a VS Code / Copilot MCP config.
* ``GET /msp/connect`` — setup page for staff.

Skills live in ``app/org_skills/<name>/SKILL.md`` — the single source for every client.
"""

from __future__ import annotations

import io
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from app.auth import audit
from app.auth.principal import Principal, get_current, internal_headers
from app.mcp.protocol import PROTOCOLS
from app.mcp.protocol import rpc_error as _rpc_error
from app.packs.msp import scope_denied

log = structlog.get_logger(__name__)

router = APIRouter(tags=["org-mcp"])

SKILLS_DIR = Path(__file__).resolve().parent.parent / "org_skills"
SERVER_NAME = "msp"
PLUGIN_NAME = "msp-tools"
MARKETPLACE_NAME = "msp-tools"
_PROTOCOLS = PROTOCOLS
_MAX_TEXT = 80_000


# ── Skills ────────────────────────────────────────────────────────────────


def _load_skills() -> list[dict[str, str]]:
    skills = []
    if not SKILLS_DIR.is_dir():
        return skills
    for f in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        text = f.read_text(encoding="utf-8")
        meta: dict[str, str] = {}
        body = text
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
        if m:
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            body = m.group(2)
        skills.append({
            "name": meta.get("name", f.parent.name),
            "description": meta.get("description", ""),
            "body": body.strip(),
            "text": text,
            "dir": f.parent.name,
            "mtime": str(int(f.stat().st_mtime)),
        })
    return skills


def _plugin_version(skills: list[dict[str, str]]) -> str:
    newest = max((int(s["mtime"]) for s in skills), default=0)
    return f"1.0.{newest}"


# ── Tools ─────────────────────────────────────────────────────────────────


@dataclass
class Tool:
    name: str
    description: str
    properties: dict[str, Any] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    method: str = "GET"
    path: str = ""  # may contain {arg} placeholders
    live: bool = False
    read_only: bool = True

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": self.properties, "required": self.required, "additionalProperties": False}


def _s(desc: str, **kw: Any) -> dict[str, Any]:
    return {"type": "string", "description": desc, **kw}


def _b(desc: str) -> dict[str, Any]:
    return {"type": "boolean", "description": desc}


def _i(desc: str) -> dict[str, Any]:
    return {"type": "integer", "description": desc}


_PLATFORM = _s("Platform", enum=["connectwise_psa", "asio", "screenconnect", "sentinelone", "eset"])

TOOLS: list[Tool] = [
    # ── Workflow library: check here FIRST for anything multi-step, so nothing gets rebuilt per conversation ──
    Tool("find_workflow", "ALWAYS call first for any request needing more than one or two steps: searches the org library "
         "(saved recipes = read workflows, playbooks = multi-step change templates, commands, skills = how-tos) and says how "
         "to run the best match. Omit query to list everything (optionally one kind).",
         {"query": _s("The user's request in plain words"), "kind": _s("Only this kind", enum=["recipe", "playbook", "command", "skill"])},
         path="__find__"),
    Tool("run_recipe", "Run a saved read workflow (recipe) by name. Read-only and immediate; follow its how_to_present.",
         {"name": _s("Recipe name from find_workflow"), "params": {"type": "object", "description": "Recipe parameters"}},
         ["name"], path="__recipe__"),
    Tool("get_skill", "The current text of an org skill (how we do something). Always the latest version.",
         {"name": _s("Skill name from find_workflow")}, ["name"], path="__skill__"),
    Tool("save_workflow", "Propose adding/updating a recipe, playbook or skill in the org library after you worked something "
         "out that others will ask again. Validated now; live for everyone once an Editor submits, an Approver approves. "
         "Read get_skill('msp-library') for the formats first.",
         {"kind": _s("What to save", enum=["recipe", "playbook", "skill"]), "name": _s("kebab-case name"),
          "content": _s("Full file: YAML (recipe/playbook) or Markdown with frontmatter (skill)", maxLength=60000),
          "delete": _b("Remove it instead")},
         ["kind", "name"], method="POST", path="__save__", read_only=False),
    Tool("search_clients", "Find MSP clients by standard name, client code, abbreviation, or the name they have in ANY tool (PSA, Asio, ScreenConnect, SentinelOne, ESET). Start here for any company question.",
         {"q": _s("What the user typed, e.g. 'APH', 'Main Street', 'heating'")}, ["q"], path="/api/clients/search"),
    Tool("get_client", "One client: standard name, client code, PSA company id, AV vendor, every linked tool company with its current name, and compliance issues.",
         {"client": _s("Client id or client code (e.g. 'APH')")}, ["client"], path="/api/clients/{client}"),
    Tool("resolve_client", "Exact lookup: which client owns a given tool company (by that tool's name or id).",
         {"platform": _PLATFORM, "name": _s("Company/site name as shown in that tool"), "platform_company_id": _s("The tool's company/site id")},
         ["platform"], path="/api/clients/resolve"),
    Tool("list_clients", "All clients with links and compliance. Filter by issue text: 'missing:antivirus', 'missing:screenconnect', 'missing:asio', 'missing:connectwise_psa', 'antivirus:both', 'name-drift', 'links-to-review', 'standard-name'.",
         {"issue": _s("Issue text to filter on"), "compliant": _b("true = only compliant, false = only non-compliant")}, path="/api/clients"),
    Tool("clients_summary", "Counts: clients, compliant vs not, issues by type, orphan companies per tool, last company-list sync.",
         path="/api/clients/summary"),
    Tool("client_devices", "A client's devices across all tools from the stored device data, with per-hostname coverage: which tools each machine is in and which required tools it is missing.",
         {"client": _s("Client id or code"), "missing_only": _b("Only machines missing a required tool"), "platform": _PLATFORM},
         ["client"], path="/api/clients/{client}/devices"),
    Tool("find_device", "Find a machine by (partial) hostname in every tool, with the client it belongs to.",
         {"hostname": _s("Full or partial hostname")}, ["hostname"], path="/api/clients/devices/find"),
    Tool("naming_report", "Clients whose name differs between tools, and standard names that break the client naming standard.",
         {"only_drift": _b("Default true; false = every client")}, path="/api/clients/naming"),
    Tool("unlinked_companies", "Companies that exist in a tool but belong to no client (orphans, leftovers, typos).",
         {"platform": _PLATFORM}, path="/api/clients/unlinked"),
    Tool("links_to_review", "Auto-suggested client↔tool links that still need a human to confirm or reject.",
         path="/api/clients/review"),
    Tool("db_unmatched", "Devices that appeared in only one platform at the last reconciliation.",
         {"company": _s("Filter to one company (canonical name)"), "platform": _PLATFORM}, path="/api/db/unmatched"),
    Tool("platform_counts", "Device and company counts per platform from stored data, with age.",
         path="/api/db/platforms"),
    Tool("snapshot_diff", "What changed per company between two daily snapshots (device counts, matched/unmatched).",
         {"from_date": _s("YYYY-MM-DD, default yesterday"), "to_date": _s("YYYY-MM-DD, default today"), "min_change": _i("Minimum change to show (default 1)")},
         path="/api/db/snapshot/diff"),
    Tool("snapshot_trend", "Daily device-count trend per company.",
         {"days": _i("Days of history (max 90, default 7)"), "company": _s("Filter to one company")}, path="/api/db/snapshot/trend"),
    Tool("data_freshness", "How old the data is: last company-list sync, last device reconciliation, recent runs and errors.",
         path="__freshness__"),
    Tool("search_tickets", "LIVE read of ConnectWise PSA service tickets using a CW conditions string, e.g. 'closedFlag=false and company/id=12345'.",
         {"conditions": _s("CW Manage conditions (default 'closedFlag=false')"), "limit": _i("Max tickets (default 50, max 200)"), "order_by": _s("Default 'id desc'")},
         path="/api/tickets", live=True),
    Tool("get_ticket", "LIVE read of one PSA ticket, optionally with its notes.",
         {"ticket_id": _i("Ticket number"), "include_notes": _b("Also return notes")}, ["ticket_id"], path="__ticket__", live=True),
    Tool("ticket_lookups", "LIVE PSA lookup lists for building ticket filters.",
         {"kind": _s("Which list", enum=["boards", "priorities", "members", "work-types"])}, ["kind"], path="/api/tickets/{kind}", live=True),
    Tool("refresh_client_registry", "Re-read company lists from every tool (~5 s) and auto-link new companies to clients. Safe: reads vendors, writes only the local registry. Confirmed links and approved names are kept.",
         method="POST", path="/api/clients/sync", read_only=False),
    # ── Changes: propose only. Execution needs an Editor to submit and an Approver to approve. ──
    Tool("list_commands", "List the write commands and playbooks that can be proposed (name, risk, platforms, parameters).",
         path="/api/commands"),
    Tool("describe_command", "Full parameter schema for one command or playbook (e.g. 'ticket_add_note', 'playbook:offboard-device').",
         {"name": _s("Command name")}, ["name"], path="__describe__"),
    Tool("propose_change", "Propose a vendor change. Builds a preview (exact requests + current state) and returns an approval link. "
         "NOTHING runs until a signed-in Editor submits it and an Approver gives final approval — tell the user to open the link.",
         {"command": _s("Command name from list_commands"), "params": {"type": "object", "description": "Command parameters"}},
         ["command"], method="POST", path="__propose__", read_only=False),
    Tool("av_gaps", "Machines in Asio WITHOUT their client's antivirus (SentinelOne or ESET), by client: no antivirus, "
         "wrong antivirus, stale; plus review/redeploy status. From the last AV sweep (Asio-detected AV + heartbeat, "
         "SentinelOne agents, ESET Connect).",
         {"client": _s("Client code or id (omit = all clients)")}, path="/api/av/gaps"),
    Tool("discrepancies", "Where Asio, ScreenConnect, ConnectWise PSA (active configurations) and antivirus disagree. "
         "Without client: per-company counts (Asio / ScreenConnect / PSA / AV expected vs installed) and issue counts. "
         "With client: the machines behind each issue (missing ScreenConnect, not in PSA, duplicate or orphaned PSA "
         "configs, missing Asio, wrong company, AV missing, AV on Hyper-V/SQL/DC, stale).",
         {"client": _s("Client code (omit = all companies; 'UNLINKED' = companies not linked to a client)")},
         path="/api/discrepancies"),
    Tool("helpdesk_incidents", "SentinelOne alert tickets waiting in the PSA Catchall company, grouped per incident "
         "(machine + threat): the tickets, the client the machine belongs to, SentinelOne's status (mitigated or not), "
         "whether triage would close it, and any triage change already proposed. Live PSA + SentinelOne read.",
         path="/api/helpdesk/s1-incidents", live=True),
    Tool("helpdesk_alerts", "Avast Business and HPE Instant On alert tickets waiting in the PSA Catchall company: the "
         "customer/site named in the alert, the ConnectWise company it matched (and how) or candidates when it didn't, "
         "whether triage would close it, and any change already proposed. Live PSA read.",
         path="/api/helpdesk/alerts", live=True),
    Tool("missing_asio_agent", "Machines that HAVE SentinelOne or ESET but NO Asio agent — device name and company.",
         path="/api/av/missing-asio"),
    Tool("check_new_client", "Before onboarding a new client: look for existing companies with a similar name in PSA, every tool and the registry, "
         "and get the suggested standard name (≤30), an available client code, catalog prices and the default agreement start date.",
         {"legal_name": _s("Legal company name"), "client_code": _s("Optional code to test")}, ["legal_name"],
         path="/api/onboarding/check"),
    Tool("get_change", "Status, steps, results and audit trail of a proposed change.",
         {"change_id": _s("Change id")}, ["change_id"], path="/api/changes/{change_id}"),
    Tool("list_changes", "Recent changes; filter by status: proposed, awaiting_final_approval, done, failed, rejected, expired, stale.",
         {"status": _s("Comma-separated statuses"), "limit": _i("Max rows (default 50)")}, path="/api/changes"),
    Tool("cancel_change", "Cancel a change you proposed that hasn't been submitted yet.",
         {"change_id": _s("Change id")}, ["change_id"], method="POST", path="__cancel__", read_only=False),
]
_TOOLS_BY_NAME = {t.name: t for t in TOOLS}
_LIBRARY_TOOLS = {"find_workflow", "run_recipe", "get_skill", "save_workflow"}
# Write-type tools any signed-in caller may use: they only create/cancel *proposals* that people approve later.
_PROPOSAL_TOOLS = {"propose_change", "save_workflow", "cancel_change"}


def recipe_tools() -> set[str]:
    """Tools a recipe may call: every read-only tool except the library tools themselves."""
    return {t.name for t in TOOLS if t.read_only and t.name not in _LIBRARY_TOOLS}


async def _internal(request: Request, method: str, path: str, params: dict[str, Any] | None = None,
                    body: Any = None) -> tuple[int, Any]:
    """Call this app's own REST API in-process (no network hop), on behalf of the MCP caller."""
    clean = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in (params or {}).items() if v is not None}
    principal = _principal(request)
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://msp.internal", timeout=180) as client:
        r = await client.request(method, path, params=clean, json=body,
                                 headers=internal_headers(principal, "mcp"))
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text


async def _run_tool(request: Request, tool: Tool, args: dict[str, Any]) -> tuple[bool, Any]:
    missing = [k for k in tool.required if args.get(k) in (None, "")]
    if missing:
        return False, f"Missing required argument(s): {', '.join(missing)}"
    unknown = [k for k in args if k not in tool.properties]
    if unknown:
        return False, f"Unknown argument(s): {', '.join(unknown)}"
    for k, v in args.items():
        spec = tool.properties[k]
        if "enum" in spec and v not in spec["enum"]:
            return False, f"{k} must be one of: {', '.join(spec['enum'])}"
        if spec.get("type") == "integer" and not isinstance(v, int):
            return False, f"{k} must be an integer"
        if spec.get("type") == "boolean" and not isinstance(v, bool):
            return False, f"{k} must be true or false"
        if spec.get("type") == "string" and (not isinstance(v, str) or len(v) > spec.get("maxLength", 500)):
            return False, f"{k} must be a string (max {spec.get('maxLength', 500)} chars)"
        if spec.get("type") == "object" and not isinstance(v, dict):
            return False, f"{k} must be an object"
    # Path arguments must be a single plain segment — never another route.
    for key in re.findall(r"{(\w+)}", tool.path):
        val = str(args.get(key, ""))
        if not re.fullmatch(r"[A-Za-z0-9 _.\-]{1,128}", val) or ".." in val:
            return False, f"Invalid {key}"

    if tool.path == "__freshness__":
        _, scan = await _internal(request, "GET", "/api/db/last-scan")
        _, summ = await _internal(request, "GET", "/api/clients/summary")
        _, runs = await _internal(request, "GET", "/api/runs")
        return True, {
            "devices_last_reconcile": scan,
            "company_lists_last_sync": summ.get("last_company_sync") if isinstance(summ, dict) else None,
            "recent_runs": runs[:5] if isinstance(runs, list) else runs,
        }
    if tool.path == "__find__":
        from app.services.library import search
        hits = search(args.get("query"), args.get("kind"))
        if not hits:
            return True, {"matches": [], "next": "Nothing saved matches. Do it step by step; if others will ask "
                                                 "again, offer to save it (save_workflow)."}
        return True, {"matches": [{k: v for k, v in h.items() if k != "mtime"} for h in hits]}
    if tool.path == "__recipe__":
        from app.services.recipes import load_recipes, run_recipe
        spec = load_recipes().get(str(args["name"]))
        if not spec:
            return False, f"No recipe named {args['name']!r} — use find_workflow(kind='recipe')"
        allowed = recipe_tools()

        async def call(name: str, a: dict[str, Any]) -> tuple[bool, Any]:
            if name not in allowed:
                return False, f"{name} is not allowed in recipes"
            return await _run_tool(request, _TOOLS_BY_NAME[name], a)

        out = await run_recipe(spec, args.get("params") or {}, call)
        return bool(out.get("ok")), out
    if tool.path == "__skill__":
        s = next((s for s in _load_skills() if s["name"] == args["name"]), None)
        return (True, s["text"]) if s else (False, f"No skill named {args['name']!r} — use find_workflow(kind='skill')")
    if tool.path == "__save__":
        params = {k: args[k] for k in ("kind", "name", "content", "delete") if k in args}
        return await _run_tool(request, _TOOLS_BY_NAME["propose_change"], {"command": "save_workflow", "params": params})
    if tool.path == "__describe__":
        name = str(args["name"])
        if not re.fullmatch(r"[a-z0-9_:\-]{1,80}", name):
            return False, "Invalid command name"
        code, data = await _internal(request, "GET", f"/api/commands/{name}")
        return code < 400, data
    if tool.path == "__propose__":
        code, data = await _internal(request, "POST", "/api/changes", body={
            "command": args["command"], "params": args.get("params") or {}})
        if code >= 400:
            return False, data.get("detail", data) if isinstance(data, dict) else data
        return True, {
            "change_number": data.get("number"), "change_id": data.get("id"), "title": data.get("title"),
            "risk": data.get("risk"), "status": data.get("status"), "warnings": data.get("warnings"),
            "steps": [s.get("description") for s in data.get("steps") or []],
            "approval_url": data.get("url"),
            "next": "Open the approval_url (office/VPN), sign in with Microsoft 365 and Submit; "
                    "An Approver then gives final approval. Nothing has run yet.",
        }
    if tool.path == "__cancel__":
        cid = str(args["change_id"])
        if not re.fullmatch(r"[0-9a-f-]{36}", cid):
            return False, "Invalid change_id"
        code, data = await _internal(request, "POST", f"/api/changes/{cid}/cancel")
        return code < 400, data
    if tool.path == "__ticket__":
        tid = int(args["ticket_id"])
        code, ticket = await _internal(request, "GET", f"/api/tickets/{tid}")
        if code >= 400:
            return False, ticket
        out: dict[str, Any] = {"ticket": ticket}
        if args.get("include_notes"):
            _, out["notes"] = await _internal(request, "GET", f"/api/tickets/{tid}/notes")
        return True, out

    path = tool.path
    params = dict(args)
    for key in re.findall(r"{(\w+)}", path):
        path = path.replace("{" + key + "}", quote(str(params.pop(key)), safe=""))
    code, data = await _internal(request, tool.method, path, None if tool.method == "POST" and not params else params)
    if code >= 400:
        detail = data.get("detail", data) if isinstance(data, dict) else data
        return False, f"HTTP {code}: {detail}"
    return True, data


def _as_text(data: Any) -> str:
    text = data if isinstance(data, str) else json.dumps(data, default=str, ensure_ascii=False, indent=1)
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT] + f"\n… truncated ({len(text):,} chars total). Narrow the request (filters, one client)."
    return text


# ── MCP JSON-RPC ──────────────────────────────────────────────────────────


_LIBRARY_RULE = """## Reuse before you build (org workflow library)
1. For anything needing more than one or two tool calls, call `find_workflow` first. If a recipe, playbook,
   command or skill matches, use it (run_recipe / propose_change / get_skill) instead of improvising.
2. If nothing matches and you worked it out in several steps or with a judgment call, finish the request, then
   offer: "Save this to the library so the team gets it next time?" — on yes, read get_skill('msp-library') and
   call `save_workflow`. Saving is a change: an Editor submits, an Approver approves; then it's live for everyone.

"""


def _instructions(skills: list[dict[str, str]]) -> str:
    base = next((s["body"] for s in skills if s["name"] == "msp-api"), "")
    return _LIBRARY_RULE + (base[:6000] if base else "MSP API Tools: client registry, devices, coverage, naming, tickets.")


async def _handle(request: Request, msg: Any) -> dict[str, Any] | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _rpc_error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
    method = msg["method"]
    mid = msg.get("id")
    is_notification = "id" not in msg
    params = msg.get("params") or {}

    if is_notification:
        return None
    try:
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in _PROTOCOLS else _PROTOCOLS[1]
            skills = _load_skills()
            result = {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "title": "MSP API Tools", "version": _plugin_version(skills)},
                "instructions": _instructions(skills),
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [
                {
                    "name": t.name,
                    "description": t.description + (" (live vendor read)" if t.live else " (static database)"),
                    "inputSchema": t.schema(),
                    "annotations": {"readOnlyHint": t.read_only, "openWorldHint": t.live or not t.read_only},
                }
                for t in TOOLS
            ]}
        elif method == "tools/call":
            tool = _TOOLS_BY_NAME.get(params.get("name", ""))
            if not tool:
                return _rpc_error(mid, -32602, f"Unknown tool: {params.get('name')}")
            principal = _principal(request)
            args = params.get("arguments") or {}
            needs_editor = not tool.read_only and tool.name not in _PROPOSAL_TOOLS
            if needs_editor and not principal.can_edit:
                ok, data = False, f"{tool.name} requires the Editor role"
            elif denied := scope_denied(principal, "write" if needs_editor else "read"):
                ok, data = False, denied  # the pack's scope rule (app/packs/msp): *:read, *:write for Editor tools
            else:
                ok, data = await _run_tool(request, tool, args)
            log.info("org_mcp.tool_call", tool=tool.name, ok=ok, who=principal.id)
            await audit.record(principal=principal.label(), auth_kind=principal.kind, source="mcp",
                               method="CALL", path="/msp/mcp", action=tool.name, status=200 if ok else 400,
                               ip=request.headers.get("x-real-ip") or (request.client.host if request.client else None),
                               detail={"args_sha": audit.args_digest(args)})
            result = {"content": [{"type": "text", "text": _as_text(data)}], "isError": not ok}
        elif method == "prompts/list":
            result = {"prompts": [{"name": s["name"], "description": s["description"]} for s in _load_skills()]}
        elif method == "prompts/get":
            s = next((s for s in _load_skills() if s["name"] == params.get("name")), None)
            if not s:
                return _rpc_error(mid, -32602, f"Unknown prompt: {params.get('name')}")
            result = {
                "description": s["description"],
                "messages": [{"role": "user", "content": {"type": "text", "text": s["body"]}}],
            }
        elif method in ("resources/list", "resources/templates/list"):
            result = {"resources": []} if method == "resources/list" else {"resourceTemplates": []}
        else:
            return _rpc_error(mid, -32601, f"Method not found: {method}")
    except Exception as exc:  # noqa: BLE001
        log.error("org_mcp.error", method=method, error=str(exc))
        return _rpc_error(mid, -32603, f"Internal error: {exc}")
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _principal(request: Request) -> Principal:
    """The authenticated caller (the middleware refuses requests without one)."""
    p = getattr(request.state, "principal", None) or get_current()
    if p is None:
        raise HTTPException(401, "Sign-in required")
    return p


async def _external_denied(request: Request) -> Response | None:
    """Defence in depth: the middleware already authenticated the request; refuse if it somehow did not."""
    from app.api.external_auth import unauthorized_response

    if getattr(request.state, "principal", None) is None and get_current() is None:
        return unauthorized_response()
    return None


@router.post("/msp/mcp")
async def mcp_post(request: Request) -> Response:
    denied = await _external_denied(request)
    if denied:
        log.warning("org_mcp.unauthorized", ip=request.headers.get("x-real-ip"))
        return denied
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        return JSONResponse(_rpc_error(None, -32600, "Content-Type must be application/json"), status_code=415)
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse(_rpc_error(None, -32700, "Parse error"), status_code=400)
    batch = isinstance(payload, list)
    responses = [r for r in [await _handle(request, m) for m in (payload if batch else [payload])] if r is not None]
    if not responses:
        return Response(status_code=202)
    return JSONResponse(responses if batch else responses[0])


@router.get("/msp/mcp")
async def mcp_get(request: Request) -> Response:
    denied = await _external_denied(request)
    if denied:
        return denied
    # Stateless server: no server-initiated SSE stream.
    return Response(status_code=405, headers={"Allow": "POST"})


@router.delete("/msp/mcp")
async def mcp_delete() -> Response:
    return Response(status_code=405, headers={"Allow": "POST"})


# ── Distribution ──────────────────────────────────────────────────────────


def _base_url(request: Request) -> str:
    return (os.environ.get("MSP_PUBLIC_BASE_URL") or str(request.base_url)).rstrip("/")


def _plugin_files(base: str) -> dict[str, str]:
    """Files of a local Claude Code marketplace containing the msp-tools plugin."""
    skills = _load_skills()
    root = "msp-tools-marketplace"
    plug = f"{root}/plugins/{PLUGIN_NAME}"
    files = {
        f"{root}/.claude-plugin/marketplace.json": json.dumps({
            "name": MARKETPLACE_NAME,
            "owner": {"name": "MSP Operations"},
            "plugins": [{
                "name": PLUGIN_NAME,
                "source": f"./plugins/{PLUGIN_NAME}",
                "description": "MSP client registry, devices, coverage, naming standards and PSA tickets.",
            }],
        }, indent=2),
        f"{plug}/.claude-plugin/plugin.json": json.dumps({
            "name": PLUGIN_NAME,
            "version": _plugin_version(skills),
            "description": "Company MSP skills + tools served by MSP API Tools.",
            "author": {"name": "MSP Operations"},
        }, indent=2),
        f"{plug}/.mcp.json": json.dumps({
            "mcpServers": {SERVER_NAME: {"type": "http", "url": f"{base}/msp/mcp"}},
        }, indent=2),
    }
    for s in skills:
        files[f"{plug}/skills/{s['dir']}/SKILL.md"] = s["text"]
    return files


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


@router.get("/msp/plugin/manifest")
async def plugin_manifest(request: Request) -> dict[str, Any]:
    skills = _load_skills()
    base = _base_url(request)
    return {
        "plugin": PLUGIN_NAME,
        "version": _plugin_version(skills),
        "mcp_url": f"{base}/msp/mcp",
        "skills": [{"name": s["name"], "description": s["description"], "zip": f"{base}/msp/plugin/skills/{s['dir']}.zip"} for s in skills],
        "tools": [t.name for t in TOOLS],
        "install": {
            "claude_code_windows": f"irm {base}/msp/plugin/install.ps1 | iex",
            "claude_code_mac_linux": f"curl -fsSL {base}/msp/plugin/install.sh | sh",
            "claude_desktop_chat_windows": f"irm {base}/msp/plugin/install-desktop.ps1 | iex",
        },
    }


@router.get("/msp/plugin/msp-tools.zip")
async def plugin_zip(request: Request) -> Response:
    return Response(
        _zip(_plugin_files(_base_url(request))),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=msp-tools.zip"},
    )


@router.get("/msp/plugin/skills/{name}.zip")
async def skill_zip(name: str) -> Response:
    """One skill as <name>/SKILL.md in a zip — the format claude.ai org skill upload takes."""
    s = next((s for s in _load_skills() if s["dir"] == name), None)
    if not s:
        raise HTTPException(404, "Unknown skill")
    return Response(
        _zip({f"{s['dir']}/SKILL.md": s["text"]}),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={s['dir']}.zip"},
    )


@router.get("/msp/plugin/vscode-mcp.json")
async def vscode_config(request: Request) -> dict[str, Any]:
    return {"servers": {SERVER_NAME: {"type": "http", "url": f"{_base_url(request)}/msp/mcp"}}}


_PS1 = r"""# MSP Tools for Claude Code — installs/updates the msp-tools plugin from __BASE__
$ErrorActionPreference = 'Stop'
$Base = '__BASE__'
$Root = Join-Path $env:USERPROFILE '.msp-tools'
New-Item -ItemType Directory -Force $Root | Out-Null
$KeyFile = Join-Path $Root 'key.txt'
$Key = $env:MSP_API_KEY
if (-not $Key -and (Test-Path $KeyFile)) { $Key = (Get-Content $KeyFile -Raw).Trim() }
if (-not $Key) { $Key = (Read-Host 'MSP Tools connection key (msp_..., from your administrator)').Trim() }
if (-not $Key.StartsWith('msp_')) { throw 'A connection key starts with msp_' }
[IO.File]::WriteAllText($KeyFile, $Key, (New-Object Text.UTF8Encoding $false))
icacls $KeyFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" *> $null
$Auth = @{ 'X-API-Key' = $Key }
$Market = Join-Path $Root 'msp-tools-marketplace'
$zip = Join-Path $Root 'msp-tools.zip'
Invoke-WebRequest "$Base/msp/plugin/msp-tools.zip" -OutFile $zip -UseBasicParsing -Headers $Auth
if (Test-Path $Market) { Remove-Item -Recurse -Force $Market }
Expand-Archive $zip -DestinationPath $Root -Force
Remove-Item $zip
$claude = Get-Command claude -ErrorAction SilentlyContinue
$ok = $false
if ($claude) {
  $ErrorActionPreference = 'Continue'
  & claude plugin marketplace add $Market *> $null
  & claude plugin marketplace update msp-tools *> $null
  & claude plugin install msp-tools@msp-tools
  if ($LASTEXITCODE -eq 0) { $ok = $true }
  $ErrorActionPreference = 'Stop'
}
if (-not $ok) {
  # No CLI (desktop app): register the local marketplace + enable the plugin in user settings.
  $dir = Join-Path $env:USERPROFILE '.claude'
  New-Item -ItemType Directory -Force $dir | Out-Null
  $file = Join-Path $dir 'settings.json'
  $s = $null
  if (Test-Path $file) {
    Copy-Item $file "$file.bak" -Force
    $raw = Get-Content $file -Raw
    if ($raw -and $raw.Trim()) { $s = $raw | ConvertFrom-Json }
  }
  if (-not $s) { $s = New-Object PSObject }
  if (-not $s.PSObject.Properties['extraKnownMarketplaces']) { $s | Add-Member extraKnownMarketplaces (New-Object PSObject) }
  $src = [pscustomobject]@{ source = [pscustomobject]@{ source = 'directory'; path = $Market } }
  $s.extraKnownMarketplaces | Add-Member -Force -NotePropertyName 'msp-tools' -NotePropertyValue $src
  if (-not $s.PSObject.Properties['enabledPlugins']) { $s | Add-Member enabledPlugins (New-Object PSObject) }
  $s.enabledPlugins | Add-Member -Force -NotePropertyName 'msp-tools@msp-tools' -NotePropertyValue $true
  [IO.File]::WriteAllText($file, ($s | ConvertTo-Json -Depth 32), (New-Object Text.UTF8Encoding $false))
  Write-Host "Registered msp-tools in $file"
}
Write-Host "MSP Tools installed. Restart Claude Code. Re-run this command any time to update."
"""

_SH = r"""#!/bin/sh
# MSP Tools for Claude Code — installs/updates the msp-tools plugin from __BASE__
set -e
BASE='__BASE__'
ROOT="$HOME/.msp-tools"
MARKET="$ROOT/msp-tools-marketplace"
mkdir -p "$ROOT"
KEY="${MSP_API_KEY:-}"
[ -z "$KEY" ] && [ -f "$ROOT/key.txt" ] && KEY=$(cat "$ROOT/key.txt")
if [ -z "$KEY" ]; then printf 'MSP Tools connection key (msp_...): '; read -r KEY < /dev/tty; fi
( umask 077; printf '%s' "$KEY" > "$ROOT/key.txt" )
curl -fsSL -H "X-API-Key: $KEY" "$BASE/msp/plugin/msp-tools.zip" -o "$ROOT/msp-tools.zip"
rm -rf "$MARKET"
unzip -q -o "$ROOT/msp-tools.zip" -d "$ROOT"
rm -f "$ROOT/msp-tools.zip"
if command -v claude >/dev/null 2>&1; then
  claude plugin marketplace add "$MARKET" >/dev/null 2>&1 || true
  claude plugin marketplace update msp-tools >/dev/null 2>&1 || true
  if claude plugin install msp-tools@msp-tools; then
    echo "MSP Tools installed. Restart Claude Code."; exit 0
  fi
fi
# No CLI (desktop app): register the local marketplace + enable the plugin in user settings.
mkdir -p "$HOME/.claude"
SETTINGS="$HOME/.claude/settings.json"
[ -f "$SETTINGS" ] && cp "$SETTINGS" "$SETTINGS.bak"
if command -v python3 >/dev/null 2>&1; then
  MARKET="$MARKET" SETTINGS="$SETTINGS" python3 - <<'PY'
import json, os
p = os.environ["SETTINGS"]
try:
    s = json.load(open(p)) if os.path.exists(p) and os.path.getsize(p) else {}
except Exception:
    s = {}
s.setdefault("extraKnownMarketplaces", {})["msp-tools"] = {"source": {"source": "directory", "path": os.environ["MARKET"]}}
s.setdefault("enabledPlugins", {})["msp-tools@msp-tools"] = True
json.dump(s, open(p, "w"), indent=2)
PY
  echo "Registered msp-tools in $SETTINGS. Restart Claude Code."
else
  echo "python3 not found. Add to $SETTINGS manually:"
  echo "  \"extraKnownMarketplaces\": {\"msp-tools\": {\"source\": {\"source\": \"directory\", \"path\": \"$MARKET\"}}},"
  echo "  \"enabledPlugins\": {\"msp-tools@msp-tools\": true}"
fi
"""


_BRIDGE_PS1 = r"""# MSP Tools bridge for Claude Desktop chat: stdio MCP <-> __BASE__/msp/mcp
# Runs on the user's PC, so chat can read the LAN server without exposing it publicly.
$ErrorActionPreference = 'Continue'
$Url = '__BASE__/msp/mcp'
$utf8 = New-Object Text.UTF8Encoding $false
$stdin = New-Object IO.StreamReader([Console]::OpenStandardInput(), $utf8)
$stdout = New-Object IO.StreamWriter([Console]::OpenStandardOutput(), $utf8)
$stdout.AutoFlush = $true
Add-Type -AssemblyName System.Net.Http
$http = New-Object Net.Http.HttpClient
$http.Timeout = [TimeSpan]::FromSeconds(300)
$KeyFile = Join-Path (Join-Path $env:USERPROFILE '.msp-tools') 'key.txt'
$Key = $env:MSP_API_KEY
if (-not $Key -and (Test-Path $KeyFile)) { $Key = (Get-Content $KeyFile -Raw).Trim() }
if ($Key) { $http.DefaultRequestHeaders.Add('X-API-Key', $Key) }
while ($null -ne ($line = $stdin.ReadLine())) {
  if (-not $line.Trim()) { continue }
  try {
    $content = New-Object Net.Http.StringContent($line, $utf8, 'application/json')
    $resp = $http.PostAsync($Url, $content).GetAwaiter().GetResult()
    $body = $resp.Content.ReadAsStringAsync().GetAwaiter().GetResult()
    if ([int]$resp.StatusCode -eq 202 -or -not $body) { continue }
    $stdout.WriteLine($body)
  } catch {
    $msg = $null
    try { $msg = $line | ConvertFrom-Json } catch {}
    if ($msg -and $msg.PSObject.Properties['id']) {
      $err = @{ jsonrpc = '2.0'; id = $msg.id; error = @{ code = -32603; message = "MSP server unreachable ($Url): $($_.Exception.Message)" } }
      $stdout.WriteLine(($err | ConvertTo-Json -Compress -Depth 5))
    }
  }
}
"""

_DESKTOP_PS1 = r"""# Adds MSP Tools to Claude Desktop chat (local bridge -> __BASE__/msp/mcp)
$ErrorActionPreference = 'Stop'
$Base = '__BASE__'
$Root = Join-Path $env:USERPROFILE '.msp-tools'
New-Item -ItemType Directory -Force $Root | Out-Null
$KeyFile = Join-Path $Root 'key.txt'
$Key = $env:MSP_API_KEY
if (-not $Key -and (Test-Path $KeyFile)) { $Key = (Get-Content $KeyFile -Raw).Trim() }
if (-not $Key) { $Key = (Read-Host 'MSP Tools connection key (msp_..., from your administrator)').Trim() }
if (-not $Key.StartsWith('msp_')) { throw 'A connection key starts with msp_' }
[IO.File]::WriteAllText($KeyFile, $Key, (New-Object Text.UTF8Encoding $false))
icacls $KeyFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" *> $null
$Auth = @{ 'X-API-Key' = $Key }
$bridge = Join-Path $Root 'msp-bridge.ps1'
Invoke-WebRequest "$Base/msp/plugin/msp-bridge.ps1" -OutFile $bridge -UseBasicParsing
# Claude Desktop config: standard install, or Microsoft Store install
$candidates = @((Join-Path $env:APPDATA 'Claude'))
Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Packages') -Directory -Filter 'Claude_*' -ErrorAction SilentlyContinue |
  ForEach-Object { $candidates += (Join-Path $_.FullName 'LocalCache\Roaming\Claude') }
$dirs = @($candidates | Where-Object { Test-Path (Join-Path $_ 'claude_desktop_config.json') })
if (-not $dirs) { $dirs = @($candidates | Where-Object { Test-Path $_ } | Select-Object -First 1) }
if (-not $dirs) { $dirs = @($candidates[0]) }
$server = [pscustomobject]@{ command = 'powershell.exe'; args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $bridge) }
foreach ($dir in $dirs) {
  New-Item -ItemType Directory -Force $dir | Out-Null
  $file = Join-Path $dir 'claude_desktop_config.json'
  $cfg = $null
  if (Test-Path $file) {
    Copy-Item $file "$file.bak" -Force
    $raw = Get-Content $file -Raw
    if ($raw -and $raw.Trim()) { $cfg = $raw | ConvertFrom-Json }
  }
  if (-not $cfg) { $cfg = New-Object PSObject }
  if (-not $cfg.PSObject.Properties['mcpServers']) { $cfg | Add-Member mcpServers (New-Object PSObject) }
  $cfg.mcpServers | Add-Member -Force -NotePropertyName 'msp' -NotePropertyValue $server
  [IO.File]::WriteAllText($file, ($cfg | ConvertTo-Json -Depth 32), (New-Object Text.UTF8Encoding $false))
  Write-Host "Added 'msp' to $file"
}
Write-Host "Fully quit Claude Desktop (tray icon -> Quit) and reopen it. Re-run any time to update."
"""


@router.get("/msp/plugin/msp-bridge.ps1")
async def bridge_ps1(request: Request) -> PlainTextResponse:
    return PlainTextResponse(_BRIDGE_PS1.replace("__BASE__", _base_url(request)))


@router.get("/msp/plugin/install-desktop.ps1")
async def install_desktop_ps1(request: Request) -> PlainTextResponse:
    return PlainTextResponse(_DESKTOP_PS1.replace("__BASE__", _base_url(request)))


@router.get("/msp/plugin/install.ps1")
async def install_ps1(request: Request) -> PlainTextResponse:
    return PlainTextResponse(_PS1.replace("__BASE__", _base_url(request)))


@router.get("/msp/plugin/install.sh")
async def install_sh(request: Request) -> PlainTextResponse:
    return PlainTextResponse(_SH.replace("__BASE__", _base_url(request)))


@router.get("/msp/connect", response_class=HTMLResponse)
async def connect_page(request: Request) -> HTMLResponse:
    base = _base_url(request)
    skills = _load_skills()
    rows = "".join(
        f"<tr><td><code>{s['name']}</code></td><td>{s['description']}</td>"
        f"<td><a href='{base}/msp/plugin/skills/{s['dir']}.zip'>zip</a></td></tr>"
        for s in skills
    )
    tools = ", ".join(f"<code>{t.name}</code>" for t in TOOLS)
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect to MSP Tools</title>
<style>
:root{{--bg:#fff;--fg:#1d1d1f;--muted:#666;--line:#e3e3e6;--code:#f4f4f6}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151517;--fg:#ececf0;--muted:#a0a0a8;--line:#2c2c31;--code:#222226}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif;max-width:880px;margin:0 auto;padding:24px 16px}}
h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:17px;margin:28px 0 8px}} p,li{{color:var(--fg)}} .m{{color:var(--muted)}}
pre,code{{background:var(--code);border-radius:6px;font:13px ui-monospace,Consolas,monospace}} code{{padding:1px 5px}}
pre{{padding:10px 12px;overflow-x:auto;white-space:pre-wrap;word-break:break-all}}
table{{border-collapse:collapse;width:100%;font-size:14px}} td{{border-top:1px solid var(--line);padding:6px 8px;vertical-align:top}}
a{{color:#2f6fde}}
</style></head><body>
<h1>Connect to MSP Tools</h1>
<p class="m">Version {_plugin_version(skills)} · MCP endpoint <code>{base}/msp/mcp</code> · data from the static database</p>

<h2>Claude Code (desktop app or CLI)</h2>
<p>Windows PowerShell:</p><pre>irm {base}/msp/plugin/install.ps1 | iex</pre>
<p>macOS / Linux:</p><pre>curl -fsSL {base}/msp/plugin/install.sh | sh</pre>
<p class="m">Installs the <code>msp-tools</code> plugin (skills + MCP server). Run again to update. Restart Claude Code afterwards.</p>

<h2>VS Code Copilot / other MCP clients</h2>
<p>Add an HTTP MCP server named <code>msp</code> with URL <code>{base}/msp/mcp</code>. For VS Code, put this in <code>.vscode/mcp.json</code> or your user MCP config:</p>
<pre>{json.dumps({"servers": {SERVER_NAME: {"type": "http", "url": f"{base}/msp/mcp"}}}, indent=2)}</pre>

<h2>Claude Desktop app — chat (Windows)</h2>
<pre>irm {base}/msp/plugin/install-desktop.ps1 | iex</pre>
<p class="m">Adds a small local bridge so desktop chat can read this server from your PC. Nothing is exposed to the internet.
Fully quit Claude Desktop (tray icon → Quit) and reopen it.</p>

<h2>Claude chat in a web browser (claude.ai)</h2>
<p>An org Owner can upload each skill zip below under <b>Organization settings → Plugins &amp; skills → Add → Upload a skill</b> for guidance.
Browser chat runs from Anthropic's cloud and cannot reach this LAN server, so use the Desktop app (above) when you need data.</p>

<h2>Skills</h2><table>{rows}</table>
<h2>Tools</h2><p>{tools}</p>
</body></html>"""
    return HTMLResponse(html)


# ── Workflow library ──────────────────────────────────────────────────────


@router.get("/api/library")
async def library_api(q: str | None = None, kind: str | None = None) -> dict[str, Any]:
    """Everything in the org library (or the best matches for q), with who last saved it through a change."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.db.repository import get_session_factory
    from app.db.schema import ChangeRequest
    from app.services.library import search

    items = search(q, kind, limit=25)
    async with get_session_factory()() as db:
        saves = (await db.execute(select(ChangeRequest).where(
            ChangeRequest.command == "save_workflow", ChangeRequest.status == "done")
            .order_by(ChangeRequest.executed_at))).scalars().all()
    last = {f"{c.params.get('kind')}:{c.params.get('name')}": c for c in saves if c.params}
    for it in items:
        c = last.get(f"{it['kind']}:{it['name']}")
        if c:
            it["saved_by"], it["approved_by"], it["change_number"] = c.submitted_by, c.approved_by, c.number
        if it.get("mtime"):
            it["updated"] = datetime.fromtimestamp(it.pop("mtime"), UTC).isoformat(timespec="minutes")
    return {"count": len(items), "items": items}


@router.get("/library", response_class=HTMLResponse)
async def library_page() -> HTMLResponse:
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Workflow Library</title><style>
:root{--bg:#f6f6f8;--card:#fff;--fg:#1d1d1f;--muted:#6a6a72;--line:#dedee3;--accent:#2f6fde}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#151517;--card:#1e1e21;--fg:#ececf0;--muted:#a0a0a8;--line:#34343a;--accent:#6b9cff}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;padding:20px 16px}
main{max-width:980px;margin:0 auto}h1{font-size:22px;margin:0}.m{color:var(--muted);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:12px 0;overflow-x:auto}
input,select{font:inherit;padding:8px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--fg)}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:7px 6px;border-top:1px solid var(--line);text-align:left;vertical-align:top}
th{border-top:0;color:var(--muted);font-weight:600}.k{font-size:12px;padding:1px 7px;border-radius:9px;border:1px solid var(--line)}
code{font:12px ui-monospace,Consolas,monospace}a{color:var(--accent)}
</style></head><body><main>
<h1>Workflow Library</h1>
<div class="m">Saved once, reused by everyone through Claude. Recipes = read workflows (run instantly) · Playbooks = multi-step
change templates (each run approved) · Commands = built-in changes · Skills = how we do things. New entries are saved through
<a href="/changes">Changes</a> (an Editor submits, an Approver approves).</div>
<div class="card" style="display:flex;gap:10px;flex-wrap:wrap"><input id="q" placeholder="Search, e.g. offboard a device" style="flex:1;min-width:220px">
<select id="kind"><option value="">All kinds</option><option>recipe</option><option>playbook</option><option>command</option><option>skill</option></select></div>
<div class="card"><table><thead><tr><th>Kind</th><th>Name</th><th>What it does</th><th>Updated</th></tr></thead><tbody id="rows"></tbody></table></div>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load(){const p=new URLSearchParams();const q=document.getElementById('q').value.trim(),k=document.getElementById('kind').value;
 if(q)p.set('q',q);if(k)p.set('kind',k);const r=await (await fetch('/api/library?'+p)).json();
 document.getElementById('rows').innerHTML=r.items.map(i=>`<tr><td><span class="k">${esc(i.kind)}</span></td>
 <td><b>${esc(i.title)}</b><div class="m"><code>${esc(i.name)}</code></div></td><td>${esc(i.description)}<div class="m">${esc(i.how)}</div></td>
 <td class="m">${esc((i.updated||'').replace('T',' '))}${i.saved_by?'<br>by '+esc(i.saved_by)+' · #'+i.change_number:''}</td></tr>`).join('')
 ||'<tr><td colspan="4" class="m">Nothing matches.</td></tr>'}
let t;document.getElementById('q').addEventListener('input',()=>{clearTimeout(t);t=setTimeout(load,300)});
document.getElementById('kind').addEventListener('change',load);load();
</script></main></body></html>""")
