# Security

This server holds credentials for your other systems and can act on them, so every request is checked and
every action is recorded. This page explains how.

## Who can get in
**Every page and every API call needs a caller**, on the LAN as well as from outside. Only health, sign-in, the
OAuth endpoints, the first-run setup page and the downloadable bridge script are public; none of them return
data.

- **People** sign in with:
  - single sign-on (Microsoft 365 / Entra ID, or any OpenID Connect provider; tokens are verified against the
    provider's published keys), or
  - a local account: a password stored with scrypt, **and** an authenticator code. Codes can't be replayed,
    and repeated failures lock the account temporarily.
  - Browser sessions use secure, HTTP-only cookies; cross-site form posts are refused.
- **Programs and AI clients** use a **connection key** (`msp_…`):
  - 256 random bits, stored only as a hash, shown once
  - carries a role (Reader or Editor, never Approver or Admin) and scopes (`<api>:read|write`)
  - can have an expiry date; revoking a key also kills every OAuth token issued from it
- **claude.ai connectors** use **OAuth 2.1**: PKCE, single-use codes, 1-hour access tokens and rotating refresh
  tokens, with only Claude, VS Code and loopback redirect addresses allowed. A token is bound to **one MCP
  server** (RFC 8707): a token for `/mcp/petstore` is refused everywhere else.
- **First-run setup** works only while nobody can sign in yet, only from your network, and only with the setup
  code from the server log. That code is derived from `MSP_MASTER_KEY`, so you need access to the server
  to read it.

## What they can do
- **Roles:** Reader (read, call read-only operations, propose) → Editor (writes where the write mode allows,
  submit changes) → Approver (final approval; manage connections, credentials and write modes) → Admin (users,
  keys, sign-in, certificates, packs, audit log). Each includes the one before it; the full table is in
  INSTALL.md and in Get started.
- **Scopes** on keys and tokens limit which APIs a caller can see at all. That includes searches, MCP servers,
  the hub and stored local copies.
- **The MSP pack** (when enabled) mixes every vendor's data in one database, so its scope rule is all-or-nothing:
  `/msp/mcp` tool calls, its REST API (`/api/clients`, `/api/db`, `/api/tickets`, `/api/av`, `/api/helpdesk`,
  `/api/discrepancies`, `/api/onboarding`, …), MSP change commands and saved reports need **`*:read`**; its routes
  that need the Editor role also need **`*:write`**. A key scoped to `petstore:read` (or even
  `connectwise_psa:read`) is refused with "This credential's scopes don't include the MSP tools (needs *:read)".
  The raw vendor proxy `/api/proxy/<platform>/…` instead checks that platform's own scope
  (`<platform>:read` / `<platform>:write`). People signing in always have `*:read` (Editors and up `*:write`), and
  claude.ai connectors signed in with single sign-on get the same, so neither is affected.
- **Write modes** per API:
  - read-only is the default for every new connection
  - direct lets Editors write
  - approval turns every write into a change request: an Editor submits it and an Approver gives final approval
- **Read or write is the definition's call, not a person's.** An operation runs without the write mode only when
  its definition says it reads (GET, GraphQL query, MCP `readOnlyHint` …) or a shipped template lists it as a
  read (ScreenConnect, ESET MSP and other vendors that read with POST). Anyone managing connections can mark an
  operation as a write, but nobody can mark a write (POST, mutation …) read-only by hand: the request is refused,
  and the call path checks where the label came from too, so a label stored any other way still goes through
  the write mode. Each label change, and each refused attempt, is audit-logged with who and old → new.
- **SQL and MongoDB connections are read-only.** Queries are checked and run in read-only transactions where the
  database supports it.
- **Syncs (local copies)** can only use read operations.

## What it can reach
- Outbound calls go through a **network guard**. It refuses cloud metadata and link-local addresses on every
  hop, including redirects. `MSP_BLOCK_PRIVATE_TARGETS=1` also refuses private and loopback networks.
- **Responses are capped in size, and calls time out.**
- **Imported definitions are parsed safely:** XML through defusedxml, and nested references are cut off.

## Where secrets live
- **Credentials for connected APIs and vendors** are encrypted with AES-256-GCM using `MSP_MASTER_KEY`. They
  never come back through the API: the UI shows only which secrets are set.
- **`.env` holds the master key and database password.** Keep it `chmod 600`. It is excluded from the Docker
  image (`.dockerignore`).
- **Back up `MSP_MASTER_KEY` separately from database backups.**

## What is recorded
The audit log (Admin → audit, or `GET /api/admin/audit`) records:
- every write, every sensitive read, every refused request
- every AI tool call: which server and tool, a hash of the arguments, and the result
- every operation call and its result
- one entry per sync run

It stores who made each call and how they authenticated. Arguments are stored only as a hash, so the log
doesn't collect customer data or secrets.

## The internet edge
`deploy/edge` puts nginx with TLS in front:
- **Firewall:** your firewall admits only the sources you choose (e.g. Anthropic's range for claude.ai).
- **Paths:** nginx passes only the MCP servers and OAuth paths; everything else returns 404 from the internet.
- **App check:** the app enforces the same path limit itself in case nginx is misconfigured. It also refuses
  public Host headers and forwarded public addresses that didn't come through the edge, which blocks tunnels.

## Rate limits
- Per caller: `MSP_RATE_LIMIT_PER_MIN`.
- Per address on anonymous endpoints (sign-in, setup, OAuth): `MSP_RATE_LIMIT_ANON_PER_MIN`.

## If something leaks

| What leaked | What to do |
|---|---|
| A connection key | `python -m app.cli revoke-key <id>` (its OAuth tokens die with it) |
| A person's access | disable them in your identity provider, and `POST /api/admin/oauth/revoke-user` to end their AI connectors now |
| A local password or authenticator | `python -m app.cli set-password` / `reset-totp` |
| A vendor or API credential | rotate it at the vendor, then update the connection |
| `MSP_MASTER_KEY` | treat every stored credential as exposed: rotate them all at the vendors, set a new key, re-enter the credentials |
