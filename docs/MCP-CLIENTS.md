# Using connections from AI clients (MCP)

Every connection is its own MCP server. Add an API on the Connections page; its MCP server exists immediately.

| URL | What it serves |
|---|---|
| `/mcp/<connection>` | One connection: `search_operations`, `describe_operation`, `call_operation`, plus every **pinned** operation as a tool of its own |
| `/mcp/<connection>/<group>` | A hand-picked set of that connection's operations, each as its own tool (see *Groups*) |
| `/mcp` | The hub: `list_connections` and the same three tools across every connection you may use |
| `/msp/mcp` | The MSP pack's curated tools (only when the pack is enabled) |

All of them speak MCP over streamable HTTP with JSON responses and no sessions. The Connections page shows each
server's URL with ready-to-paste setups (click a connection → **Use with AI clients**, or **Set up** next to the
hub). `GET /api/mcp/servers` returns the same thing as JSON.

## Why search instead of one tool per endpoint
Large APIs have hundreds or thousands of operations. Listing them all as tools overwhelms AI clients and uses up
their context. Instead the client asks `search_operations("open tickets for a customer")`, reads the arguments
with `describe_operation`, and runs `call_operation`. Pin the handful you use all the time and they also appear
as typed tools.

## Local copies
When a connection keeps a local copy of some of its data (see `docs/LOCAL-COPY.md`), its server also has
`search_records` and `get_record`. They answer from this server's database instantly, with no API call, and
the server's instructions tell the client what is kept and when it was last synced. The hub searches every local
copy you may read.

## Credentials
Every client needs one of these:
- **A connection key** (`msp_…`). An admin creates it, on the server or signed in as Admin/Approver:
  `python -m app.cli create-key "Pat - laptop" --role user --scope "petstore:read" --expires-days 365`
  Keys carry a role (`user` reads, `editor` may write) and scopes (`<connection>:read|write`, or `*`). A key
  scoped to `petstore:read` sees only the Petstore server; the hub lists only Petstore for it.
- **OAuth** (claude.ai connectors). The person signs in with single sign-on (or enters a key). The token is
  bound to the one server it was issued for, so a token for `/mcp/petstore` is refused at `/mcp/helpdesk` and at
  `/mcp`.

## Client setup
Replace `tools.example.com` with your server, and `<key>` with the connection key.

**Claude Code**
```bash
claude mcp add --transport http petstore https://tools.example.com/mcp/petstore --header "X-API-Key: <key>"
```

**VS Code** (`.vscode/mcp.json`, prompts for the key once)
```json
{
  "inputs": [{"id": "msp-key", "type": "promptString", "description": "Connection key", "password": true}],
  "servers": {"petstore": {"type": "http", "url": "https://tools.example.com/mcp/petstore",
                           "headers": {"X-API-Key": "${input:msp-key}"}}}
}
```

**Cursor** (`mcp.json`)
```json
{"mcpServers": {"petstore": {"url": "https://tools.example.com/mcp/petstore", "headers": {"X-API-Key": "<key>"}}}}
```

**claude.ai / Claude Desktop / mobile connectors**: add a custom connector with the server URL and leave the
OAuth fields blank. This needs the server reachable from the internet through the edge (`deploy/edge/README.md`).

**Clients that only start local programs** (Claude Desktop chat config, older tools): use the stdio bridge.
Download it from `https://tools.example.com/mcp/bridge.py` (Python 3, standard library only), then:
```json
{"mcpServers": {"petstore": {"command": "python",
  "args": ["C:/Tools/mcp-bridge.py", "https://tools.example.com/mcp/petstore"],
  "env": {"MSP_API_KEY": "<key>"}}}}
```
The bridge sends the key on every request and refuses plain `http://` except to localhost or a LAN address.
The key can also live in `~/.msp-tools/key.txt` instead of the config.

## Writes
Each connection has a write mode (Connections page, or `PUT /api/admin/write-modes/<connection>`):
- **read-only** (default): write operations are refused.
- **direct**: callers with the Editor role and a write scope run them immediately.
- **approval**: each write becomes a change request. The tool returns its link; nothing runs until an Editor
  submits it and an Approver approves it.

The server's instructions tell the AI client which mode applies, so it can say so up front.

## Groups
Publish a hand-picked set of operations as its own server, e.g. a read-only "pets" server for a helpdesk team:
```bash
curl -X PATCH https://tools.example.com/api/connections/petstore -H "X-API-Key: <approver key>" \
  -H "Content-Type: application/json" \
  -d '{"settings": {"mcp_groups": {"pets-read": ["list_pets", "show_pet_by_id"]}}}'
```
`/mcp/petstore/pets-read` then serves exactly those operations, each as its own tool. Search, describe and call
stay inside the group. Group names are lowercase letters, digits, `-` and `_`.

## Audit
Every tool call is in the audit log (Admin → audit, or `GET /api/admin/audit`): who, which server, which tool, the
result status, and a hash of the arguments (not the arguments themselves). Operation calls are logged as
`<connection>.<operation>` with whether they were writes.
