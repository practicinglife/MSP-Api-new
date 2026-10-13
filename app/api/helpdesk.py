"""Resolve Catch-All Tickets — API + page: alert tickets (SentinelOne, Avast, HPE Instant On) filed under the PSA Catchall company.

* ``GET  /api/helpdesk/s1-incidents``          — incidents (machine + threat), their tickets, client, S1 status,
                                                 whether triage would close, and any change already proposed
* ``POST /api/helpdesk/s1-incidents/propose``  — propose one ``s1_ticket_triage`` change per incident that doesn't
                                                 already have an open/done one (nothing runs until approved)
* ``GET  /helpdesk``                           — page (office/VPN)

A background scan proposes changes every ``MSP_S1_TRIAGE_MINUTES`` (default 15; 0 = off).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

log = structlog.get_logger(__name__)
router = APIRouter(tags=["helpdesk"])
_TASKS: set[asyncio.Task[Any]] = set()
_LOCK = asyncio.Lock()


async def _existing_changes() -> dict[int, dict[str, Any]]:
    """ticket id → the open/executed triage change that covers it (so incidents aren't proposed twice)."""
    from app.services.changes import list_changes

    out: dict[int, dict[str, Any]] = {}
    for c in await list_changes("proposed,awaiting_final_approval,executing,done", limit=300):
        if c.command not in ("s1_ticket_triage", "alert_ticket_triage"):
            continue
        for tid in (c.params or {}).get("ticket_ids") or []:
            out[int(tid)] = {"id": c.id, "number": c.number, "status": c.status}
    return out


async def incidents() -> list[dict[str, Any]]:
    from app.services import helpdesk as hd

    existing = await _existing_changes()
    out = []
    for inc in await hd.find_incidents():
        if inc.get("unparsed"):
            out.append({**inc, "client": None, "threat": None})
            continue
        raw = await hd.s1_threat(inc["machine"], inc["threat_name"], inc["threat_id"])
        th = hd.threat_summary(raw) if raw else None
        client = await hd.resolve_client(inc["machine"], th)
        change = next((existing[t["id"]] for t in inc["tickets"] if t["id"] in existing), None)
        out.append({**inc, "threat": th, "client": client, "would_close": bool(th and th["mitigated"]),
                    "change": change})
    return out


@router.get("/api/helpdesk/s1-incidents")
async def list_incidents() -> dict[str, Any]:
    rows = await incidents()
    return {"count": len(rows), "incidents": rows}


async def alert_groups() -> list[dict[str, Any]]:
    """Avast / Instant On alerts in Catchall with their company match and any change already proposed."""
    from app.services import alert_triage as at

    existing = await _existing_changes()
    out = []
    for g in await at.find_groups():
        match = await at.match_company(g["name"], g["source"])
        change = next((existing[t["id"]] for t in g["tickets"] if t["id"] in existing), None)
        out.append({**g, "match": match, "change": change,
                    "would_close": g["source"] == "avast" and g["handled"]})
    return out


@router.get("/api/helpdesk/alerts")
async def list_alerts() -> dict[str, Any]:
    rows = await alert_groups()
    return {"count": len(rows), "alerts": rows}


@router.get("/api/helpdesk/companies")
async def company_search(q: str) -> dict[str, Any]:
    """PSA company search for the 'pick a company' box (active, not deleted)."""
    from app.services import vendor_client

    qq = q.replace('"', "").strip()
    if len(qq) < 2:
        return {"companies": []}
    r = await vendor_client.get("connectwise_psa", "/company/companies", {
        "conditions": f'name like "%{qq}%" and deletedFlag=false', "pageSize": 15, "fields": "id,identifier,name,status"})
    rows = r["body"] if r["ok"] and isinstance(r["body"], list) else []
    return {"companies": [{"id": x["id"], "identifier": x.get("identifier"), "name": x["name"],
                           "status": (x.get("status") or {}).get("name")} for x in rows]}


from pydantic import BaseModel  # noqa: E402


class AliasBody(BaseModel):
    source: str
    name: str
    psa_company_id: int
    ticket_ids: list[int] = []


@router.post("/api/helpdesk/alias")
async def set_alias(body: AliasBody, request: Request) -> dict[str, Any]:
    """Remember that an alert's customer/site name is this PSA company (Editor sign-in), then propose the change."""
    from fastapi import HTTPException

    from app.api.auth_m365 import ROLE_APPROVER, ROLE_EDITOR, check_csrf, require_role
    from app.db.repository import get_session_factory
    from app.db.schema import AlertCompanyAlias
    from app.services import vendor_client
    from app.services.changes import ChangeError, propose
    from app.services.normalizers import normalize_company_name

    session = await require_role(request, ROLE_EDITOR, ROLE_APPROVER)
    check_csrf(request, session)
    if body.source not in ("avast", "instanton"):
        raise HTTPException(400, "source must be avast or instanton")
    r = await vendor_client.get("connectwise_psa", f"/company/companies/{body.psa_company_id}")
    if not r["ok"]:
        raise HTTPException(404, "PSA company not found")
    key = f"{body.source}|{normalize_company_name(body.name) or body.name.strip().lower()}"
    async with get_session_factory()() as db:
        row = await db.get(AlertCompanyAlias, key) or AlertCompanyAlias(key=key)
        row.source, row.name, row.psa_company_id = body.source, body.name[:256], body.psa_company_id
        row.company_name, row.set_by = str(r["body"].get("name") or "")[:256], session.upn
        await db.merge(row)
        await db.commit()
    log.info("helpdesk.alias_set", key=key, company=body.psa_company_id, by=session.upn)
    out: dict[str, Any] = {"ok": True, "remembered": f"'{body.name}' → {r['body'].get('name')}"}
    if body.ticket_ids:
        try:
            c = await propose("alert_ticket_triage", {"ticket_ids": body.ticket_ids}, source="helpdesk",
                              requested_by=f"{session.upn} (help desk)")
            out["change_id"] = c.id
        except ChangeError as exc:
            out["propose_error"] = str(exc)
    return out


async def propose_all(requested_by: str = "help desk triage") -> dict[str, Any]:
    from app.services.changes import ChangeError, propose

    if _LOCK.locked():
        return {"status": "already_running"}
    async with _LOCK:
        proposed, skipped = [], []
        for inc in await incidents():
            ids = [t["id"] for t in inc["tickets"]]
            if inc.get("unparsed"):
                skipped.append({"tickets": ids, "why": "couldn't read the machine name"})
                continue
            if inc.get("change"):
                skipped.append({"tickets": ids, "why": f"change #{inc['change']['number']} ({inc['change']['status']})"})
                continue
            try:
                c = await propose("s1_ticket_triage", {"ticket_ids": ids}, source="helpdesk", requested_by=requested_by)
                proposed.append({"change_id": c.id, "number": c.number, "title": c.title})
            except ChangeError as exc:
                skipped.append({"tickets": ids, "why": str(exc)})
        # Avast / Instant On alerts — only the ones with a confident company match (others wait for a person)
        for g in await alert_groups():
            ids = [t["id"] for t in g["tickets"]]
            if g.get("change"):
                skipped.append({"tickets": ids, "why": f"change #{g['change']['number']} ({g['change']['status']})"})
                continue
            if not g["match"]["psa_company_id"]:
                skipped.append({"tickets": ids, "why": f"no company match for '{g['name']}' — pick one on /helpdesk"})
                continue
            try:
                c = await propose("alert_ticket_triage", {"ticket_ids": ids}, source="helpdesk",
                                  requested_by=requested_by)
                proposed.append({"change_id": c.id, "number": c.number, "title": c.title})
            except ChangeError as exc:
                skipped.append({"tickets": ids, "why": str(exc)})
        if proposed:
            log.info("helpdesk.s1_triage_proposed", count=len(proposed))
        return {"proposed": proposed, "skipped": skipped}


@router.post("/api/helpdesk/s1-incidents/propose")
async def propose_now() -> dict[str, Any]:
    return await propose_all("help desk triage (manual)")


async def _loop(minutes: int) -> None:
    while True:
        await asyncio.sleep(minutes * 60)
        try:
            await propose_all()
        except Exception as exc:  # noqa: BLE001
            log.error("helpdesk.s1_triage_scan_failed", error=str(exc))


def start_triage_loop() -> None:
    minutes = int(os.environ.get("MSP_S1_TRIAGE_MINUTES", "15") or 0)
    if minutes > 0:
        task = asyncio.create_task(_loop(minutes))
        _TASKS.add(task)


@router.get("/helpdesk", response_class=HTMLResponse)
async def page() -> HTMLResponse:
    return HTMLResponse(_PAGE)


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Resolve Catch-All Tickets</title><style>
:root{--bg:#f6f6f8;--card:#fff;--fg:#1d1d1f;--muted:#6a6a72;--line:#dedee3;--accent:#2f6fde;--ok:#1f7a3a;--warn:#9a6700;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#151517;--card:#1e1e21;--fg:#ececf0;--muted:#a0a0a8;--line:#34343a;--accent:#6b9cff;--ok:#5fd17f;--warn:#e3b341;--bad:#ff8a80}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;padding:20px 16px}
main{max-width:1000px;margin:0 auto}h1{font-size:22px;margin:0}h3{font-size:15px;margin:0}.m{color:var(--muted);font-size:13px}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}a{color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:10px 0}
.bar{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
button{padding:8px 14px;border-radius:8px;border:1px solid var(--accent);background:var(--accent);color:#fff;font:inherit;cursor:pointer}button:disabled{opacity:.5}
button.sec{background:transparent;color:var(--accent)}
table{width:100%;border-collapse:collapse;font-size:14px}td{padding:4px 6px;border-top:1px solid var(--line);vertical-align:top}
</style></head><body><main>
<div class="bar"><div><h1>Resolve Catch-All Tickets</h1><h2 style="font-size:17px;margin:6px 0 0">SentinelOne</h2><div class="m">SentinelOne alert tickets filed under <b>Catchall</b>, grouped per incident (machine + threat).
Each incident becomes one change: move the tickets to the machine's company, merge them, add the summary — and close + resolve in SentinelOne + log on the company only when SentinelOne reports it <b>mitigated</b>.
Proposed automatically every 15 minutes; an Editor submits, an Approver approves.</div></div>
<button id="go">Propose changes now</button></div>
<div id="list" class="m">Loading…</div>
<h2 style="font-size:17px;margin-top:26px">Avast &amp; HPE Instant On</h2>
<div class="m">The customer (Avast) or site (Instant On) named in the alert is matched to a ConnectWise company. Avast alerts close when Avast shows it handled; Instant On stays open. No match? Pick the company — it's remembered for next time.</div>
<div id="alerts" class="m">Loading…</div>
<script>
let ME={};fetch('/auth/me').then(r=>r.json()).then(m=>ME=m);
async function pick(source,name,ids,cid,btn){if(btn)btn.disabled=true;const r=await fetch('/api/helpdesk/alias',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':ME.csrf||''},
 body:JSON.stringify({source,name,psa_company_id:+cid,ticket_ids:ids})});const j=await r.json();if(!r.ok){alert(j.detail||r.status);if(btn)btn.disabled=false;return}
 if(j.change_id)location.href='/changes/'+j.change_id;else{alert(j.remembered+(j.propose_error?'\\n'+j.propose_error:''));loadAlerts()}}
async function searchCo(inp,box,source,name,ids){const q=inp.value.trim();if(q.length<2)return;const d=await (await fetch('/api/helpdesk/companies?q='+encodeURIComponent(q))).json();
 box.innerHTML=d.companies.map(c=>`<div><button class="sec" style="padding:2px 8px;margin:2px 0" data-id="${c.id}">Use</button> ${esc(c.name)} <span class="m">${esc(c.identifier||'')} · ${esc(c.status||'')}</span></div>`).join('')||'<span class="m">no companies</span>';
 box.querySelectorAll('button').forEach(b=>b.onclick=()=>{if(confirm('Use '+b.parentNode.textContent.replace('Use','').trim()+' for “'+name+'” from now on?'))pick(source,name,ids,b.dataset.id,b)})}
async function loadAlerts(){const d=await (await fetch('/api/helpdesk/alerts')).json();
 $('alerts').innerHTML=d.count?d.alerts.map((a,ix)=>{const m=a.match;const ids=a.tickets.map(t=>t.id);
  const ch=a.change?`<a href="/changes/${esc(a.change.id)}">change #${a.change.number} (${esc(a.change.status.replaceAll('_',' '))})</a>`:'<span class="m">no change yet</span>';
  const who=m.psa_company_id?`→ <b>${esc(m.company)}</b> <span class="m">(${esc(m.how)})</span>`:`<span class="bad">no confident match for “${esc(a.name)}”</span>
   <div style="margin-top:6px"><input placeholder="Search ConnectWise companies" style="padding:6px;border-radius:6px;border:1px solid var(--line);background:transparent;color:var(--fg);width:260px" data-ix="${ix}"> <span class="m">${(m.candidates||[]).slice(0,4).map(c=>esc(c.company)).join(' · ')}</span><div class="res" data-ix="${ix}"></div></div>`;
  const what=a.source==='avast'?`Avast · ${esc(a.device)} · ${esc(a.threat)} · action: ${esc(a.action||'?')} ${a.would_close?'<span class="ok">→ will close</span>':'<span class="warn">→ stays open</span>'}`:`Instant On · site “${esc(a.raw_name)}” <span class="warn">→ stays open</span>`;
  return `<div class="card"><div class="bar"><h3>${esc(a.name)}</h3>${ch}</div><div>${what}</div><div>${who}</div><table>${a.tickets.map(t=>`<tr><td>#${t.id}</td><td>${esc(t.summary)}</td></tr>`).join('')}</table></div>`}).join(''):'<div class="card ok">No Avast or Instant On alerts waiting in Catchall.</div>';
 document.querySelectorAll('#alerts input[data-ix]').forEach(inp=>{const a=d.alerts[+inp.dataset.ix];const box=document.querySelector(`#alerts .res[data-ix="${inp.dataset.ix}"]`);let t;
  inp.oninput=()=>{clearTimeout(t);t=setTimeout(()=>searchCo(inp,box,a.source,a.name,a.tickets.map(x=>x.id)),350)}})}
const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load(){const d=await (await fetch('/api/helpdesk/s1-incidents')).json();
 $('list').innerHTML=d.count?d.incidents.map(i=>{const th=i.threat;const c=i.client;
  const st=!th?'<span class="bad">threat not found in SentinelOne</span>':(th.mitigated?'<span class="ok">Mitigated → will close</span>':`<span class="warn">${esc(th.mitigation_text||th.mitigation)} → stays open</span>`);
  const ch=i.change?`<a href="/changes/${esc(i.change.id)}">change #${i.change.number} (${esc(i.change.status.replaceAll('_',' '))})</a>`:'<span class="m">no change yet</span>';
  return `<div class="card"><div class="bar"><h3>${esc(i.machine||'(machine not found in the email)')} <span class="m">${esc(i.threat_name||'')}</span></h3>${ch}</div>
  <div>${c?`→ <b>${esc(c.client)}</b> <span class="m">${esc(c.client_code)} · matched by ${esc(c.matched_by)}</span>`:'<span class="bad">no client match</span>'} · ${st}
  ${th?`<span class="m"> · ${esc(th.classification)} (${esc(th.confidence)}) · ${th.actions.map(a=>esc(a.action+' '+a.status)).join(', ')}</span>`:''}</div>
  <table>${i.tickets.map(t=>`<tr><td>#${t.id}</td><td>${esc(t.event)}</td></tr>`).join('')}</table></div>`}).join(''):'<div class="card ok">No SentinelOne alert tickets waiting in Catchall.</div>'}
$('go').onclick=async e=>{e.target.disabled=true;const r=await (await fetch('/api/helpdesk/s1-incidents/propose',{method:'POST'})).json();
 alert((r.proposed||[]).length+' change(s) proposed'+((r.skipped||[]).length?'\\n'+r.skipped.map(s=>'#'+s.tickets.join(',#')+': '+s.why).join('\\n'):''));e.target.disabled=false;load()};
load();loadAlerts();
</script></main></body></html>"""
