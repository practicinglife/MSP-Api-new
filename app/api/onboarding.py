"""New client onboarding: duplicate/availability check + the form page (/onboard).

The form proposes one ``onboard_client`` change; an Editor submits it and the Approver gives final approval.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
from rapidfuzz import fuzz
from sqlalchemy import select

from app.db.repository import get_session_factory
from app.db.schema import Client, PlatformCompany
from app.services import vendor_client
from app.services.client_naming import clean_standard_name, suggest_client_code, validate_standard_name
from app.services.commands.onboarding import ALERT_EMAIL, LINES, _cw_quote, catalog_prices, first_of_next_month
from app.services.normalizers import normalize_company_name

router = APIRouter(tags=["changes"])


async def _psa_identifier_taken(code: str) -> bool:
    r = await vendor_client.get("connectwise_psa", "/company/companies",
                                {"conditions": f"identifier={_cw_quote(code)}", "pageSize": 1})
    return bool(r["ok"] and r["body"])


@router.get("/api/onboarding/catalog")
async def catalog() -> dict[str, Any]:
    """Catalog prices and line names, so the form shows editable prices before any name is typed."""
    return {"catalog_prices": await catalog_prices(), "lines": LINES, "default_start_date": first_of_next_month(),
            "alert_email": ALERT_EMAIL}


@router.get("/api/onboarding/check")
async def check(legal_name: str = Query(..., min_length=2), client_code: str | None = None) -> dict[str, Any]:
    """Look for existing companies like this one (PSA + every tool's cache + registry) and suggest name/code."""
    norm = normalize_company_name(legal_name)
    words = [w for w in re.findall(r"[A-Za-z0-9]+", legal_name) if len(w) > 2][:2]
    matches: list[dict[str, Any]] = []

    # PSA live search on the first meaningful word(s)
    if words:
        # deletedFlag=false: PSA keeps deleted companies in the API but hides them in the UI
        cond = " and ".join(f"name like {_cw_quote('%' + w + '%')}" for w in words) + " and deletedFlag=false"
        r = await vendor_client.get("connectwise_psa", "/company/companies",
                                    {"conditions": cond, "pageSize": 50, "fields": "id,identifier,name,status,types"})
        for c in (r["body"] if r["ok"] and isinstance(r["body"], list) else []):
            score = fuzz.token_sort_ratio(norm, normalize_company_name(c.get("name", "")))
            if score >= 60:
                matches.append({"source": "connectwise_psa", "id": c.get("id"), "name": c.get("name"),
                                "identifier": c.get("identifier"), "status": (c.get("status") or {}).get("name"),
                                "score": score})

    # Every tool's cached company list + registry
    async with get_session_factory()() as db:
        pcs = list((await db.execute(select(PlatformCompany).where(PlatformCompany.active.is_(True)))).scalars())
        clients = list((await db.execute(select(Client))).scalars())
    for pc in pcs:
        if pc.platform == "connectwise_psa":
            continue
        score = fuzz.token_sort_ratio(norm, pc.name_norm)
        if score >= 80:
            matches.append({"source": pc.platform, "id": pc.platform_company_id, "name": pc.name, "score": score})
    for c in clients:
        score = max(fuzz.token_sort_ratio(norm, normalize_company_name(c.standard_name)),
                    fuzz.token_sort_ratio(norm, normalize_company_name(c.legal_name or "")))
        if score >= 80:
            matches.append({"source": "registry", "id": c.client_code, "name": c.standard_name, "score": score})
    matches.sort(key=lambda m: -m["score"])
    exact = [m for m in matches if m["score"] >= 95]

    std = clean_standard_name(legal_name)
    taken = {c.client_code for c in clients if c.client_code}
    code = (client_code or "").strip().upper() or suggest_client_code(std, taken)
    code_status = "ok"
    if code in taken:
        code_status = "used in registry"
    elif code and await _psa_identifier_taken(code):
        code_status = "used as a PSA Company ID"
        for _ in range(6):  # suggest the next free one
            taken.add(code)
            code = suggest_client_code(std, taken)
            if code and not await _psa_identifier_taken(code):
                code_status = "ok"
                break
    return {
        "legal_name": legal_name,
        "suggested_standard_name": std[:30],
        "standard_name_problems": validate_standard_name(std[:30]),
        "standard_name_was_shortened": len(std) > 30,
        "suggested_client_code": code,
        "client_code_status": code_status,
        "matches": matches[:15],
        "likely_duplicate": bool(exact),
        "catalog_prices": await catalog_prices(),
        "lines": LINES,
        "default_start_date": first_of_next_month(),
        "alert_email": ALERT_EMAIL,
    }


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>New Client Onboarding</title><style>
:root{--bg:#f6f6f8;--card:#fff;--fg:#1d1d1f;--muted:#6a6a72;--line:#dedee3;--accent:#2f6fde;--ok:#1f7a3a;--warn:#9a6700;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#151517;--card:#1e1e21;--fg:#ececf0;--muted:#a0a0a8;--line:#34343a;--accent:#6b9cff;--ok:#5fd17f;--warn:#e3b341;--bad:#ff8a80}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;padding:20px 16px}
main{max-width:860px;margin:0 auto}h1{font-size:22px;margin:0}h2{font-size:15px;margin:0 0 10px}.m{color:var(--muted);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:12px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px 14px}
label{display:block;font-size:13px;color:var(--muted);margin-bottom:3px}
input,select{font:inherit;padding:8px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--fg);width:100%;box-sizing:border-box}
.span2{grid-column:span 2}@media(max-width:520px){.span2{grid-column:span 1}}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:6px 6px;border-top:1px solid var(--line);text-align:left}th{border-top:0;color:var(--muted);font-weight:600}
td input{width:90px}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
button{padding:10px 18px;border-radius:8px;border:1px solid var(--accent);background:var(--accent);color:#fff;font:inherit;cursor:pointer}
button:disabled{opacity:.5;cursor:default}.bar{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
.match{font-size:13px;padding:2px 0}
</style></head><body><main>
<div class="bar"><div><h1>New Client Onboarding</h1><div class="m">Creates the client in ConnectWise PSA (company, contact, managed-services agreement), SentinelOne or ESET, and Asio. You submit it; an Approver gives final approval.</div></div><div id="who" class="m"></div></div>

<div class="card"><h2>Company</h2><div class="grid">
<div class="span2"><label for="legal">Legal company name (PSA name, invoices)</label><input id="legal" autocomplete="off" placeholder="Brian Wear Plumbing, LLC"></div>
<div><label for="std">Standard name (other tools, max 30)</label><input id="std" maxlength="30"></div>
<div><label for="code">Client code (PSA Company ID)</label><input id="code" maxlength="4"></div>
</div><div id="namecheck" class="m" style="margin-top:8px"></div><div id="matches"></div>
<label style="margin-top:8px;display:flex;gap:6px;align-items:center;color:var(--fg)"><input type="checkbox" id="allow" style="width:auto"> This is a different company from the matches above</label></div>

<div class="card"><h2>Address and main phone</h2><div class="grid">
<div class="span2"><label for="a1">Street address</label><input id="a1"></div><div><label for="a2">Suite (optional)</label><input id="a2"></div>
<div><label for="city">City</label><input id="city"></div><div><label for="state">State</label><input id="state" maxlength="2" placeholder="MO"></div>
<div><label for="zip">ZIP</label><input id="zip" maxlength="10"></div><div><label for="phone">Main phone</label><input id="phone"></div>
</div></div>

<div class="card"><h2>Main contact <span class="m">(ConnectWise PSA only — ticketing)</span></h2><div class="grid">
<div><label for="cf">First name</label><input id="cf"></div><div><label for="cl">Last name</label><input id="cl"></div>
<div><label for="ce">Email</label><input id="ce" type="email"></div><div><label for="cp">Direct phone</label><input id="cp"></div>
</div><div class="m" style="margin-top:6px">Antivirus alerts go to <b id="alertmail">support@</b>, never to the customer.</div></div>

<div class="card"><h2>Services — managed-services agreement</h2>
<div class="grid" style="margin-bottom:10px"><div><label>Antivirus (pick one)</label>
<div style="display:flex;gap:18px;padding:8px 0">
<label style="display:flex;gap:6px;align-items:center;color:var(--fg);margin:0"><input type="checkbox" id="av_s1" style="width:auto"> SentinelOne</label>
<label style="display:flex;gap:6px;align-items:center;color:var(--fg);margin:0"><input type="checkbox" id="av_eset" style="width:auto"> ESET</label></div></div>
<div><label for="start">Agreement start</label><input id="start" type="date"></div></div>
<table><thead><tr><th>Line</th><th>Qty</th><th>Price $ <span class="m">(editable)</span></th><th>Monthly</th></tr></thead><tbody id="lines"></tbody>
<tfoot><tr><th colspan="3">Monthly total</th><th id="total">$0.00</th></tr></tfoot></table></div>

<div class="bar"><div id="msg" class="m"></div><button id="go" disabled>Propose onboarding</button></div>
</main><script>
const $=id=>document.getElementById(id);let CHK=null;
const ROWS=[['workstations','Managed workstations',true],['servers','Managed servers',true],['network','Managed network (0 or 1)',true],['sc_clients','ScreenConnect clients',true],['av','Antivirus seats',true]];
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function who(){const m=await (await fetch('/auth/me')).json();$('who').innerHTML=m.signed_in?esc(m.name)+' · '+esc((m.roles||[]).join(', ')):(m.configured?'<a href="/auth/login?next=/onboard">Sign in with Microsoft 365</a>':'')}
let CAT={catalog_prices:{},lines:{}};
function avVendor(){return $('av_s1').checked?'sentinelone':($('av_eset').checked?'eset':'')}
function lineKey(k){return k==='av'?avVendor():k}
let avTouched=false;const EDITED={};  // prices typed by the user, per line key — kept across re-renders
function renderLines(){const p=CAT.catalog_prices||{};const keep={};ROWS.forEach(([k])=>{const e=$('q_'+k);if(e)keep[k]=e.value});
 $('lines').innerHTML=ROWS.map(([k,l])=>{const key=lineKey(k);const cat=p[key];const val=key&&EDITED[key]!==undefined?EDITED[key]:(cat??'');
 const label=k==='av'?(key?' <span class="m">('+esc(CAT.lines[key]||'')+')</span>':' <span class="warn">(tick SentinelOne or ESET)</span>'):'';
 const note=key&&EDITED[key]!==undefined&&cat!==undefined&&+EDITED[key]!==+cat?`<div class="m">catalog $${cat}</div>`:'';
 return `<tr><td>${esc(l)}${label}</td><td><input type="number" min="0" id="q_${k}" value="${esc(keep[k]??0)}"></td>
 <td><input type="number" min="0" step="0.01" id="p_${k}" value="${val}" ${key?'':'disabled'}>${note}</td><td id="t_${k}">$0.00</td></tr>`}).join('');
 ROWS.forEach(([k])=>{['q_','p_'].forEach(pre=>$(pre+k).addEventListener('input',total))});
 ROWS.forEach(([k])=>$('p_'+k).addEventListener('change',e=>{const key=lineKey(k);if(key){EDITED[key]=e.target.value;renderLines()}}));
 ['q_workstations','q_servers'].forEach(id=>$(id).addEventListener('input',()=>{if(!avTouched){$('q_av').value=(+$('q_workstations').value||0)+(+$('q_servers').value||0);total()}}));
 $('q_av').addEventListener('input',()=>{avTouched=true});total()}
function total(){let t=0;ROWS.forEach(([k])=>{const q=+$('q_'+k).value||0,pr=+$('p_'+k).value||0;$('t_'+k).textContent='$'+(q*pr).toFixed(2);t+=q*pr});$('total').textContent='$'+t.toFixed(2);valid()}
let timer=null;$('legal').addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(check,500)});
$('code').addEventListener('change',()=>check(true));
// Exactly one antivirus: ticking one unticks the other
$('av_s1').addEventListener('change',()=>{if($('av_s1').checked)$('av_eset').checked=false;renderLines()});
$('av_eset').addEventListener('change',()=>{if($('av_eset').checked)$('av_s1').checked=false;renderLines()});
async function check(codeOnly){const n=$('legal').value.trim();if(n.length<2){return}
 $('namecheck').textContent='Checking ConnectWise and every tool…';
 const q=new URLSearchParams({legal_name:n});if(codeOnly&&$('code').value)q.set('client_code',$('code').value);
 CHK=await (await fetch('/api/onboarding/check?'+q)).json();CAT={...CAT,catalog_prices:CHK.catalog_prices,lines:CHK.lines};
 if(!codeOnly||!$('std').value)$('std').value=CHK.suggested_standard_name;$('code').value=CHK.suggested_client_code;
 $('alertmail').textContent=CHK.alert_email;if(!$('start').value)$('start').value=CHK.default_start_date;
 let s=[];if(CHK.standard_name_was_shortened)s.push('<span class="warn">Standard name shortened to 30 characters — adjust if needed.</span>');
 s.push(CHK.client_code_status==='ok'?`<span class="ok">Code ${esc(CHK.suggested_client_code)} is available.</span>`:`<span class="bad">Code ${esc(CHK.suggested_client_code)}: ${esc(CHK.client_code_status)}</span>`);
 $('namecheck').innerHTML=s.join(' ');
 $('matches').innerHTML=CHK.matches.length?('<div class="'+(CHK.likely_duplicate?'bad':'warn')+'" style="margin-top:8px">'+(CHK.likely_duplicate?'Likely duplicate — this company may already exist:':'Similar companies already exist:')+'</div>'+
  CHK.matches.map(m=>`<div class="match">• ${esc(m.name)} <span class="m">(${esc(m.source)}${m.identifier?' · '+esc(m.identifier):''}${m.status?' · '+esc(m.status):''} · ${m.score}% match)</span></div>`).join('')):'<div class="ok" style="margin-top:8px">No existing company looks like this one.</div>';
 renderLines()}
