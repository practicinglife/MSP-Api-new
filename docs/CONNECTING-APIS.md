# Connecting APIs and data sources

Every connection turns a definition into **operations**: callable tools with typed inputs, read-only or write.
Admins (Approver role) create connections; anyone signed in can search and call them, subject to their scopes
and the connection's write mode.

| Kind | Give it | What becomes a tool |
|---|---|---|
| `openapi` | OpenAPI 3 / Swagger 2 spec (URL or pasted JSON/YAML) | every operation (path + method) |
| `postman` | Postman collection v2.0/2.1 JSON | every request; folders → tags, `{{variables}}` carried over |
| `curl` | one or more `curl …` commands (`# name: …` above a command names it) | every command |
| `soap` | WSDL (URL or pasted) | every operation of every service/port (zeep builds the envelopes) |
| `graphql` | endpoint URL (introspection), SDL / introspection JSON, or your own named operations | Query fields (read), Mutation fields (write) |
| `odata` | service URL (its `$metadata` is read) or pasted `$metadata`; V2 + V4, SAP Gateway | list / get / create / update / delete per entity set, function & action imports |
| `sql` | connection string (`secrets.dsn`) + optional saved named queries | `list_tables`, `describe_table`, `sample_rows`, `run_query` (read-only) + one per saved query |
| `mongodb` | connection string (needs `pymongo`) | `list_collections`, `sample_documents`, `find`, `count_documents`, `aggregate` (read-only) |
| `mcp` | another MCP server's URL (streamable HTTP) as the base URL | that server's tools, behind this app's auth and audit |

## Create a connection (API)

Preview first with `?dry_run=true`: you see how many operations it found, the base URL, which auth the
definition asks for and warnings. Nothing is saved.

```bash
curl -X POST "https://tools.example.com/api/connections?dry_run=true" -H "X-API-Key: <admin key>" \
  -H "Content-Type: application/json" -d '{
    "kind": "openapi", "name": "Petstore",
    "source": {"url": "https://petstore3.swagger.io/api/v3/openapi.json"},
    "auth": {"type": "api_key", "api_key": "<secret>", "api_key_name": "X-API-Key"}
  }'
```

Drop `dry_run` to save it. Then:

- `GET /api/connections` — all connections
- `GET /api/connections/{slug}/operations?q=invoice` — search a connection's operations
- `GET /api/connections/{slug}/operations/{name}` — full input/output schema
- `POST /api/connections/{slug}/operations/{name}/call` with `{"args": {…}}` — call it
- `PATCH /api/connections/{slug}/operations/{name}` with `{"pinned": true}`, `{"enabled": false}` or `{"read_only": true}`
- `POST /api/connections/{slug}/reimport` — re-read the definition (your pinned / enabled / read-only choices are kept)
- `PATCH /api/connections/{slug}` — rename, change base URL, rotate credentials (`{"auth": {...}}`), enable/disable

## Auth types

| `auth.type` | Secret fields (encrypted) | Settings |
|---|---|---|
| `none` | – | – |
| `api_key` | `api_key` | `api_key_name` (default `X-API-Key`), `api_key_in` (`header` / `query`) |
| `basic` | `username`, `password` | – |
| `bearer` | `token` | – |
| `headers` | `headers` (object of header → value) | – |
| `oauth2_client_credentials` | `client_id`, `client_secret` | `token_url`, `scope`, `audience`, `client_auth` (`basic` / `body`) |
| `wsse` (SOAP) | `username`, `password` | – |

SQL and MongoDB take `auth: {"dsn": "postgresql://user:pass@host/db"}`. Credentials found inside a definition
(a token in a cURL command, a key in a Postman collection) are moved into the encrypted secrets automatically.

## Writes

Each connection's slug is also its **write mode** platform name (Admin → write modes, or `MSP_WRITE_MODE_<SLUG>`):

- `read_only` (default) — write operations are refused.
- `direct` — Editors (and keys with a `<slug>:write` scope) may call write operations.
- `approval` — a write call returns `202` with a change request; it runs after an Editor submits and an
  Approver approves it.

Read-only is decided per operation from the definition (HTTP GET/HEAD, GraphQL query, OData read, SOAP names
starting Get/List/Find…, MCP `readOnlyHint`) and can be overridden per operation.

## Safety

- Outbound calls never go to link-local / cloud-metadata addresses; set `MSP_BLOCK_PRIVATE_TARGETS=1` to also
  refuse private networks (internet-only installs).
- SQL is limited to single read statements, runs in a read-only transaction where supported and is rolled back.
  MongoDB pipelines may not use `$out`, `$merge`, `$where`, `$function` or `$accumulator`. Still give these
  connections a database user that can only read.
- Every call is in the audit log (who, which operation, a hash of the arguments, result).
- Upstream errors come back as `502` with `upstream_status`, so a vendor's 401 is never confused with this app's.
