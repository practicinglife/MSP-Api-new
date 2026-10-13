"""Change requests API + pages.

* Propose: anyone (dashboard, MCP/Claude, workflows) — ``POST /api/changes``; an MSP pack command needs the
  pack's scope ``*:read`` (``app/packs/msp``), like the rest of the pack
* Submit: an Editor (signed in) — ``POST /api/changes/{id}/submit``
* Final approval: ``MSPTools.Approver`` only — ``POST /api/changes/{id}/approve`` (the only path that executes)
"""

from __future__ import annotations

import html
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.api.auth_m365 import (
    ROLE_APPROVER,
    ROLE_EDITOR,
    check_csrf,
    has_role,
    require_role,
)
from app.auth.principal import get_current
from app.org_profile import external_base_url
from app.services import changes as svc
from app.services.commands import CommandError, get_command, list_commands

router = APIRouter(tags=["changes"])
_CORE_COMMANDS = {"connection_call"}  # every other command comes from the MSP pack


class ProposeBody(BaseModel):
    command: str
    params: dict[str, Any] = {}


class SubmitBody(BaseModel):
    confirmation: str = ""


class RejectBody(BaseModel):
    reason: str


def _err(exc: svc.ChangeError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


async def _requester(request: Request) -> tuple[str, str]:
    """Who is asking — always the authenticated caller, never a client-supplied header."""
    p = getattr(request.state, "principal", None) or get_current()
    if p is None:
        raise HTTPException(401, "Sign-in required")
    source = {"ui": "dashboard"}.get(p.source, p.source)
    return p.label()[:255], source


@router.get("/api/commands")
async def commands() -> list[dict[str, Any]]:
    return list_commands()


@router.get("/api/commands/{name:path}")
async def command(name: str) -> dict[str, Any]:
    try:
        return get_command(name).describe()
    except CommandError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/api/changes")
async def propose(request: Request, body: ProposeBody) -> dict[str, Any]:
    who, source = await _requester(request)
    if body.command not in _CORE_COMMANDS:
        from app.packs.msp import scope_denied

        p = getattr(request.state, "principal", None) or get_current()
        if p is not None and (denied := scope_denied(p, "read")):
            raise HTTPException(403, denied)
    try:
        c = await svc.propose(body.command, body.params, source=source, requested_by=who)
    except svc.ChangeError as exc:
        raise _err(exc) from exc
    return {**svc.detail(c, [], external_base_url()),
            "next": "An Editor must sign in and submit it, then an Approver gives final approval at the url."}


@router.get("/api/changes")
async def list_(status: str | None = None, limit: int = 100) -> dict[str, Any]:
    rows = await svc.list_changes(status, min(limit, 500))
    return {"count": len(rows), "changes": [svc.summarize(c, external_base_url()) for c in rows]}


@router.get("/api/changes/{change_id}")
async def get_(change_id: str) -> dict[str, Any]:
    try:
        c, events = await svc.get(change_id)
    except svc.ChangeError as exc:
        raise _err(exc) from exc
    return svc.detail(c, events, external_base_url())


@router.post("/api/changes/{change_id}/submit")
async def submit(request: Request, change_id: str, body: SubmitBody) -> dict[str, Any]:
    s = await require_role(request, ROLE_EDITOR, ROLE_APPROVER)
    check_csrf(request, s)
    try:
        c = await svc.submit(change_id, editor=s.upn, typed_confirmation=body.confirmation)
    except svc.ChangeError as exc:
        raise _err(exc) from exc
    return svc.summarize(c, external_base_url())


@router.post("/api/changes/{change_id}/approve")
async def approve(request: Request, change_id: str) -> dict[str, Any]:
    s = await require_role(request, ROLE_APPROVER)
    check_csrf(request, s)
    try:
        c = await svc.final_approve(change_id, approver=s.upn)
    except svc.ChangeError as exc:
        raise _err(exc) from exc
    return {**svc.summarize(c, external_base_url()), "results": c.results}


@router.post("/api/changes/{change_id}/reject")
async def reject(request: Request, change_id: str, body: RejectBody) -> dict[str, Any]:
    s = await require_role(request, ROLE_EDITOR, ROLE_APPROVER)
    check_csrf(request, s)
    try:
        c, _ = await svc.get(change_id)
        if c.status == "awaiting_final_approval" and not has_role(s, ROLE_APPROVER):
            raise svc.ChangeError("Only the Approver can reject a change awaiting final approval", 403)
        c = await svc.reject(change_id, actor=s.upn, reason=body.reason)
    except svc.ChangeError as exc:
        raise _err(exc) from exc
    return svc.summarize(c, external_base_url())


@router.post("/api/changes/{change_id}/cancel")
async def cancel(request: Request, change_id: str) -> dict[str, Any]:
    who, _ = await _requester(request)
    try:
        c = await svc.cancel(change_id, actor=who)
    except svc.ChangeError as exc:
        raise _err(exc) from exc
    return svc.summarize(c, external_base_url())


# ── Pages ─────────────────────────────────────────────────────────────────

_STYLE = """
:root{--bg:#f6f6f8;--card:#fff;--fg:#1d1d1f;--muted:#6a6a72;--line:#dedee3;--accent:#2f6fde;--ok:#1f7a3a;
--warn:#9a6700;--bad:#b3261e;--code:#f2f2f5}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#151517;--card:#1e1e21;--fg:#ececf0;
--muted:#a0a0a8;--line:#34343a;--accent:#6b9cff;--ok:#5fd17f;--warn:#e3b341;--bad:#ff8a80;--code:#26262a}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;padding:20px 16px}
main{max-width:1000px;margin:0 auto} h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:22px 0 8px}
.m{color:var(--muted);font-size:13px} a{color:var(--accent)} .card{background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:14px 16px;margin:10px 0} table{width:100%;border-collapse:collapse;font-size:14px}
td,th{text-align:left;padding:7px 8px;border-top:1px solid var(--line);vertical-align:top} th{color:var(--muted);font-weight:600;border-top:0}
.b{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid var(--line)}
.delete{color:var(--bad);border-color:var(--bad)} .update{color:var(--warn);border-color:var(--warn)} .create{color:var(--ok);border-color:var(--ok)}
pre{background:var(--code);padding:8px 10px;border-radius:6px;overflow-x:auto;font-size:12px;white-space:pre-wrap;word-break:break-word;margin:4px 0}
button{padding:8px 16px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);font:inherit;cursor:pointer}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)} button.danger{color:var(--bad);border-color:var(--bad)}
input,textarea{font:inherit;padding:8px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--fg);width:100%;box-sizing:border-box}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center} .warn{color:var(--warn)} .bad{color:var(--bad)} .okc{color:var(--ok)}
.bar{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
"""

_HEAD = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
         'content="width=device-width,initial-scale=1"><title>{t}</title><style>' + _STYLE.replace("{", "{{").replace("}", "}}")
         + "</style></head><body><main>")

