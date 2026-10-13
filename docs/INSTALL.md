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
Open `https://<server>:8000/setup` from your network (the app serves HTTPS only; until you install a trusted certificate it uses its own local CA, so the browser warns once), enter that code, choose a username and password, and add
the account to an authenticator app (Microsoft or Google Authenticator, 1Password …). The page works only
while nobody can sign in yet, and never through the internet edge.

(Command-line alternative: `docker compose exec app python -m app.cli create-user admin --role admin`.)

## 4. Work through the checklist
Signed in as the administrator, `/setup` lists what still needs attention, with the exact `.env` lines.
After editing `.env`, run `docker compose up -d` to restart.

**HTTPS.** The app only ever serves HTTPS. Out of the box it uses a certificate from its own local CA (kept in the
`appcerts` volume); set `MSP_CERT_NAMES=server-name,192.168.1.10` in `.env` so it names this server. For a trusted
certificate:
- **Get started → Secure it** installs a Let's Encrypt certificate (DNS record) or one you upload, and restarts the
  app on it.
- Or use `deploy/edge/setup-edge.sh` (nginx + Let's Encrypt), which also restricts what the internet can reach. It
  proxies to the app at `https://127.0.0.1:8000`; re-run it after upgrading from a version that served plain HTTP.
- Then set `MSP_EXTERNAL_BASE_URL=https://tools.example.com`.

**Single sign-on (optional).** Register an app with your identity provider:
- **Redirect URI:** `https://tools.example.com/auth/callback`
- **Microsoft 365 / Entra ID:** set `MSP_ENTRA_TENANT_ID`, `MSP_ENTRA_CLIENT_ID` and `MSP_ENTRA_CLIENT_SECRET`.
  Create app roles with the values `Reader`, `Editor`, `Approver` and `Admin`, and assign them. (Older `MSPTools.User` /
  `.Editor` / `.Approver` / `.Admin` roles keep working.) Or do all of this in **Get started → Secure it**.
- **Any other OpenID Connect provider:** set `MSP_OIDC_ISSUER`, `MSP_OIDC_CLIENT_ID` and `MSP_OIDC_CLIENT_SECRET`.
  `MSP_OIDC_ROLES_CLAIM` names the claim that carries roles or groups. `MSP_OIDC_ROLE_MAP` maps your group names
  to the roles, e.g. `{"it-admins": "Admin", "helpdesk": "Reader"}`.
- Keep one local admin account as a break-glass login.

**Roles.**

Four roles; each includes everything the one before it can do. An Editor submits, an Approver approves: every
approval-mode write needs two people. This is exactly what the app enforces
(`app/auth/principal.py` `ACCESS`, checked by `tests/test_roles.py`), and **Get started → Secure it** shows it:

| Access | Reader | Editor | Approver | Admin |
|---|---|---|---|---|
| Sign in, search and list connections and their operations | ✓ | ✓ | ✓ | ✓ |
| Call read-only API operations (and read local copies) on the connections they may use | ✓ | ✓ | ✓ | ✓ |
| Propose a write: it becomes a change request and runs only once approved | ✓ | ✓ | ✓ | ✓ |
| Call write API operations, on connections whose write mode is `direct` | | ✓ | ✓ | ✓ |
| Submit change requests for approval (approval-mode writes) | | ✓ | ✓ | ✓ |
| Give final approval to change requests (or reject them) | | | ✓ | ✓ |
| Add, edit and delete connections, credentials, built-in integrations and local copies | | | ✓ | ✓ |
| Set write modes; export and import connection settings | | | ✓ | ✓ |
| Users, connection keys, sign-in, certificates, packs, setup and the audit log | | | | ✓ |

Every new connection starts read-only: nobody, not even an Admin, can write through it until an Approver sets its
write mode. Connection keys are Reader or Editor (never Approver or Admin) and are limited further by their scopes.

**Connection keys** (for AI clients and scripts):
```bash
docker compose exec app python -m app.cli create-key "Pat - laptop" --role reader --scope "*:read" --expires-days 365
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

## Settings reference
Settings come from `.env` (template: [`.env.example`](../.env.example)) or from the **Get started** wizard (saved in
`data/settings.json`). Values in `.env` always win.

| Setting | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL (or SQLite) connection string |
| `MSP_MASTER_KEY` | Encrypts stored credentials. Generate with `python -m app.cli gen-master-key`; keep it out of database backups |
| `MSP_PACKS` | `msp` to turn on the MSP pack; empty for core only |
| `MSP_EXTERNAL_BASE_URL` / `MSP_PUBLIC_BASE_URL` | The address AI clients and OAuth use |
| `MSP_ENTRA_*` / `MSP_OIDC_*` | Single sign-on (Microsoft 365 or any OpenID Connect provider), role claim mapping |
| `MSP_WRITE_MODE_DEFAULT`, `MSP_WRITE_MODE_<API>` | Starting write modes (default `read_only`) |
| `MSP_BLOCK_PRIVATE_TARGETS` | Refuse connections to private and loopback addresses |
| `MSP_RATE_LIMIT_PER_MIN`, `MSP_RATE_LIMIT_ANON_PER_MIN` | Rate limits |
| `MSP_EDGE_DOMAIN`, `MSP_OFFICE_IPS` | Internet edge name, and addresses treated as the office |
| `MSP_API_DIRECTORY` | `off` disables the public API directory search |
| `MSP_CERT_NAMES` | Extra names and addresses for the local HTTPS certificate (e.g. the Docker host) |

Admin commands: `python -m app.cli --help`.

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
