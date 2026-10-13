"""AV gap review: find machines missing their antivirus, flag them for review, redeploy per client.

* ``GET  /api/av/gaps``                 — machines in Asio without their client's antivirus (by client)
* ``GET  /api/av/missing-asio``         — machines with SentinelOne/ESET but no Asio agent (``?format=csv``)
* ``GET  /api/av/deployment/{code}``    — what a redeploy would run for a client (script + parameters)
* ``POST /api/av/sweep``                — refresh AV evidence now (background; ``GET /api/av/sweep`` for status)
* ``GET  /av-review``                   — review page (office/VPN); actions propose change requests

Also runs nightly: smart reconcile + AV sweep (``MSP_NIGHTLY_UTC_HOUR``, default 7).
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select

from app.db.repository import get_session_factory
from app.db.schema import Client

log = structlog.get_logger(__name__)
router = APIRouter(tags=["av-review"])

_SWEEP: dict[str, Any] = {"running": False, "started_at": None, "finished_at": None, "result": None, "error": None}
_LOCK = asyncio.Lock()
_TASKS: set[asyncio.Task[Any]] = set()


async def run_sweep(reason: str = "manual") -> dict[str, Any]:
    """One sweep at a time; records status for the page."""
    from app.services.av_coverage import sweep

    if _LOCK.locked():
        return {"status": "already_running", "started_at": _SWEEP["started_at"]}
    async with _LOCK:
        _SWEEP.update(running=True, started_at=datetime.now(UTC).isoformat(), error=None, reason=reason)
        try:
            async with get_session_factory()() as db:
                _SWEEP["result"] = await sweep(db)
        except Exception as exc:  # noqa: BLE001
            log.error("av.sweep_failed", error=str(exc))
            _SWEEP["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            _SWEEP.update(running=False, finished_at=datetime.now(UTC).isoformat())
    return dict(_SWEEP)


@router.post("/api/av/sweep")
async def start_sweep() -> dict[str, Any]:
    """Refresh AV evidence (Asio detected antivirus + heartbeat, ESET Connect) and verify flagged machines."""
    if _LOCK.locked():
        return {"status": "already_running", **_SWEEP}
    task = asyncio.create_task(run_sweep())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return {"status": "started", "check": "/api/av/sweep"}


@router.get("/api/av/sweep")
async def sweep_status() -> dict[str, Any]:
    return dict(_SWEEP)


@router.get("/api/av/gaps")
async def gaps(client: str | None = None) -> dict[str, Any]:
    from app.services.av_coverage import compute_gaps

    async with get_session_factory()() as db:
        cid = None
        if client:
            c = (await db.execute(select(Client).where(
                (Client.client_code == client.upper()) | (Client.id == client)))).scalar_one_or_none()
            if not c:
                raise HTTPException(404, f"No client {client!r}")
            cid = c.id
        return await compute_gaps(db, cid)


@router.get("/api/av/missing-asio")
async def missing_asio(format: str = "json") -> Any:  # noqa: A002
    from app.services.av_coverage import missing_asio as compute

    async with get_session_factory()() as db:
        data = await compute(db)
    if format != "csv":
        return data
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Company", "Client code", "Device", "Antivirus", "Last seen", "Not seen 30+ days"])
    for m in data["machines"]:
        w.writerow([m["company"], m["client_code"] or "", m["hostname"], m["antivirus"], m["last_seen"] or "",
                    "yes" if m.get("old") else ""])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=missing-asio-agent.csv"})


@router.get("/api/av/deployment/{code}")
async def deployment(code: str) -> dict[str, Any]:
    from app.services.av_coverage import deployment_for_client

    async with get_session_factory()() as db:
        c = (await db.execute(select(Client).where(Client.client_code == code.upper()))).scalar_one_or_none()
        if not c:
            raise HTTPException(404, f"No client {code!r}")
        out = await deployment_for_client(db, c)
    tok = (out.get("parameters") or {}).get("SITETOKEN")
    if tok and len(tok) > 12:  # the page only needs to see that a token was found
        out["parameters"] = {**out["parameters"], "SITETOKEN": tok[:6] + "…" + tok[-4:]}
    return out


# ── Nightly: smart reconcile + AV sweep ───────────────────────────────────


async def nightly_loop() -> None:
    hour = int(os.environ.get("MSP_NIGHTLY_UTC_HOUR", "7"))
    while True:
        now = datetime.now(UTC)
        nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            from app.api.connector_health import get_connectors
            from app.services.reconciliation import run_smart_reconciliation
            log.info("nightly.start")
            await run_smart_reconciliation(get_connectors())
            await run_sweep("nightly")
            log.info("nightly.done", sweep_error=_SWEEP.get("error"))
        except Exception as exc:  # noqa: BLE001
            log.error("nightly.failed", error=str(exc))


def start_nightly() -> asyncio.Task[Any] | None:
    if os.environ.get("MSP_NIGHTLY", "on").lower() in ("0", "off", "false", "no"):
        return None
    task = asyncio.create_task(nightly_loop())
    _TASKS.add(task)
    return task


# ── Review page ───────────────────────────────────────────────────────────


@router.get("/av-review", response_class=HTMLResponse)
async def review_page() -> HTMLResponse:
    return HTMLResponse(_PAGE)


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Antivirus Review</title><style>
:root{--bg:#f6f6f8;--card:#fff;--fg:#1d1d1f;--muted:#6a6a72;--line:#dedee3;--accent:#2f6fde;--ok:#1f7a3a;--warn:#9a6700;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#151517;--card:#1e1e21;--fg:#ececf0;--muted:#a0a0a8;--line:#34343a;--accent:#6b9cff;--ok:#5fd17f;--warn:#e3b341;--bad:#ff8a80}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;padding:20px 16px}
main{max-width:1060px;margin:0 auto}h1{font-size:22px;margin:0}h2{font-size:16px;margin:22px 0 8px}h3{font-size:15px;margin:0}
.m{color:var(--muted);font-size:13px}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:10px 0;overflow-x:auto}
.bar{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.stat b{font-size:22px;display:block}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:6px;border-top:1px solid var(--line);text-align:left;vertical-align:top}
th{border-top:0;color:var(--muted);font-weight:600}code{font:12px ui-monospace,Consolas,monospace;word-break:break-all}
button{padding:8px 14px;border-radius:8px;border:1px solid var(--accent);background:var(--accent);color:#fff;font:inherit;cursor:pointer}
button.sec{background:transparent;color:var(--accent)}button:disabled{opacity:.5;cursor:default}a{color:var(--accent)}
.pill{font-size:12px;padding:1px 8px;border-radius:9px;border:1px solid var(--line);white-space:nowrap}
</style></head><body><main>
<div class="bar"><div><h1>Antivirus Review</h1><div class="m">Machines in Asio missing their client's antivirus (SentinelOne or ESET), and machines with antivirus but no Asio agent.
Fixes are change requests: an Editor submits, an Approver approves. Hyper-V hosts, SQL servers and domain controllers should have no antivirus — see <a href="/discrepancies">Discrepancies</a>.</div></div><div id="who" class="m"></div></div>
<div class="card bar"><div id="sweep" class="m">Loading…</div><div style="display:flex;gap:8px;flex-wrap:wrap">
<button class="sec" id="btn-sweep">Refresh AV status</button><button id="btn-flag">Flag all for review</button></div></div>
<div class="stats" id="stats"></div>
<h2>Needs immediate review</h2><div id="clients"></div>
<h2>Clients without a single antivirus set</h2><div id="fixav" class="card m">—</div>
<h2>Antivirus installed but no Asio agent <a class="m" href="/api/av/missing-asio?format=csv">(CSV)</a></h2><div id="noasio" class="card m">Loading…</div>
<script>
const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const AV={sentinelone:'SentinelOne',eset:'ESET'};
async function who(){const m=await (await fetch('/auth/me')).json();$('who').innerHTML=m.signed_in?esc(m.name)+' · '+esc((m.roles||[]).join(', ')):(m.configured?'<a href="/auth/login?next=/av-review">Sign in with Microsoft 365</a>':'')}
async function propose(command,params,btn){btn&&(btn.disabled=true);const r=await fetch('/api/changes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command,params})});
 const j=await r.json();if(!r.ok){alert(j.detail||r.status);btn&&(btn.disabled=false);return}location.href='/changes/'+j.id}
function age(ts){if(!ts)return '';const d=(Date.now()-Date.parse(ts))/864e5;return d<1?'today':Math.floor(d)+'d ago'}
async function sweepStatus(){const s=await (await fetch('/api/av/sweep')).json();
 $('btn-sweep').disabled=!!s.running;$('sweep').innerHTML=s.running?'⏳ Refreshing AV status (Asio + ESET Connect)… this page updates when done.':
 (s.error?'<span class="bad">Last refresh failed: '+esc(s.error)+'</span>':(s.finished_at?'Last refresh '+esc(s.finished_at.replace('T',' ').slice(0,16))+' UTC':'')) ;
 if(s.running)setTimeout(()=>sweepStatus().then(r=>{if(!r.running)load()}),8000);return s}
function machineRow(m){const st={no_antivirus:'<span class="bad">No antivirus</span>',wrong_antivirus:'<span class="warn">Wrong antivirus</span>',stale:'<span class="m">Stale</span>'}[m.status]||esc(m.status);
 const rv=m.review?`<span class="pill">${esc(m.review.status.replace('_',' '))}</span> <a class="m" href="/changes/${esc(m.review.change_id)}">change</a>`:'';
 return `<tr><td><b>${esc(m.hostname)}</b></td><td>${st}${m.defender_only?' <span class="m">(Defender only)</span>':''}</td><td class="m">${esc((m.asio_products&&m.asio_products.length?m.asio_products:m.found).join(', '))}</td>
 <td class="m">${m.online===true?'<span class="ok">online</span>':m.online===false?'offline':''} ${esc(age(m.last_contact))}</td><td>${rv}</td></tr>`}
async function deploy(code,el){el.innerHTML='Checking deployment…';const d=await (await fetch('/api/av/deployment/'+encodeURIComponent(code))).json();
 const p=Object.entries(d.parameters||{}).map(([k,v])=>`<div><code>${esc(k)}</code> = <code>${esc(v||'—')}</code></div>`).join('');
 el.innerHTML=`<div class="m">Asio script <b>${esc(d.script||'—')}</b>${d.site?' · SentinelOne site '+esc(d.site.name||d.site.id):''}${d.eset_company?' · ESET company '+esc(d.eset_company):''}</div>${p}`+
 (d.problems&&d.problems.length?'<div class="bad">'+d.problems.map(esc).join('<br>')+'</div>':'<div class="ok">Ready to deploy</div>');return d}
async function load(){const g=await (await fetch('/api/av/gaps')).json();const t=g.totals||{};
 if(g.swept_at&&!$('sweep').textContent.trim())$('sweep').textContent='AV data from '+g.swept_at.replace('T',' ').slice(0,16)+' UTC (refreshes nightly)';
 $('stats').innerHTML=[['Checked in Asio',g.checked],['OK',t.ok||0,'ok'],['No antivirus',t.no_antivirus||0,'bad'],['Wrong antivirus',t.wrong_antivirus||0,'warn'],['Stale (30d+)',t.stale||0],['Unlinked company',t.unlinked_company||0]]
  .map(([l,v,c])=>`<div class="card stat"><b class="${c||''}">${v??0}</b><span class="m">${l}</span></div>`).join('');
 if(!g.swept_at){$('clients').innerHTML='<div class="card">No AV data yet — click <b>Refresh AV status</b>.</div>'}
 else $('clients').innerHTML=g.clients.length?g.clients.map(c=>`<div class="card"><div class="bar"><div><h3>${esc(c.client)} <span class="m">${esc(c.client_code)}</span></h3>
 <div class="m">Expected: ${esc(AV[c.av_vendor]||c.av_vendor)} · ${c.counts.no_antivirus} no AV · ${c.counts.wrong_antivirus} wrong AV · ${c.counts.stale} stale</div></div>
 <div style="display:flex;gap:8px"><button class="sec" onclick="deploy('${esc(c.client_code)}',this.closest('.card').querySelector('.dep'))">Show deployment</button>
 <button onclick="propose('av_remediate',{client_code:'${esc(c.client_code)}'},this)">Propose fix for this client</button></div></div>
 <div class="dep" style="margin:8px 0"></div><table><tr><th>Machine</th><th>Status</th><th>Found</th><th>Asio contact</th><th>Review</th></tr>${c.machines.map(machineRow).join('')}</table></div>`).join(''):'<div class="card ok">Every checked machine has its antivirus.</div>';
 $('fixav').innerHTML=g.fix_client_av.length?g.fix_client_av.map(c=>`${esc(c.client)} (${esc(c.client_code)}) — AV: ${esc(c.av_vendor||'none')} · ${c.machines} machine(s)`).join('<br>')+'<div style="margin-top:6px">Link exactly one of SentinelOne or ESET to each in the client registry.</div>':'None.';
 const n=await (await fetch('/api/av/missing-asio')).json();
 $('noasio').innerHTML=n.count?`<div class="m">${n.active} active · ${n.old} not seen for 30+ days (probably retired — remove from SentinelOne/ESET)</div><table><tr><th>Company</th><th>Device</th><th>Antivirus</th><th>Last seen</th></tr>`+n.machines.map(m=>`<tr${m.old?' class="m"':''}><td>${esc(m.company)} <span class="m">${esc(m.client_code||'')}</span></td><td><b>${esc(m.hostname)}</b></td><td>${esc(m.antivirus)}</td><td class="m">${esc(age(m.last_seen))}${m.old?' · old':''}</td></tr>`).join('')+'</table>':'None — every machine with antivirus has an Asio agent.'}
$('btn-sweep').onclick=async()=>{await fetch('/api/av/sweep',{method:'POST'});sweepStatus()};
$('btn-flag').onclick=e=>{if(confirm('Propose ONE change that flags every machine missing its antivirus (all clients) into the Asio review group?'))propose('av_flag_for_review',{},e.target)};
who();sweepStatus();load();
</script></main></body></html>"""
