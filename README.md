# API Tools

Connect any API to your AI tools, on your own server. Add an API from its definition; it becomes a searchable
catalogue of operations, its own MCP server for Claude, VS Code, Cursor and other AI clients, and (if you want)
a local copy of its data that refreshes on a schedule. Sign-in, permissions, write approvals and an audit log
cover all of it.

An optional **MSP pack** adds a ready-made integration for ConnectWise PSA, Asio, ScreenConnect, SentinelOne
and ESET.

## What it does

**Any API.** Paste or link a definition and every operation in it becomes callable:

| Kind | You give it | You get |
|---|---|---|
| OpenAPI 3 / Swagger 2 | the spec (URL, paste or file) | every endpoint |
| Postman | a collection (v2.0 / 2.1) | every request, with its auth and variables |
| cURL | one or more curl commands | each command, with its parameters |
| SOAP | a WSDL | every operation |
| GraphQL | the endpoint (introspection) or a schema | every query and mutation |
| OData | the service URL or `$metadata` (V2, V4, SAP Gateway) | every entity set and function |
| SQL | a connection string | read-only tools: tables, columns, queries |
| MongoDB | a connection string | read-only find / count / aggregate |
| MCP | another MCP server's URL | its tools, behind this server's sign-in and audit |

**Search.** Find operations across every API by what you want to do: "open tickets for a customer",
"remove device". It handles synonyms, plurals and typos.

**One MCP server per API.** `/mcp/<api>` offers `search_operations`, `describe_operation` and
`call_operation`, plus the operations you pin as tools of their own. Searching first keeps even APIs with
thousands of endpoints usable. `/mcp` is a hub across everything. OAuth tokens are bound to one server.

**Local copy.** Any read operation can be synced into the database on a schedule (paging handled), then
searched instantly by people and AI clients without calling the API.

**Safe writes.** Each API has a write mode: read-only (the default), direct, or approval, where every write
becomes a change request that an Editor submits, an Approver approves.

**Security everywhere.** Every page and call needs a signed-in person, a connection key or an OAuth token,
including on the LAN. The rest:
- single sign-on (Microsoft 365 or any OpenID Connect provider) or local accounts with an authenticator app
- roles and per-API scopes
- encrypted stored credentials
- an outbound network guard
- rate limits
- an audit log of every write and every AI tool call

## Quick start (this computer, no Docker)

Python 3.12+:
```bash
pip install -r requirements.txt
python -m app.cli run
```
It prints the address (`https://localhost`) and a setup code. Open it, enter the code, create your login (password + authenticator
QR code), and the **Get started** wizard walks you through connecting your tools, the first sync and matching company names.
With no `.env` it uses SQLite and a generated encryption key in `data/` (back that folder up, keep `data/master.key` secret)
and a local HTTPS certificate in `certs/`. Use Docker and PostgreSQL below for a shared server.

## Quick start (Docker)

```bash
git clone <this repository> api-tools && cd api-tools
cp .env.example .env
```
Edit `.env`: set `POSTGRES_PASSWORD` and `MSP_MASTER_KEY` (generate one with
`python -c "import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`), and
fix `DATABASE_URL`'s password to match. Then:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose exec app python -m app.cli setup-code
```
Open `http://<server>:8000/setup`, enter the setup code, and create the first administrator. The setup page
then shows a checklist of what's left: HTTPS, single sign-on, the address AI clients use, and so on. Add
your first API on **Connections**.

Full instructions: [docs/INSTALL.md](docs/INSTALL.md).

## The MSP pack

Set `MSP_PACKS=msp` and your vendor credentials in `.env`, and the app adds:
- ConnectWise PSA, Asio, ScreenConnect, SentinelOne and ESET synced into its database
- a client registry and naming standards
- cross-tool reconciliation and discrepancy reports
- antivirus gap reviews
- helpdesk alert triage
- onboarding and offboarding change templates
- the MSP dashboard
- a curated `/msp/mcp` server with an org library of skills, recipes and playbooks

A new install starts without it. See [docs/MSP-PACK.md](docs/MSP-PACK.md).

## Documentation

| | |
|---|---|
| [INSTALL](docs/INSTALL.md) | Requirements, setup, HTTPS, single sign-on, backups, upgrades |
| [SECURITY](docs/SECURITY.md) | How access is controlled and what to keep safe |
| [CONNECTING-APIS](docs/CONNECTING-APIS.md) | Every kind of API, auth types, write modes |
| [MCP-CLIENTS](docs/MCP-CLIENTS.md) | Using connections from Claude, VS Code, Cursor and others |
| [LOCAL-COPY](docs/LOCAL-COPY.md) | Keeping and searching a local copy of an API's data |
| [MSP-PACK](docs/MSP-PACK.md) | The MSP pack in depth |
| [deploy/edge](deploy/edge/README.md) | Reaching the MCP servers from the internet (nginx + TLS) |

## Development

Python 3.12+. Tests run entirely offline: APIs are mocked, databases are temporary SQLite files, and the test
setup refuses any real network connection.
```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
ruff check app tests
```
**Local HTTPS.** Run the app on this machine at `https://localhost`:
```bash
python -m app.cli make-certs                # local CA (once) + certificate for localhost, in certs/ (git-ignored)
certutil -user -addstore Root certs\ca.crt  # Windows: trust the CA once (confirm the prompt)
```
Set `MSP_EXTERNAL_BASE_URL=https://localhost`, then
`uvicorn app.main:app --host 127.0.0.1 --port 443 --ssl-certfile certs/server.crt --ssl-keyfile certs/server.key`.
For Microsoft 365 sign-in, add `https://localhost/auth/callback` as a redirect URI on the Entra app
registration. Re-run `make-certs --name <host>` to add names; it keeps the same CA.

Schema changes need a migration: `alembic revision --autogenerate -m "what changed"`
(`tests/test_migrations.py` fails until there is one).

## License

MIT. See [LICENSE.txt](LICENSE.txt).
