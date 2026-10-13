---
name: msp-api
description: Base reference for the company MSP API Tools server (ConnectWise PSA, Asio, ScreenConnect, SentinelOne, ESET) — the client registry, which tools to use, data freshness, and safety rules. Load before answering any question about MSP clients, devices, coverage, naming, or tickets.
---

# MSP API Tools — base reference

All data comes from the company MSP server through the `msp` MCP tools. Answers come from a
**static database** that is refreshed on a schedule — not from live vendor calls — so they are fast
and the same for everyone. Say how fresh the data is when it matters (`data_freshness`).

## The client registry is the source of truth
Every customer has a permanent **client id**, a 2–4 letter **client code** (e.g. APH, GLX, INI), and
a **standard name**. Each tool's company is linked to the client by that tool's own ID:

| Tool | What the client is called there |
|---|---|
| ConnectWise PSA | Company (system of record for agreements) |
| Asio (RMM) | Company — names truncated at 30 chars, `& , . '` shown as `_` |
| ScreenConnect | CustomProperty1 on each session — punctuation shown as `-` |
| SentinelOne | Site — often abbreviated (APH, APH-B, Main Street) |
| ESET | Company |

So **never fuzzy-match names yourself** — call `search_clients` or `resolve_client` and use the client
it returns. Required for every client: **PSA + Asio + ScreenConnect + exactly one of SentinelOne or ESET.**

## Reuse before you build
For anything multi-step, call `find_workflow` first and use the saved recipe / playbook / skill it finds.
If you had to work something out that others will ask again, offer to save it (`msp-library` skill).
Common recipes: `client-health`, `onboarding-status`, `coverage-gaps-report`.

## Which tool to use
| Question | Tool |
|---|---|
| "Find / look up <company or abbreviation>" | `search_clients`, then `get_client` |
| "Which client is SentinelOne site X / SC company Y?" | `resolve_client` |
| "Devices for <client>", "what's missing at <client>" | `client_devices` (`missing_only=true`) |
| "Where is machine ABC-123?" | `find_device` |
| "Which clients are missing ScreenConnect / AV / Asio?" | `list_clients` with `issue` |
| Overall health | `clients_summary` |
| Names that differ across tools | `naming_report` |
| Companies in a tool that belong to no client | `unlinked_companies` |
| What changed since yesterday / trend | `snapshot_diff`, `snapshot_trend` |
| Tickets | `search_tickets`, `get_ticket`, `ticket_lookups` (these are **live** PSA reads) |
| How old is the data? | `data_freshness` |

| Make a change (rename, ticket, cleanup, offboard) | `propose_change` — see the `msp-changes` skill |

## Safety
- Reading tools are read-only. Changes are only **proposed** (`propose_change`); they run after a
  Microsoft 365 Editor submits them and an Approver gives final approval. `refresh_client_registry` is safe.
- Never suggest triggering a full reconciliation casually — it freezes the server for 15–25 minutes.
  Admins run it from the dashboard.
- Remediation (installs, uninstalls, offboarding), ticket edits, and renames are done by admins in the
  dashboard, not through these tools.

## Output style
Lead with the answer and numbers. Use the client's standard name and code, e.g. "Acme Plumbing and Heating (APH)". Sort lists by count, show the top 15–25, summarize the rest.
