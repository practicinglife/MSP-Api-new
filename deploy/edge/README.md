# External access over HTTPS

Lets Claude (claude.ai connectors, Claude Desktop/mobile, Claude Code) and other MCP clients reach this
server from outside your network, while your firewall only admits the traffic you choose. Inside the
network nothing changes: every page and API still requires sign-in (see `app/security.py`).

```
Claude (160.79.104.0/21) ──443──► your firewall (allow Anthropic only) ──► nginx on <server>:443
                                                                            │  only MCP servers + OAuth paths
                                                                            ▼  adds X-MSP-Edge: 1
                                                                          app :8000 → key / OAuth token required
```

Throughout, replace `tools.example.com` with your hostname and `<server>` with the server's LAN address.

## 1. DNS
- Public DNS: `A  tools.example.com  →  <your office public IP>`
- Internal DNS: `tools.example.com → <server>` so office browsers reach the sign-in page directly
  (or enable NAT loopback / hairpin on your firewall and list your public IPs in `OFFICE_IPS`).

## 2. Certificate + nginx (on the Linux server)
```bash
cd /opt/msp-api-tools
sudo DOMAIN=tools.example.com EMAIL=admin@example.com bash deploy/edge/setup-edge.sh http
# no inbound port 80 and your DNS is at GoDaddy? use the DNS challenge instead:
sudo DOMAIN=tools.example.com EMAIL=admin@example.com bash deploy/edge/setup-edge.sh godaddy
```
Optional: `OFFICE_IPS="203.0.113.10 203.0.113.11"` — your office's public IPs, so office browsers that come
in through the public address get the full app (dashboard, sign-in) instead of only MCP/OAuth.

`http` mode needs inbound port 80, used only for `/.well-known/acme-challenge/`. `godaddy` mode asks for a
GoDaddy Production API key once (https://developer.godaddy.com/keys); it is stored only in
`/root/.acme.sh/account.conf` for renewals. Renewal is automatic (acme.sh daily cron, reloads nginx).
Check: `sudo /root/.acme.sh/acme.sh --list`.

Also set in `.env`: `MSP_EDGE_DOMAIN=tools.example.com`, `MSP_EXTERNAL_BASE_URL=https://tools.example.com`,
and `MSP_OFFICE_IPS` (comma separated) to match.

## 3. Firewall (any vendor — SonicWall, FortiGate, pfSense, Meraki, …)
1. Address objects: `Anthropic-Claude` = `160.79.104.0/21` (WAN); `MSP-Tools-Server` = `<server>` (LAN).
2. Inbound NAT: WAN public IP, HTTPS (443) → `MSP-Tools-Server`:443.
3. WAN → LAN access rule: source `Anthropic-Claude`, destination the public IP (pre-NAT), HTTPS, allow.
   Make sure no other rule allows HTTPS from **Any** to this server (port-forward wizards often add one).
4. Only for `setup-edge.sh http`: allow HTTP (80) from Any to the server — nginx answers only the ACME
   challenge on port 80.
5. Outbound: unchanged (the server calls vendor APIs, Let's Encrypt, and your DNS provider).
6. Never forward 8000, 5432, 4040 or 8088 from the WAN.

nginx enforces its own allowlist (Anthropic + private ranges + `OFFICE_IPS`) as a second layer.

## 4. Connection keys
Created by an admin — on the server, or signed in to the dashboard as an Admin/Approver
(`POST /api/admin/api-keys`). The full key is shown once; store it in a password manager.
```bash
docker compose exec app python -m app.cli create-key "claude.ai org" --role user --scope "*:read" --expires-days 365
docker compose exec app python -m app.cli list-keys
docker compose exec app python -m app.cli revoke-key <id>      # also kills every OAuth token issued from it
```
Keys carry a role (`user` = read + propose changes, `editor` = may write) and scopes
(`*:read`, `connectwise_psa:write`, …). Use one key per purpose so you can revoke one without breaking others.

## 5. Connect Claude
MCP servers: `https://tools.example.com/mcp/<connection>` (one per connection), `/mcp/<connection>/<group>`
(hand-picked operations), `/mcp` (hub across every connection) and, with the MSP pack, `/msp/mcp`. The
Connections page shows each one's URL and copy-paste client setups; see also `docs/MCP-CLIENTS.md`.
- **claude.ai / Claude Desktop / mobile (org owner):** Organization settings → Connectors → Add custom
  connector → the server's URL (leave OAuth client fields blank). Each member clicks **Connect**: with single
  sign-on configured they sign in with their own account; otherwise they enter a connection key. The token
  they get works only for that one server. Do the Connect step from the office/VPN (the browser opens the
  sign-in page on this server).
- **Claude Code:**
  `claude mcp add --transport http petstore https://tools.example.com/mcp/petstore --header "X-API-Key: <key>"`
- **VS Code / Copilot / Cursor:** HTTP MCP server with header `X-API-Key: <key>`.

## Security model
Layers, outermost first — every layer must fail before data is reachable:
1. **Firewall** — 443 only from Anthropic `160.79.104.0/21`. That range is shared by every Claude customer,
   so it stops scanners but not another Claude user — the key / sign-in is the real control.
2. **nginx** — same allowlist + LAN; outside sources reach only the MCP servers and the OAuth paths (everything
   else 404); GET/POST only; rate limits; TLS 1.2/1.3 + HSTS.
3. **App boundary** (`app/security.py`) — refuses public Host headers or public X-Forwarded-For that didn't
   come through nginx (tunnels), and limits edge traffic to the published paths even if nginx were misconfigured.
4. **Authentication everywhere** (`app/security.py`, `app/auth/`) — every request, LAN included, needs a
   signed-in person (OIDC with verified signatures, or local account with password + authenticator code),
   a connection key (256-bit, stored hashed, scoped, optional expiry), or an OAuth token bound to one MCP
   server (PKCE, single-use codes, 1 h access tokens, rotating refresh tokens, allow-listed redirects).
5. **Authorization** — roles (Reader / Editor / Approver / Admin), per-platform write modes
   (read-only by default), change approvals, audit log of every write and every MCP tool call.

## Verify
```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://tools.example.com/msp/mcp \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'      # 401 without a key
curl -s -X POST https://tools.example.com/msp/mcp -H "X-API-Key: <key>" \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'      # {"result":{}}
```
From outside the office (e.g. a phone hotspot) the first command should **time out** — the firewall only
admits Anthropic.
