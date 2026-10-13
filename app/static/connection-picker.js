// Add-a-connection picker, shared by /connections and the Get started wizard (/setup).
// Tabs: Templates (well-known APIs: type only your key) · Public directory (APIs.guru) · Custom (the full form) ·
// Import file (settings exported from another install). Uses the page's CSS variables (--card, --line, --fg, --bg,
// --muted, --accent, --ok, --bad, --warn).
//
//   ConnectionPicker.mount(element, {onSaved(result), onCustom(), onOpen(slug), onBuiltin(platform), canImport})
// Find an API: one search over the templates (instant) and the public directory (as you type).
"use strict";
(function () {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const AUTH_LABELS = {none: "None", api_key: "API key", basic: "Username and password", bearer: "Bearer token",
                       headers: "Custom headers (JSON)", oauth2_client_credentials: "OAuth 2 client credentials", wsse: "WS-Security"};
  const FIELD_LABELS = {api_key: "API key", api_key_name: "Header or parameter name", api_key_in: "Sent in (header or query)",
                        username: "Username", password: "Password", token: "Token", headers: "Headers as JSON",
                        client_id: "Client ID", client_secret: "Client secret", token_url: "Token URL", scope: "Scope",
                        audience: "Audience", client_auth: "Client auth (body or basic)"};
  const SECRETS = new Set(["api_key", "password", "token", "client_secret"]);
  const CSS = `
.cp-tabs{display:flex;gap:4px;flex-wrap:wrap;margin:4px 0 12px}
.cp-tabs button{margin:0;padding:6px 12px;border-radius:99px;border:1px solid var(--line);background:transparent;color:var(--fg);font:inherit;font-size:13px;cursor:pointer}
.cp-tabs button.on{background:var(--accent);border-color:var(--accent);color:var(--accent-fg,#fff)}
.cp-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:8px;margin-top:10px}
.cp-card{text-align:left;margin:0;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--fg);font:inherit;cursor:pointer}
.cp-card:hover{border-color:var(--accent)} .cp-card b{display:block;font-size:14px} .cp-card span{display:block;font-size:12px;color:var(--muted);margin-top:3px}
.cp-chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.cp-chips button{margin:0;padding:3px 10px;border-radius:99px;border:1px solid var(--line);background:transparent;color:var(--muted);font:inherit;font-size:12px;cursor:pointer}
.cp-chips button.on{color:var(--accent);border-color:var(--accent)}
.cp-in{width:100%;box-sizing:border-box;font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg)}
.cp-lbl{display:block;font-size:13px;color:var(--muted);margin:10px 0 4px}
.cp-help{font-size:12px;color:var(--muted);margin-top:3px}
.cp-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px}
.cp-btn{margin:0;padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);font:inherit;cursor:pointer}
.cp-btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-fg,#fff)} .cp-btn:disabled{opacity:.5;cursor:default}
.cp-out{font-size:13px;margin-top:10px} .cp-ok{color:var(--ok)} .cp-bad{color:var(--bad)} .cp-warn{color:var(--warn)} .cp-m{color:var(--muted);font-size:13px}
.cp-pill{display:inline-block;font-size:11px;padding:0 7px;border:1px solid var(--line);border-radius:99px;color:var(--muted);margin-left:6px;vertical-align:1px}
.cp-back{background:none;border:0;color:var(--accent);padding:0;margin:0 0 6px;font:inherit;font-size:13px;cursor:pointer}
.cp-table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px} .cp-table td,.cp-table th{text-align:left;padding:6px;border-top:1px solid var(--line)}`;

  async function call(path, opts = {}) {
    const r = await fetch(path, {headers: {"Content-Type": "application/json"}, ...opts});
    const data = await r.json().catch(() => ({}));
    if (r.status === 401) { location.href = "/auth/login?next=" + encodeURIComponent(location.pathname + location.search); throw new Error("Sign in"); }
    if (!r.ok) throw new Error((typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) || ("HTTP " + r.status));
    return data;
  }

  function mount(root, opts = {}) {
    if (!document.getElementById("cp-style")) {
      const st = document.createElement("style"); st.id = "cp-style"; st.textContent = CSS; document.head.appendChild(st);
    }
    let kinds = null, templates = null, categories = {}, tab = "templates", category = "", query = "";
    const tabs = [["templates", "Find an API"], ["custom", "Custom"],
                  ...(opts.canImport ? [["import", "Import file"]] : [])];
    root.innerHTML = `<div class="cp-tabs">${tabs.map(([k, l]) => `<button type="button" data-tab="${k}">${l}</button>`).join("")}</div><div class="cp-body"></div>`;
    const body = root.querySelector(".cp-body");
    const setTab = (t) => {
      tab = t;
      root.querySelectorAll("[data-tab]").forEach(b => b.classList.toggle("on", b.dataset.tab === t));
      if (t === "custom") { body.innerHTML = ""; if (opts.onCustom) opts.onCustom(); return; }
      if (opts.onLeaveCustom) opts.onLeaveCustom();
      ({templates: showTemplates, import: showImport}[t])();
    };
    root.querySelectorAll("[data-tab]").forEach(b => b.onclick = () => setTab(b.dataset.tab));

    async function getKinds() { return kinds || (kinds = await call("/api/connections/kinds")); }

    // ── Templates ──
    // ── Find an API: templates first, then the public directory, narrowing as you type ──
    let dirTimer = null, dirSeq = 0;
    async function showTemplates() {
      if (!templates) {
        body.innerHTML = '<div class="cp-m">Loading…</div>';
        const r = await call("/api/connections/templates"); templates = r.templates; categories = r.categories;
      }
      body.innerHTML = `<input class="cp-in" id="cp-q" placeholder="Type to search: ConnectWise, Control, ESET, Microsoft 365, GitHub, weather…" value="${esc(query)}" autocomplete="off">
        <div class="cp-chips">${[["", "All"], ...Object.entries(categories)].map(([k, l]) =>
          `<button type="button" data-cat="${k}" class="${k === category ? "on" : ""}">${esc(l)}</button>`).join("")}</div>
        <div class="cp-grid" id="cp-cards"></div><div id="cp-dir"></div>`;
      const builtinOn = (t) => t.builtin && opts.onBuiltin;
      const render = () => {
        const words = query.toLowerCase().split(/ +/).filter(Boolean);
        const list = templates.filter(t => (!category || t.category === category) &&
          words.every(w => (t.name + " " + (t.aliases || []).join(" ") + " " + t.description + " " + t.id).toLowerCase().includes(w)));
        body.querySelector("#cp-cards").innerHTML = list.map(t => `<button type="button" class="cp-card" data-t="${esc(t.id)}">
          <b>${esc(t.name)}${builtinOn(t) ? '<span class="cp-pill" style="display:inline">built-in</span>' : t.starter ? '<span class="cp-pill" style="display:inline">starter</span>' : ""}</b>
          <span>${esc(t.description)}</span></button>`).join("");
        body.querySelectorAll("[data-t]").forEach(b => b.onclick = () => {
          const t = templates.find(x => x.id === b.dataset.t);
          if (builtinOn(t)) opts.onBuiltin(t.builtin); else templateForm(t);
        });
        searchDirectory(list.length);
      };
      // The directory is searched as you type (after a pause), below the templates.
      const searchDirectory = (templateHits) => {
        clearTimeout(dirTimer);
        const box = body.querySelector("#cp-dir"), q = query.trim(), seq = ++dirSeq;
        if (q.length < 2) { box.innerHTML = templateHits ? "" : '<div class="cp-m" style="margin-top:8px">Nothing matches. Keep typing to search the public directory too.</div>'; return; }
        box.innerHTML = '<div class="cp-m" style="margin-top:10px">Searching the public directory…</div>';
        dirTimer = setTimeout(async () => {
          try {
            const r = await call("/api/connections/directory?limit=12&q=" + encodeURIComponent(q));
            if (seq !== dirSeq) return;
            box.innerHTML = r.results.length ? `<div class="cp-m" style="margin-top:14px">From the public API directory (<a href="https://apis.guru" target="_blank" rel="noopener">APIs.guru</a>, quality varies):</div>
              <div class="cp-grid">${r.results.map((e, i) => `<button type="button" class="cp-card" data-d="${i}"><b>${esc(e.title)}</b>
                <span>${esc(e.provider)}${e.categories.length ? " · " + esc(e.categories.join(", ")) : ""}</span><span>${esc(e.description.slice(0, 110))}</span></button>`).join("")}</div>`
              : (templateHits ? "" : '<div class="cp-m" style="margin-top:8px">Nothing found. Use Custom to connect it with its definition.</div>');
            box.querySelectorAll("[data-d]").forEach(b => b.onclick = () => directoryForm(r.results[+b.dataset.d]));
          } catch (e) {
            if (seq === dirSeq) box.innerHTML = `<div class="cp-m" style="margin-top:8px">Public directory unavailable: ${esc(e.message)}</div>`;
          }
        }, 350);
      };
      body.querySelector("#cp-q").oninput = (e) => { query = e.target.value; render(); };
      body.querySelectorAll("[data-cat]").forEach(b => b.onclick = () => {
        category = b.dataset.cat; body.querySelectorAll("[data-cat]").forEach(x => x.classList.toggle("on", x === b)); render(); });
      render();
      body.querySelector("#cp-q").focus();
    }

    function fieldsHtml(fields) {
      return fields.map(f => `<label class="cp-lbl" for="cp-f-${esc(f.key)}">${esc(f.label)}${f.required ? "" : " (optional)"}</label>
        <input class="cp-in" id="cp-f-${esc(f.key)}" data-field="${esc(f.key)}" type="${f.secret ? "password" : "text"}" autocomplete="off"
          value="${esc(f.default || "")}" placeholder="${esc(f.placeholder || "")}">
        ${f.help || f.docs ? `<div class="cp-help">${esc(f.help || "")}${f.docs ? ` <a href="${esc(f.docs)}" target="_blank" rel="noopener">Where do I get this?</a>` : ""}</div>` : ""}`).join("");
    }
    function authHtml(type, hint) {
      const spec = (kinds.auth_types || {})[type] || {secrets: [], settings: []};
      return `<div class="cp-lbl" style="margin-top:14px"><b>Sign-in: ${esc(AUTH_LABELS[type] || type)}</b> (read from the definition)</div>` +
        [...spec.secrets, ...spec.settings].map(k => `<label class="cp-lbl">${esc(FIELD_LABELS[k] || k)}</label>
          <input class="cp-in" data-auth="${esc(k)}" type="${SECRETS.has(k) ? "password" : "text"}" autocomplete="off" value="${esc((hint || {})[k] || "")}">`).join("");
    }
    function readAuth(scope, type) {
      const auth = {type};
      scope.querySelectorAll("[data-auth]").forEach(i => {
        const v = i.value.trim(); if (!v) return;
        if (i.dataset.auth === "headers") { try { auth.headers = JSON.parse(v); } catch { throw new Error("Headers must be JSON"); } }
        else auth[i.dataset.auth] = v;
      });
      return auth;
    }
    function previewHtml(r) {
      return `<span class="cp-ok">Found ${r.operations} endpoint${r.operations === 1 ? "" : "s"}</span> (${r.read_operations} read, ${r.write_operations} write)
        ${r.base_url ? ` at ${esc(r.base_url)}` : ""}${(r.warnings || []).length ? `<div class="cp-warn">${r.warnings.map(esc).join("<br>")}</div>` : ""}`;
    }

    // One form for a template or a directory entry: name + fields → Test → (sign-in fields if detected) → Save.
    async function connectForm({title, description, note, fields, defaultName, back, preview, save, detect}) {
      await getKinds();
      body.innerHTML = `<button type="button" class="cp-back">‹ Back</button>
        <div><b style="font-size:16px">${esc(title)}</b></div>${description ? `<div class="cp-m">${esc(description)}</div>` : ""}
        ${note ? `<div class="cp-help">${note}</div>` : ""}
        <label class="cp-lbl" for="cp-name">Name</label><input class="cp-in" id="cp-name" value="${esc(defaultName)}">
        ${fieldsHtml(fields)}<div id="cp-auth"></div>
        <div class="cp-row"><button type="button" class="cp-btn" id="cp-test">Test</button>
          <button type="button" class="cp-btn primary" id="cp-save" ${detect ? "disabled" : ""}>Save</button></div>
        <div class="cp-out" id="cp-out"></div>`;
      body.querySelector(".cp-back").onclick = back;
      let detected = null;
      const values = () => {
        const v = {}; body.querySelectorAll("[data-field]").forEach(i => { v[i.dataset.field] = i.value.trim(); }); return v;
      };
      const out = body.querySelector("#cp-out");
      const authNow = () => detected && detected !== "none" ? readAuth(body.querySelector("#cp-auth"), detected) : (detected ? {type: "none"} : null);
      body.querySelector("#cp-test").onclick = async () => {
        out.innerHTML = '<span class="cp-m">Reading the definition…</span>';
        try {
          const r = await preview(body.querySelector("#cp-name").value.trim(), values(), authNow());
          out.innerHTML = previewHtml(r);
          if (detect && detected === null) {
            detected = r.auth_type || "none";
            body.querySelector("#cp-auth").innerHTML = detected === "none" ? '<div class="cp-m" style="margin-top:10px">No sign-in needed.</div>' : authHtml(detected, r.auth_hint);
          }
          body.querySelector("#cp-save").disabled = false;
        } catch (e) { out.innerHTML = `<span class="cp-bad">${esc(e.message)}</span>`; }
      };
      body.querySelector("#cp-save").onclick = async () => {
        out.innerHTML = '<span class="cp-m">Saving…</span>';
        try {
          const r = await save(body.querySelector("#cp-name").value.trim(), values(), authNow());
          out.innerHTML = `<span class="cp-ok">Saved: ${r.operations} endpoints${r.pinned && r.pinned.length ? `, ${r.pinned.length} made into tools` : ""}.</span>`;
          if (opts.onSaved) opts.onSaved(r);
        } catch (e) { out.innerHTML = `<span class="cp-bad">${esc(e.message)}</span>`; }
      };
    }

    function templateForm(t) {
      const send = (dry) => (name, values, auth) => call(`/api/connections/from-template${dry ? "?dry_run=true" : ""}`,
        {method: "POST", body: JSON.stringify({template: t.id, name, values, ...(auth ? {auth} : {})})});
      connectForm({
        title: "Connect " + t.name, description: t.description, fields: t.fields, defaultName: t.name, back: showTemplates,
        note: t.starter ? "Starter definition: the common read endpoints. Add more later with Custom, or re-import a fuller definition." :
              (t.kind === "sql" || t.kind === "mongodb" ? "Read-only tools: list tables, describe them, and run read queries." : ""),
        preview: send(true), save: send(false), detect: t.detect_auth,
      });
    }

    function directoryForm(e) {
      const send = (dry) => (name, values, auth) => call(`/api/connections${dry ? "?dry_run=true" : ""}`, {method: "POST", body: JSON.stringify({
        kind: "openapi", name, base_url: values.base_url || "", source: {url: e.spec_url, use_auth: false}, auth: auth || {type: "none"},
        settings: {directory: e.id}})});
      connectForm({
        title: "Connect " + e.title, description: e.description, defaultName: e.title, back: showTemplates, detect: true,
        fields: [{key: "base_url", label: "Base URL", required: false, help: "Only if the definition doesn't say where the API lives"}],
        note: `Definition: <a href="${esc(e.spec_url)}" target="_blank" rel="noopener">${esc(e.spec_url)}</a>`,
        preview: send(true), save: send(false),
      });
    }

    // ── Import a settings file ──
    function showImport() {
      body.innerHTML = `<div class="cp-m">A file exported from another API Tools install (Connections → Export settings).
        Credentials are never in it: you'll add them after importing.</div>
        <input type="file" id="cp-file" accept=".json,application/json" style="margin-top:10px"><div id="cp-iout"></div>`;
      body.querySelector("#cp-file").onchange = async (ev) => {
        const out = body.querySelector("#cp-iout");
        let data;
        try { data = JSON.parse(await ev.target.files[0].text()); } catch { out.innerHTML = '<div class="cp-bad">Not a JSON file</div>'; return; }
        try {
          const plan = await call("/api/admin/connections/import?dry_run=true", {method: "POST", body: JSON.stringify(data)});
          const rows = (res) => `<table class="cp-table"><tr><th>Connection</th><th>Result</th></tr>${res.map(r => `<tr><td>${esc(r.name || r.slug)}
            <span class="cp-pill">${esc(r.kind)}</span></td><td>${esc(r.action)}${r.reason ? ": " + esc(r.reason) : ""}
            ${r.needs_credentials ? ` <span class="cp-warn">needs credentials</span>${r.action === "created" && opts.onOpen ? ` <a href="#" data-open="${esc(r.slug)}">add them</a>` : ""}` : ""}</td></tr>`).join("")}</table>`;
          out.innerHTML = rows(plan.results) + `<div class="cp-row"><button type="button" class="cp-btn primary" id="cp-imp"
            ${plan.results.some(r => r.action === "create") ? "" : "disabled"}>Import ${plan.results.filter(r => r.action === "create").length}</button></div>`;
          body.querySelector("#cp-imp").onclick = async () => {
            out.innerHTML = '<div class="cp-m">Importing…</div>';
            try {
              const done = await call("/api/admin/connections/import", {method: "POST", body: JSON.stringify(data)});
              out.innerHTML = rows(done.results);
              out.querySelectorAll("[data-open]").forEach(a => a.onclick = (e2) => { e2.preventDefault(); opts.onOpen(a.dataset.open); });
              if (opts.onSaved) opts.onSaved({imported: true});
            } catch (e) { out.innerHTML = `<div class="cp-bad">${esc(e.message)}</div>`; }
          };
        } catch (e) { out.innerHTML = `<div class="cp-bad">${esc(e.message)}</div>`; }
      };
    }

    setTab(opts.start || "templates");
    return {setTab};
  }

  window.ConnectionPicker = {mount};
})();
