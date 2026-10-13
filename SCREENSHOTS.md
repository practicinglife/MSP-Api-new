# Screenshots

The images in [`docs/images/`](docs/images/) are referenced by the README. All of them must come from a
**demo install** with no real vendor credentials, customer names, internal hostnames or keys.

## Already captured

These were taken from a throwaway local install. It had placeholder users (`admin.demo`, `approver.demo`,
`editor.demo`, `reader.demo`), the Petstore sample API (`api.petstore.example.com`, which doesn't exist) and no vendor
credentials:

| File | Shows |
|---|---|
| `main-interface.png` | Connections page (Admin): built-in integrations, all "not connected" |
| `api-integration-configuration.png` | Connections → Add a connection: the template picker |
| `msp-dashboard.png` | The MSP pack dashboard (empty demo data) |
| `msp-tools.png` | `/msp/connect`: the `/msp` skills and tools |
| `roles-permissions.png` | Get started → Secure it → "Who can do what" |
| `change-approvals.png` | `/changes` as an Approver, with two proposed demo writes |

To re-take them, run a separate demo instance (it never touches `data/`):

```bash
python -m app.cli run --data demo-data --port 9443 --packs msp --sqlite --no-sso
```

Make sure no `.env` with real credentials is present in the folder you run it from.

## Still needed: MCP client screenshots

These show the permission model from the AI side. They have to be taken in a real MCP client (Claude Desktop,
Claude Code, VS Code), so they can't be captured automatically. Use the demo instance above, not a production
server.

### Prepare two demo instances and keys

1. Start a demo **with** the MSP pack (`--packs msp`, port 9443) and one **without** (`--packs none`, port 9444,
   `--data demo-core`).
2. In each, sign in as the demo admin and create two connection keys on the **/setup** page (connection keys),
   or with `python -m app.cli create-key`:
   - `demo-reader`: role `reader`, scope `*:read`
   - `demo-editor`: role `editor`, scopes `*:read`, `*:write`
3. Add the Petstore sample (Connections → Find an API → Any OpenAPI URL, or paste
   `tests/fixtures/connections/petstore.yaml`), and set its write mode to **approval**.

### `mcp-with-msp-tools.png`: MCP WITH /msp tools available

1. Add an HTTP MCP server named `msp` with URL `https://localhost:9443/msp/mcp` and header
   `X-API-Key: <demo-reader key>`.
   - Claude Code: `claude mcp add --transport http msp https://localhost:9443/msp/mcp --header "X-API-Key: <key>"`
2. Open the client's tool list (Claude Code: `/mcp` → `msp` → tools).
3. Capture the list showing `search_clients`, `av_gaps`, `propose_change` and the rest.
4. Crop out the key and any paths that show your user name.

### `mcp-without-msp-tools.png`: MCP WITHOUT /msp tools available

1. Point the same client at the **core-only** instance: `https://localhost:9444/msp/mcp`.
2. The connection fails with `404 Not Found`, because `/msp/mcp` does not exist without the MSP pack. Capture
   the client's error.
3. Optional second panel: add `https://localhost:9444/mcp` (the hub) and capture its tool list:
   `list_connections`, `search_operations`, `describe_operation`, `call_operation`.

### `mcp-role-denied.png` (optional, recommended)

The same `/msp/mcp` server, used with the **reader** key:
1. Ask the assistant to "refresh the client registry".
2. Capture the tool result: `refresh_client_registry requires the Editor role`.
3. Repeat with the editor key and capture the successful result side by side.

### Checklist before committing an image

- [ ] No API keys, `msp_…` connection keys, tokens or passwords visible
- [ ] No customer or company names other than demo data
- [ ] No internal hostnames, IP addresses or public domain names (use `localhost`)
- [ ] No personal user names or email addresses
- [ ] Saved as PNG in `docs/images/` with the file name above
