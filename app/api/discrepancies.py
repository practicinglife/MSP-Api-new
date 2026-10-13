"""Discrepancy report API + page: where Asio, ScreenConnect, PSA and antivirus disagree.

* ``GET  /api/discrepancies``                — every client: per-tool counts, mismatches, issue counts
* ``GET  /api/discrepancies?client=CODE``    — one client with the machines behind each issue (``UNLINKED`` too)
* ``GET  /api/discrepancies?format=csv``     — every issue, one row per machine
* ``POST /api/discrepancies/role-override``  — mark/unmark a machine as Hyper-V / SQL / DC (Editor sign-in)
* ``GET  /discrepancies``                    — the page (office/VPN)
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from app.db.repository import get_session_factory

router = APIRouter(tags=["discrepancies"])


@router.get("/api/discrepancies")
async def discrepancies(client: str | None = None, format: str = "json") -> Any:  # noqa: A002
    from app.services.discrepancies import build_report

    async with get_session_factory()() as db:
        if format != "csv":
            return await build_report(db, client)
        report = await build_report(db, client)
        detail = [report] if client else []
        if not client:  # CSV of everything: build each client's machine list
            for c in report["clients"]:
                detail.append(await build_report(db, c["client_code"] or "UNLINKED"))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Company", "Client code", "Issue", "Machine", "Tool / detail", "PSA config", "Last Asio contact"])
    for rep in detail:
        for c in rep["clients"]:
            for kind, machines in (c.get("issues") or {}).items():
                for m in machines:
                    tool = m.get("tool") or ", ".join(m.get("seen_in") or m.get("roles") or []) or m.get("status") or ""
                    psa = (m.get("psa") or {}).get("platform_id") or ", ".join(
                        str(x["platform_id"]) for x in m.get("extra") or [])
                    w.writerow([c["client"], c["client_code"] or "", report["issue_types"].get(kind, kind),
                                m.get("hostname", ""), tool, psa or "", m.get("last_contact") or ""])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=discrepancies.csv"})


class RoleOverride(BaseModel):
    endpoint_id: str
    role: str  # hyperv | sql | dc | none | auto (auto = remove the override)
    hostname: str = ""
    note: str = ""


@router.post("/api/discrepancies/role-override")
async def role_override(body: RoleOverride, request: Request) -> dict[str, Any]:
    """Correct a machine's no-antivirus server role. Local data (no vendor write); Editor sign-in, audited."""
    from app.api.auth_m365 import ROLE_APPROVER, ROLE_EDITOR, check_csrf, require_role
    from app.db.schema import MachineRoleOverride

    session = await require_role(request, ROLE_EDITOR, ROLE_APPROVER)
    check_csrf(request, session)
    if body.role not in ("hyperv", "sql", "dc", "none", "auto"):
        raise HTTPException(400, "role must be hyperv, sql, dc, none or auto")
    async with get_session_factory()() as db:
        row = await db.get(MachineRoleOverride, body.endpoint_id)
        if body.role == "auto":
            if row:
                await db.delete(row)
        else:
            if row is None:
                row = MachineRoleOverride(endpoint_id=body.endpoint_id)
                db.add(row)
            row.role, row.hostname, row.note = body.role, body.hostname[:256], body.note[:512]
            row.set_by, row.set_at = session.upn, datetime.now(UTC)
        await db.commit()
    import structlog
    structlog.get_logger(__name__).info("discrepancies.role_override", endpoint=body.endpoint_id,
                                        role=body.role, by=session.upn)
    return {"ok": True, "endpoint_id": body.endpoint_id, "role": body.role, "set_by": session.upn}