_COMMON_JS = """
async function me(){const r=await fetch('/auth/me');return r.json()}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function badge(r){return `<span class="b ${esc(r)}">${esc(r)}</span>`}
async function who(){const m=await me();const el=document.getElementById('who');
 if(!m.signed_in){el.innerHTML=m.configured?`<a href="/auth/login?next=${encodeURIComponent(location.pathname)}">Sign in with Microsoft 365</a>`
 :'<span class="warn">Microsoft 365 sign-in not configured yet</span>';return m}
 el.innerHTML=`${esc(m.name)} · ${esc((m.roles||[]).map(r=>r.replace('MSPTools.','')).join(', '))} · <a href="#" onclick="fetch('/auth/logout',{method:'POST'}).then(()=>location.reload())">Sign out</a>`;return m}
"""


@router.get("/changes", response_class=HTMLResponse)
async def changes_page() -> HTMLResponse:
    return HTMLResponse(_HEAD.format(t="Changes") + """
<div class="bar"><div><h1>Changes</h1><div class="m">Every vendor edit: proposed → submitted by an Editor → final approval by an Approver.</div></div>
<div id="who" class="m"></div></div>
<h2>Awaiting final approval</h2><div class="card"><table id="t-final"></table></div>
<h2>Proposed (needs an Editor to submit)</h2><div class="card"><table id="t-prop"></table></div>
<h2>History</h2><div class="card"><table id="t-hist"></table></div>
<script>""" + _COMMON_JS + """
function rows(list){if(!list.length)return '<tr><td class="m">None</td></tr>';
 return '<tr><th>#</th><th>Change</th><th>Risk</th><th>Requested by</th><th>Status</th><th>Created</th></tr>'+
 list.map(c=>`<tr><td>${c.number}</td><td><a href="/changes/${c.id}">${esc(c.title)}</a><div class="m">${esc(c.command)} · ${c.step_count} step(s)</div></td>
 <td>${badge(c.risk)}</td><td>${esc(c.requested_by)}${c.submitted_by?`<div class="m">submitted by ${esc(c.submitted_by)}</div>`:''}</td>
 <td>${esc(c.status)}</td><td class="m">${esc((c.created_at||'').replace('T',' ').slice(0,16))}</td></tr>`).join('')}
(async()=>{await who();const r=await (await fetch('/api/changes?limit=200')).json();const all=r.changes;
 document.getElementById('t-final').innerHTML=rows(all.filter(c=>c.status==='awaiting_final_approval'));
 document.getElementById('t-prop').innerHTML=rows(all.filter(c=>c.status==='proposed'));
 document.getElementById('t-hist').innerHTML=rows(all.filter(c=>!['proposed','awaiting_final_approval'].includes(c.status)).slice(0,50));})();
</script></main></body></html>""")


