---
name: msp-coverage-gaps
description: Find coverage gaps from the static database — clients missing PSA/Asio/ScreenConnect/antivirus, devices missing a required tool, machines in only one tool, double AV, orphans, and what changed since the last snapshot. Use for "which clients need AV?", "what's missing at <client>", "devices only in SentinelOne", "gap analysis", "what changed this week".
---

# Coverage gaps

Load `msp-api` first. Required stack per client: **PSA + Asio + ScreenConnect + exactly one of SentinelOne/ESET**.

## Client level
- `clients_summary` — counts by issue type.
- `list_clients(issue=...)`: `missing:antivirus`, `missing:screenconnect`, `missing:asio`,
  `missing:connectwise_psa`, `antivirus:both`, `multiple-`. Sort by what matters to the user.

## Device level (one client)
`client_devices(client, missing_only=true)` → each hostname with the tools it is in and the tools it
is **missing**. Group the answer:
- In AV only (e.g. SentinelOne) but not PSA/Asio/SC → unmanaged machine or stale AV record
- In PSA but nowhere else → probably retired; PSA config should be inactivated
- Missing ScreenConnect or Asio → agent install needed
Priority: 🔴 no antivirus · 🟠 no RMM/remote access · 🟡 missing from PSA only

## Across all clients
- `db_unmatched(platform=...)` — devices found in only one platform at the last reconcile.
- `unlinked_companies` — companies in a tool that belong to no client (offboarding leftovers, typos).
- `snapshot_diff` / `snapshot_trend` — what got better or worse.

Devices come from the last reconciliation; mention its age (`data_freshness`) if it's over a day old.
Fixes (agent installs, offboarding) are admin actions in the dashboard — recommend, don't perform.