@router.get("/discrepancies", response_class=HTMLResponse)
async def page() -> HTMLResponse:
    return HTMLResponse(_PAGE)


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Discrepancies</title><style>
:root{--bg:#f6f6f8;--card:#fff;--fg:#1d1d1f;--muted:#6a6a72;--line:#dedee3;--accent:#2f6fde;--ok:#1f7a3a;--warn:#9a6700;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#151517;--card:#1e1e21;--fg:#ececf0;--muted:#a0a0a8;--line:#34343a;--accent:#6b9cff;--ok:#5fd17f;--warn:#e3b341;--bad:#ff8a80}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;padding:20px 16px}
main{max-width:1180px;margin:0 auto}h1{font-size:22px;margin:0}h2{font-size:16px;margin:20px 0 8px}h3{font-size:15px;margin:14px 0 6px}
.m{color:var(--muted);font-size:13px}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:10px 0;overflow-x:auto}
.bar{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:6px;border-top:1px solid var(--line);text-align:left;vertical-align:top}
th{border-top:0;color:var(--muted);font-weight:600;cursor:pointer;white-space:nowrap}td.n{text-align:right;font-variant-numeric:tabular-nums}
tr.click{cursor:pointer}tr.click:hover td{background:rgba(127,127,127,.08)}
button{padding:7px 12px;border-radius:8px;border:1px solid var(--accent);background:var(--accent);color:#fff;font:inherit;cursor:pointer}
button.sec{background:transparent;color:var(--accent)}a{color:var(--accent)}select{font:inherit;padding:3px;border-radius:6px;background:transparent;color:var(--fg);border:1px solid var(--line)}
.pill{font-size:12px;padding:1px 8px;border-radius:9px;border:1px solid var(--line);white-space:nowrap}
</style></head><body><main>
<div class="bar"><div><h1>Discrepancies</h1><div class="m">Where Asio, ScreenConnect, ConnectWise PSA (active configurations) and antivirus disagree — per company, then machine by machine.
Hyper-V hosts, SQL servers and domain controllers are expected to have <b>no</b> antivirus.</div></div>
<div class="m"><span id="who"></span> · <a href="/av-review">Antivirus review</a> · <a href="/api/discrepancies?format=csv">CSV (all)</a></div></div>
<div class="card" id="totals">Loading…</div>
<div class="card"><table id="t"><thead><tr><th data-k="client">Company</th><th data-k="asio">Asio</th><th data-k="screenconnect">ScreenConnect</th><th data-k="psa">PSA active</th>
<th data-k="av_expected">AV expected</th><th data-k="av_installed">AV installed</th><th data-k="excluded_servers">No-AV servers</th><th data-k="stale">Stale</th><th data-k="issues">Issues</th></tr></thead><tbody></tbody></table></div>
<div id="detail"></div>
<script>
const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let DATA=null,SORT={k:'issues',d:-1},ME={};
async function who(){ME=await (await fetch('/auth/me')).json();$('who').innerHTML=ME.signed_in?esc(ME.name):(ME.configured?'<a href="/auth/login?next=/discrepancies">Sign in</a>':'')}
const age=ts=>{if(!ts)return '';const d=(Date.now()-Date.parse(ts))/864e5;return d<1?'today':Math.floor(d)+'d ago'};
function val(c,k){if(k==='client')return (c.client||'').toLowerCase();if(k==='issues')return Object.values(c.issue_counts).reduce((a,b)=>a+b,0);return c.counts[k]}
function render(){const rows=[...DATA.clients].sort((a,b)=>{const x=val(a,SORT.k),y=val(b,SORT.k);return (x>y?1:x<y?-1:0)*SORT.d});
 const mm=(c,k)=>c.count_mismatch.includes(k)?' bad':'';
 $('t').querySelector('tbody').innerHTML=rows.map(c=>{const n=val(c,'issues');return `<tr class="click" data-code="${esc(c.client_code||'UNLINKED')}"><td><b>${esc(c.client)}</b> <span class="m">${esc(c.client_code||'')}</span></td>
 <td class="n">${c.counts.asio}</td><td class="n${mm(c,'screenconnect')}">${c.counts.screenconnect}</td><td class="n${mm(c,'psa')}">${c.counts.psa}</td>
 <td class="n">${c.counts.av_expected}</td><td class="n${mm(c,'antivirus')}">${c.counts.av_installed}</td><td class="n">${c.counts.excluded_servers}</td><td class="n m">${c.counts.stale}</td>
 <td>${n?Object.entries(c.issue_counts).map(([k,v])=>`<span class="pill" title="${esc(DATA.issue_types[k])}">${esc(k.replaceAll('_',' '))} ${v}</span>`).join(' '):'<span class="ok">none</span>'}</td></tr>`}).join('');
 document.querySelectorAll('tr.click').forEach(tr=>tr.onclick=()=>detail(tr.dataset.code))}
async function load(){DATA=await (await fetch('/api/discrepancies')).json();const t=DATA.totals;
 const iss=Object.keys(DATA.issue_types).filter(k=>t[k]).map(k=>`<span class="pill" title="${esc(DATA.issue_types[k])}">${esc(k.replaceAll('_',' '))} <b>${t[k]}</b></span>`).join(' ');
 $('totals').innerHTML=`<div><b>All companies:</b> Asio ${t.count_asio||0} · ScreenConnect ${t.count_screenconnect||0} · PSA active ${t.count_psa||0} · AV expected ${t.count_av_expected||0} · AV installed ${t.count_av_installed||0} · no-AV servers ${t.count_excluded_servers||0} · stale ${t.count_stale||0}</div>
 <div style="margin-top:6px">${iss||'<span class="ok">No discrepancies</span>'}</div><div class="m" style="margin-top:4px">AV data from ${esc((DATA.swept_at||'never').replace('T',' ').slice(0,16))} UTC · red counts don't match Asio. Click a company for machines.</div>`;
 render()}
document.querySelectorAll('#t th').forEach(th=>th.onclick=()=>{const k=th.dataset.k;SORT={k,d:SORT.k===k?-SORT.d:(k==='client'?1:-1)};render()});
function roleSel(m){const cur=(m.roles&&m.roles.length)?'':'';return `<select onchange="setRole('${esc(m.endpoint_id)}','${esc(m.hostname)}',this.value)">
 <option value="">server role…</option><option value="hyperv">Hyper-V host</option><option value="sql">SQL server</option><option value="dc">Domain controller</option><option value="none">Not an excluded server</option><option value="auto">Use detection</option></select>`}
async function setRole(eid,host,role){if(!role)return;const r=await fetch('/api/discrepancies/role-override',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':ME.csrf||''},body:JSON.stringify({endpoint_id:eid,hostname:host,role})});
 const j=await r.json();if(!r.ok){alert(j.detail||r.status);return}alert('Saved — it applies to the report right away.');load()}
async function propose(code,btn){btn.disabled=true;const r=await fetch('/api/changes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:'psa_inactivate_orphans',params:{client_code:code}})});
 const j=await r.json();if(!r.ok){alert(j.detail||r.status);btn.disabled=false;return}location.href='/changes/'+j.id}
function machineLine(kind,m){const psa=m.psa?` <span class="m">PSA config ${esc(m.psa.platform_id)}${m.psa.endpoint_id?'':' · no Endpoint ID'}</span>`:'';
 const extra=m.extra?` <span class="m">keep ${esc(m.keep.platform_id)} · extra ${m.extra.map(x=>esc(x.platform_id)).join(', ')}</span>`:'';
 const info=[m.tool&&m.asio_company&&('in '+m.tool+(m.tool_company?' as company "'+m.tool_company+'"':'')+' — Asio has it under '+m.asio_company),m.tool&&!m.asio_company&&('in '+m.tool),m.seen_in&&('seen in '+m.seen_in.join(', ')),m.roles&&m.roles.join(', '),m.found&&('found: '+m.found.join(', ')),m.in&&('still in '+m.in.join(', '))].filter(Boolean).map(esc).join(' · ');
 const contact=m.last_contact!==undefined&&m.last_contact!==null?` <span class="m">${m.online===true?'online':m.online===false?'offline':''} ${esc(age(m.last_contact))}</span>`:'';
 const role=(kind==='av_on_excluded_server'||kind==='av_missing')&&m.endpoint_id?' '+roleSel(m):'';
 return `<tr><td><b>${esc(m.hostname)}</b></td><td>${info}${psa}${extra}${contact}${role}</td></tr>`}
async function detail(code){$('detail').innerHTML='<div class="card">Loading…</div>';const d=await (await fetch('/api/discrepancies?client='+encodeURIComponent(code))).json();
 const c=d.clients[0];if(!c){$('detail').innerHTML='';return}const iss=c.issues||{};
 const canFix=(iss.psa_without_machine||iss.psa_duplicate)&&c.client_code;
 $('detail').innerHTML=`<div class="card"><div class="bar"><h2 style="margin:0">${esc(c.client)} <span class="m">${esc(c.client_code||'')}</span></h2>
 ${canFix?`<button onclick="propose('${esc(c.client_code)}',this)">Propose: inactivate orphaned/duplicate PSA configs</button>`:''}</div>
 <div class="m">Asio ${c.counts.asio} · ScreenConnect ${c.counts.screenconnect} · PSA ${c.counts.psa} · SentinelOne ${c.counts.sentinelone} · AV expected ${c.counts.av_expected} / installed ${c.counts.av_installed}</div>
 ${Object.keys(d.issue_types).filter(k=>iss[k]).map(k=>`<h3>${esc(d.issue_types[k])} <span class="m">(${iss[k].length})</span></h3><table>${iss[k].map(m=>machineLine(k,m)).join('')}</table>`).join('')||'<p class="ok">No discrepancies.</p>'}</div>`;
 $('detail').scrollIntoView({behavior:'smooth'})}
who();load();
</script></main></body></html>"""
