---
name: msp-company-lookup
description: Look up an MSP client by name, abbreviation, client code, or the name it has in any tool (PSA, Asio, ScreenConnect, SentinelOne, ESET) and show its links, AV, and issues. Use for "find APH", "look up Acme", "what is APH", "which client is SentinelOne site APH-B", "show client APH".
---

# Client lookup

Load `msp-api` first.

1. `search_clients(q)` with whatever the user typed — it matches standard names, client codes, and every
   linked tool's name (so "APH", "APH-B", "Main Street" all work). Abbreviations listed in the org's company-aliases file resolve the same way.
2. One clear hit → `get_client(client)` (id or code). Several → list the top 3–5 with code and ask.
3. If the user gives a tool-specific name, `resolve_client(platform, name)` is exact.

Report:
- **Standard name (code)**, PSA company id, AV vendor
- Each tool and what the client is called there (show drift plainly: "Asio: *Acme Plumbing _ Heating*")
- Compliance issues in plain words: missing tools, two AV products, name drift, links awaiting review
- Offer `client_devices` for device detail

Nothing found → `unlinked_companies` may show it as an orphan in one tool; say so.