@router.get("/changes/{change_id}", response_class=HTMLResponse)
async def change_page(change_id: str) -> HTMLResponse:
    cid = html.escape(change_id, quote=True)
    return HTMLResponse(_HEAD.format(t="Change") + f"""
<div class="bar"><div><a href="/changes">← All changes</a></div><div id="who" class="m"></div></div>
<div id="c"></div>
<script>const CID="{cid}";""" + _COMMON_JS + """
let ME={};
async function act(path,body){const r=await fetch(`/api/changes/${CID}/${path}`,{method:'POST',
 headers:{'Content-Type':'application/json','X-CSRF-Token':ME.csrf||''},body:JSON.stringify(body||{})});
 const j=await r.json().catch(()=>({}));if(!r.ok){alert(j.detail||r.status);}load();}
function stepHtml(s,i,res){const req=s.kind==='http'?`${s.method} ${s.platform} ${s.path}`:`${s.action}`;
 const payload=s.kind==='http'?s.body:s.args;
 return `<tr><td>${i+1}</td><td>${esc(s.description)}<div class="m">${esc(req)}</div>
 ${payload!=null?`<details><summary class="m">Request</summary><pre>${esc(JSON.stringify(payload,null,2))}</pre></details>`:''}
 ${s.before!=null?`<details><summary class="m">Current (before)</summary><pre>${esc(JSON.stringify(s.before,null,2))}</pre></details>`:'<div class="m">No readable before-state</div>'}
 ${res?`<div class="${res.status==='ok'?'okc':'bad'}">${esc(res.status)} ${res.http_status??''}</div>
   ${res.error?`<div class="bad">${esc(res.error)}</div>`:''}
   <details><summary class="m">Response</summary><pre>${esc(JSON.stringify(res.response,null,2))}</pre></details>`:''}</td></tr>`}
async function load(){ME=await who();const c=await (await fetch(`/api/changes/${CID}`)).json();
 if(c.detail){document.getElementById('c').innerHTML=`<p class="bad">${esc(c.detail)}</p>`;return}
 const roles=ME.roles||[];const isAp=roles.includes('Approver')||roles.includes('Admin');const isEd=isAp||roles.includes('Editor');let actions='';
 if(c.status==='proposed'&&isEd){actions=`<div class="card"><b>Submit for final approval</b><div class="m">You confirm this preview is correct. Nothing runs until an Approver approves.</div>
  ${c.risk==='delete'?'<p class="bad">This removes or decommissions something. Type DELETE to confirm.</p><input id="conf" placeholder="DELETE">':''}
  <div class="row" style="margin-top:8px"><button class="primary" onclick="act('submit',{confirmation:(document.getElementById('conf')||{}).value||''})">Submit</button>
  <button class="danger" onclick="const r=prompt('Reason for rejecting?');if(r)act('reject',{reason:r})">Reject</button></div></div>`}
 if(c.status==='awaiting_final_approval'&&isAp){actions=`<div class="card"><b>Final approval</b><div class="m">Submitted by ${esc(c.submitted_by)}. Approving runs every step now.</div>
  <div class="row" style="margin-top:8px"><button class="primary" onclick="if(confirm('Run this change now?'))act('approve')">Approve &amp; run</button>
  <button class="danger" onclick="const r=prompt('Reason for rejecting?');if(r)act('reject',{reason:r})">Reject</button></div></div>`}
 if(['proposed','awaiting_final_approval'].includes(c.status)&&!ME.signed_in&&!actions){actions='<p class="m">Sign in to submit or approve.</p>'}
 const res=c.results||[];
 document.getElementById('c').innerHTML=`<h1>#${c.number} ${esc(c.title)}</h1>
 <div class="row m">${badge(c.risk)} <span>Status: <b>${esc(c.status)}</b></span><span>Command: ${esc(c.command)}</span>
 <span>Requested by ${esc(c.requested_by)} (${esc(c.source)})</span>${c.submitted_by?`<span>Submitted by ${esc(c.submitted_by)}</span>`:''}
 ${c.approved_by?`<span>Approved by ${esc(c.approved_by)}</span>`:''}<span>Expires ${esc((c.expires_at||'').replace('T',' ').slice(0,16))} UTC</span></div>
 ${c.rejected_reason?`<p class="bad">Rejected: ${esc(c.rejected_reason)}</p>`:''}
 ${(c.warnings||[]).map(w=>`<p class="warn">⚠ ${esc(w)}</p>`).join('')}
 ${actions}
 <h2>Steps (${c.steps.length})</h2><div class="card"><table>${c.steps.map((s,i)=>stepHtml(s,i,res[i])).join('')}</table></div>
 <h2>Audit</h2><div class="card"><table>${c.events.map(e=>`<tr><td class="m">${esc((e.at||'').replace('T',' ').slice(0,19))}</td><td>${esc(e.event)}</td><td>${esc(e.actor)}</td>
 <td class="m">${e.details?esc(JSON.stringify(e.details)):''}</td></tr>`).join('')}</table></div>`;
 if(c.status==='executing'){document.getElementById('c').insertAdjacentHTML('afterbegin','<p class="warn">Running — some steps wait on the vendor (a few minutes). This page refreshes itself.</p>');setTimeout(load,10000)}}
load();
</script></main></body></html>""")