const REQ=['legal','std','code','a1','city','state','zip','phone','cf','cl','ce','cp'];
REQ.forEach(id=>$(id).addEventListener('input',valid));$('allow').addEventListener('change',valid);
function valid(){const missing=REQ.filter(id=>!$(id).value.trim());let m='';
 if(missing.length)m='Fill in every field.';else if(!/^[A-Za-z0-9 -]{1,30}$/.test($('std').value))m='Standard name: letters, digits, spaces, hyphens; max 30.';
 else if(!/^[A-Za-z]{2,4}$/.test($('code').value))m='Client code: 2–4 letters.';
 else if(CHK&&CHK.likely_duplicate&&!$('allow').checked)m='Possible duplicate — confirm it is a different company.';
 else if(!avVendor())m='Tick SentinelOne or ESET.';
 else if(!(+$('q_workstations').value||+$('q_servers').value))m='Add at least one workstation or server.';
 $('msg').textContent=m;$('go').disabled=!!m;return !m}
$('go').addEventListener('click',async()=>{if(!valid())return;$('go').disabled=true;$('msg').textContent='Building the preview…';
 const prices={};ROWS.forEach(([k])=>{const v=$('p_'+k).value;if(v!=='')prices[lineKey(k)]=+v});
 const params={legal_name:$('legal').value.trim(),standard_name:$('std').value.trim(),client_code:$('code').value.trim().toUpperCase(),
  address1:$('a1').value.trim(),address2:$('a2').value.trim()||undefined,city:$('city').value.trim(),state:$('state').value.trim().toUpperCase(),zip:$('zip').value.trim(),phone:$('phone').value.trim(),
  contact_first:$('cf').value.trim(),contact_last:$('cl').value.trim(),contact_email:$('ce').value.trim(),contact_phone:$('cp').value.trim(),
  workstations:+$('q_workstations').value||0,servers:+$('q_servers').value||0,network:(+$('q_network').value||0)>0,sc_clients:+$('q_sc_clients').value||0,
  av_vendor:avVendor(),av_quantity:+$('q_av').value||0,prices,start_date:$('start').value,allow_similar:$('allow').checked};
 const r=await fetch('/api/changes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:'onboard_client',params})});
 const j=await r.json();if(!r.ok){$('msg').innerHTML='<span class="bad">'+esc(j.detail||r.status)+'</span>';$('go').disabled=false;return}
 location.href='/changes/'+j.id});
who();renderLines();
fetch('/api/onboarding/catalog').then(r=>r.json()).then(c=>{CAT=c;$('alertmail').textContent=c.alert_email;
 if(!$('start').value)$('start').value=c.default_start_date;renderLines()});
</script></body></html>"""


@router.get("/onboard", response_class=HTMLResponse)
async def onboard_page() -> HTMLResponse:
    return HTMLResponse(_PAGE)
