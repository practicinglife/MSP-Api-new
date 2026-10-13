---
name: msp-playbooks
description: Multi-step MSP playbooks that bundle several vendor changes into one approved change — standardize a client across all tools, offboard a device everywhere. Use for "standardize APH", "clean up client X", "offboard/retire laptop ABC123", "we replaced this machine".
---

# Playbooks

Load `msp-api` and `msp-changes` first. A playbook is proposed like any command
(`propose_change("playbook:<name>", params)`) and produces **one** change with several steps, approved
once (Editor submits → the Approver's final approval).

| Playbook | Params | What it does |
|---|---|---|
| `playbook:standardize-client` | `client` (id or code) | Applies the approved standard name to PSA, SentinelOne and every ScreenConnect session; fills missing PSA Endpoint IDs from Asio. Steps with nothing to do are skipped with a note. |
| `playbook:offboard-device` | `hostname` | SentinelOne decommission + Asio endpoint removal + PSA configuration inactivated (kept for history). ScreenConnect removal is flagged for the console. |

Before proposing:
- **standardize-client:** `get_client` — the standard name must be approved (`naming_status: approved`) and
  pass the naming standard. If not, tell the user to approve it in the client registry first.
- **offboard-device:** `find_device` — confirm the hostname belongs to exactly one client and is really
  retired (ask if unsure). This is a delete-risk change.

Playbook files live on the server in `app/org_playbooks/*.yaml`; new ones appear automatically.
