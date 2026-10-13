# MCP tools reference

Every MCP tool MSP-API serves, where it is defined, and what it takes to run it. Everything here comes from the
source. The file paths point at the definitions.

**Legend**

| Column | Meaning |
|---|---|
| R / W / X | **R**ead: returns data, changes nothing. **W**rite: changes data in this server or a vendor. **X**: execute, meaning it runs an API operation or a saved workflow whose effect depends on what it calls |
| Required permission | The lowest role that can run it. Roles are Reader < Editor < Approver < Admin (`app/auth/principal.py`) |

Every MCP request also needs a valid credential: a browser session, a connection key (`msp_…`), or an OAuth access
token. A request without one gets `401` from the middleware (`app/security.py`) before any tool code runs.

---

## 1. Core servers (every install)

Defined in [`app/mcp/servers.py`](../app/mcp/servers.py) and routed by
[`app/api/mcp_servers.py`](../app/api/mcp_servers.py).

| URL | Server |
|---|---|
| `POST /mcp` | Hub: every connection the caller's scopes allow |
| `POST /mcp/{connection}` | One connection |
| `POST /mcp/{connection}/{group}` | A hand-picked group of one connection's operations |

| Tool | Server | Purpose | R/W/X | Required permission |
|---|---|---|---|---|
| `list_connections` | hub | Connections the caller may use, each with its write mode and its own MCP URL | R | Reader + read scope |
| `search_operations` | hub, connection, group | Ranked search of operations by plain-language intent (synonyms, typos) | R | Reader + read scope |
| `describe_operation` | hub, connection, group | Full argument schema of one operation, plus the connection's write-mode rule for writes | R | Reader + read scope |
| `call_operation` | hub, connection, group | Runs one operation | X | Reads: Reader + read scope. Writes: depends on the write mode (below) |
| `search_records` | hub, connection, group (only when a local copy exists) | Searches the locally synced copy of API data. No API call | R | Reader + read scope |
| `get_record` | hub, connection, group (only when a local copy exists) | One stored record by key. No API call | R | Reader + read scope |
| *pinned operations* | connection, group | Each pinned operation becomes its own typed tool, with its JSON Schema as the input schema | R or X | Same as `call_operation` for that operation |

### How `call_operation` decides (`app/connections/service.py` → `call_operation`)

| Operation | Write mode `read_only` (default) | `direct` | `approval` |
|---|---|---|---|
| Read-only operation | Runs (Reader + read scope) | Runs | Runs |
| Write operation | Refused (`403`) | Runs only for Editor+ with a `write` scope for that connection | Becomes a change request. Nothing runs until an Editor submits it and an Approver approves it |

Whether an operation counts as a read or a write comes from its definition when it is imported. For OpenAPI,
Postman and cURL, `GET`/`HEAD`/`OPTIONS` requests are reads. For OData, list and get are reads. For SOAP, it is guessed from the
operation name. For GraphQL, queries are reads and mutations are writes. For a proxied MCP server, it is the
remote tool's `readOnlyHint`. SQL and MongoDB connections are read-only. A shipped template can list
vendor reads that use POST. An Approver can mark an operation as a write, but nobody can mark a write read-only
by hand: the request is refused (see [SECURITY.md](SECURITY.md)).

---

## 2. `/msp/mcp`: the MSP pack server

Only present when the MSP pack is enabled (`MSP_PACKS=msp` or chosen in **Get started**). Defined in
[`app/api/org_mcp.py`](../app/api/org_mcp.py) (`TOOLS`). Every tool call is executed by calling this app's own
REST API in-process, **as the original caller**. The request passes through the same authentication and
authorization middleware as a browser or script would (`_internal` → `internal_headers`).

The **list** of tools is the same for every role. Permission is enforced when a tool is **called**:
1. In `_handle`: a non-read-only tool that is not a proposal tool needs Editor (`"… requires the Editor role"`).
2. In the middleware, for the REST call the tool makes: role and write-scope checks (`app/security.py → _authorize`).
3. In the change engine: submit needs Editor, approval needs Approver, and both need a signed-in person
   (`app/api/changes.py`).

### Workflow library

| Tool | Integration | Purpose | R/W/X | Required permission |
|---|---|---|---|---|
| `find_workflow` | Org library | Search saved recipes, playbooks, commands and skills | R | Reader |
| `run_recipe` | Org library | Run a saved read workflow. Recipes may only call read-only `/msp` tools | X (reads only) | Reader |
| `get_skill` | Org library | Current text of an org skill (`app/org_skills/*/SKILL.md`) | R | Reader |
| `save_workflow` | Org library | Propose adding, updating or deleting a library entry. Becomes a change request | W (proposal) | Reader to propose. Editor submits, Approver approves |

### Clients, devices and coverage (stored data)

