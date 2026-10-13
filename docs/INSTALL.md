# Installing

## What you need
- A Linux server (or any machine with Docker), 2 CPU / 4 GB RAM is plenty to start
- Docker with the Compose plugin
- A DNS name for the server if people or AI clients will reach it by name (needed for HTTPS)

The app is one container plus PostgreSQL. Everything else (the API definitions you add, local copies, users,
keys) lives in the database.

## 1. Get the code and settings
```bash
git clone <this repository> /opt/api-tools && cd /opt/api-tools
cp .env.example .env
chmod 600 .env
```
In `.env`:
- `POSTGRES_PASSWORD`: a long random password. Use the same one inside `DATABASE_URL`, with host
  `postgres`:
  `DATABASE_URL=postgresql+asyncpg://postgres:<password>@postgres:5432/msp_tools`
- `MSP_MASTER_KEY`: encrypts stored credentials. Generate one:
  `python3 -c "import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`.
  **Keep a copy outside the server.** Without it, stored credentials can't be decrypted.

Everything else can stay blank for now.

## 2. Start it
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose logs -f app      # wait for "Application startup complete"
```
The database schema is created automatically. `docker-compose.prod.yml` keeps the database in a named volume
and doesn't publish PostgreSQL's port. Plain `docker compose up` is the development setup: it reloads code
changes and publishes PostgreSQL on localhost.

## 3. Create the first administrator
```bash
docker compose exec app python -m app.cli setup-code
```
Open `http://<server>:8000/setup` from your network, enter that code, choose a username and password, and add
the account to an authenticator app (Microsoft or Google Authenticator, 1Password …). The page works only
while nobody can sign in yet, and never through the internet edge.

(Command-line alternative: `docker compose exec app python -m app.cli create-user admin --role admin`.)

## 4. Work through the checklist
Signed in as the administrator, `/setup` lists what still needs attention, with the exact `.env` lines.
After editing `.env`, run `docker compose up -d` to restart.

**HTTPS.** Required for real use: sign-in cookies and keys must not cross the network in plain text.
- Use `deploy/edge/setup-edge.sh` (nginx + Let's Encrypt). It also restricts what the internet can reach.
- Or put your own reverse proxy in front.
- Then set `MSP_EXTERNAL_BASE_URL=https://tools.example.com`.

**Single sign-on (optional).** Register an app with your identity provider:
- **Redirect URI:** `https://tools.example.com/auth/callback`
- **Microsoft 365 / Entra ID:** set `MSP_ENTRA_TENANT_ID`, `MSP_ENTRA_CLIENT_ID` and `MSP_ENTRA_CLIENT_SECRET`.
  Create app roles `MSPTools.User`, `MSPTools.Editor`, `MSPTools.Approver` and `MSPTools.Admin`, and assign them.
- **Any other OpenID Connect provider:** set `MSP_OIDC_ISSUER`, `MSP_OIDC_CLIENT_ID` and `MSP_OIDC_CLIENT_SECRET`.
  `MSP_OIDC_ROLES_CLAIM` names the claim that carries roles or groups. `MSP_OIDC_ROLE_MAP` maps your group names
  to the roles, e.g. `{"it-admins": "MSPTools.Admin"}`.
- Keep one local admin account as a break-glass login.

**Roles.**

| Role | Can |
|---|---|
| User | read and search, call read operations, propose changes |
| Editor | also call write operations where the write mode allows, and submit change requests |
| Approver | also give final approval, and manage connections and their credentials |
| Admin | also manage keys, write modes and the audit log |

**Connection keys** (for AI clients and scripts):
```bash
docker compose exec app python -m app.cli create-key "Pat - laptop" --role user --scope "*:read" --expires-days 365
```
Scope a key to the APIs it needs (`petstore:read`, `jira:write` …). Keys are shown once and stored only as a
hash.

## 5. Add APIs
On **Connections** → *Add a connection*. Preview first: nothing is saved until you press Save. New
connections are read-only until an Approver changes their write mode. See
[CONNECTING-APIS.md](CONNECTING-APIS.md), [MCP-CLIENTS.md](MCP-CLIENTS.md) and [LOCAL-COPY.md](LOCAL-COPY.md).

## The MSP pack
1. Add `MSP_PACKS=msp` to `.env`.
2. Add credentials for the vendors you use (ConnectWise PSA, Asio, ScreenConnect, SentinelOne, ESET).
3. Fill in the `MSP_PSA_*` business rules you need. Blank ones turn the feature that depends on them off.
4. Restart.

The MSP dashboard is then at `/`. See [MSP-PACK.md](MSP-PACK.md).

## Backups
Back up the database and keep `MSP_MASTER_KEY` with the backup, stored separately. Without the key,
credentials in the backup can't be read.
```bash
docker compose exec -T postgres pg_dump -U postgres -Fc msp_tools > api-tools-$(date +%F).dump
# restore: docker compose exec -T postgres pg_restore -U postgres -d msp_tools --clean < api-tools-YYYY-MM-DD.dump
```

## Upgrading
```bash
docker compose exec -T postgres pg_dump -U postgres -Fc msp_tools > before-upgrade.dump   # always first
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```
Database migrations run automatically at startup (`python -m app.cli migrate` runs them by hand).

**From a version before packs and migrations (before Phase 5):**
- Add `MSP_PACKS=msp` to `.env`, or the MSP features won't load.
- The first start repairs and adopts the existing database. No data is removed; the log shows
  `db.migrate.adopted_existing_database`.
- The onboarding agreement-type setting is now called `MSP_PSA_AGREEMENT_TYPE_ID`: rename the older `…_AGREEMENT_TYPE_ID` line in `.env`. Managed device types are `MSP_PSA_DEVICE_TYPES`.
- New Asio companies now default to UTC; set `MSP_DEFAULT_TIMEZONE` (e.g. `(UTC-06:00) Central Time (US & Canada)`)
  to keep your own time zone.

## Troubleshooting

| Symptom | Look at |
|---|---|
| "Nobody can sign in yet" in the log | Go to `/setup`, or `python -m app.cli create-user` |
| Can't save credentials | `MSP_MASTER_KEY` is missing or invalid |
| AI client gets 401 | the key (`python -m app.cli list-keys`), or the OAuth token belongs to another MCP server |
| AI client gets 403 on a server | the key has no scope for that API |
| A write is refused | the API's write mode (Connections page) and the caller's role |
| Local copy shows an error | the sync's message on the operation page; the last good copy is kept |
| MSP dashboard missing | `MSP_PACKS=msp` in `.env`, then restart |