| Tool | Integration | Purpose | R/W/X | Required permission |
|---|---|---|---|---|
| `search_clients` | Client registry | Find a client by name, code or any tool's company name | R | Reader |
| `get_client` | Client registry | One client with linked tool companies and compliance issues | R | Reader |
| `resolve_client` | Client registry | Which client owns a given tool company | R | Reader |
| `list_clients` | Client registry | All clients, filterable by issue | R | Reader |
| `clients_summary` | Client registry | Counts, compliance and orphan companies per tool | R | Reader |
| `client_devices` | All synced tools | A client's devices across tools, with per-host coverage | R | Reader |
| `find_device` | All synced tools | Find a machine by (partial) hostname | R | Reader |
| `naming_report` | Client registry | Name drift between tools, and naming-standard violations | R | Reader |
| `unlinked_companies` | Client registry | Tool companies that belong to no client | R | Reader |
| `links_to_review` | Client registry | Auto-suggested links waiting for a person | R | Reader |
| `db_unmatched` | Reconciliation | Devices seen in only one platform at the last reconciliation | R | Reader |
| `platform_counts` | Reconciliation | Device and company counts per platform | R | Reader |
| `snapshot_diff` | Reconciliation | Per-company changes between two daily snapshots | R | Reader |
| `snapshot_trend` | Reconciliation | Daily device-count trend | R | Reader |
| `data_freshness` | Reconciliation | Last sync, last reconciliation, recent runs | R | Reader |
| `av_gaps` | Asio, SentinelOne, ESET | Machines without their client's antivirus | R | Reader |
| `missing_asio_agent` | Asio, SentinelOne, ESET | Machines with antivirus but no Asio agent | R | Reader |
| `discrepancies` | Asio, ScreenConnect, PSA, AV | Where the tools disagree, per company or per machine | R | Reader |
| `check_new_client` | PSA, all tools | Duplicate check, suggested name and code before onboarding | R | Reader |
| `refresh_client_registry` | All configured tools | Re-reads company lists from vendors and writes the local registry | W (local) | **Editor** + write scope |

### Live vendor reads

| Tool | Integration | Purpose | R/W/X | Required permission |
|---|---|---|---|---|
| `search_tickets` | ConnectWise PSA | Live ticket search with a conditions string | R (live) | Reader |
| `get_ticket` | ConnectWise PSA | One ticket, optionally with notes | R (live) | Reader |
| `ticket_lookups` | ConnectWise PSA | Boards, priorities, members, work types | R (live) | Reader |
| `helpdesk_incidents` | ConnectWise PSA, SentinelOne | SentinelOne alert tickets grouped per incident | R (live) | Reader |
| `helpdesk_alerts` | ConnectWise PSA | Avast / HPE Instant On alert tickets and company matches | R (live) | Reader |

### Changes (propose, never execute)

| Tool | Integration | Purpose | R/W/X | Required permission |
|---|---|---|---|---|
| `list_commands` | Change engine | Write commands and playbooks that can be proposed | R | Reader |
| `describe_command` | Change engine | Parameter schema of one command | R | Reader |
| `propose_change` | Change engine → vendors | Builds a preview (exact requests + current state) and returns an approval link. **Nothing runs.** | W (proposal) | Reader to propose. **Editor** submits, **Approver** approves (signed-in people only) |
| `get_change` | Change engine | Status, steps, results and audit trail | R | Reader |
| `list_changes` | Change engine | Recent changes by status | R | Reader |
| `cancel_change` | Change engine | Cancel a change that is still `proposed` | W (proposal) | Reader, and only the person or key that requested it |

### Commands an AI client can propose (`propose_change`)

Registered in [`app/services/commands/`](../app/services/commands/). Each produces a plan. It runs only after an
Editor submits it and an Approver gives final approval, and never on a platform whose write mode is `read_only`.
Changes rated `delete` also require typing `DELETE` when submitting.

| Command | Risk | Platforms |
|---|---|---|
| `ticket_create` | create | ConnectWise PSA |
| `ticket_add_note` | create | ConnectWise PSA |
| `ticket_add_time` | create | ConnectWise PSA |
| `ticket_update` | update | ConnectWise PSA |
| `alert_ticket_triage` | update | ConnectWise PSA |
| `s1_ticket_triage` | update | ConnectWise PSA, SentinelOne |
| `apply_standard_name` | update | ConnectWise PSA, SentinelOne, ScreenConnect |
| `psa_set_endpoint_id` | update | ConnectWise PSA |
| `fix_psa_endpoint_ids` | update | ConnectWise PSA |
| `psa_inactivate_configuration` | update | ConnectWise PSA |
| `psa_inactivate_duplicates` | update | ConnectWise PSA |
| `psa_inactivate_orphans` | update | ConnectWise PSA |
| `av_flag_for_review` | update | Asio |
| `av_remediate` | update | Asio, SentinelOne, ESET |
| `asio_remove_endpoint` | delete | Asio |
| `s1_decommission_agent` | delete | SentinelOne |
| `offboard_device` | delete | SentinelOne, Asio, ConnectWise PSA |
| `onboard_client` | create | ConnectWise PSA, SentinelOne, ESET |
| `onboard_finish` | create | SentinelOne, Asio |
| `offboard_client` | delete | ConnectWise PSA, SentinelOne, ESET |
| `vendor_call` | varies | Any built-in vendor (one raw API write) |
| `connection_call` | varies | Any connection (created automatically by approval-mode writes) |
| `save_workflow` | update | Org library |
| `playbook:offboard-device` | varies | Asio, ConnectWise PSA, SentinelOne |
| `playbook:standardize-client` | varies | ConnectWise PSA, ScreenConnect, SentinelOne |

### Prompts

`/msp/mcp` also serves each org skill (`app/org_skills/<name>/SKILL.md`) as an MCP prompt (`prompts/list`,
`prompts/get`).
