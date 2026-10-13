---
name: msp-changes
description: Make changes in PSA, Asio, ScreenConnect, SentinelOne or ESET — rename clients, fix PSA configurations, decommission/offboard devices, create or update tickets, or call any vendor write endpoint. Every change is proposed here and only runs after a signed-in Editor submits it and an Approver gives final approval. Use for "rename X", "offboard machine Y", "add a note to ticket 123", "inactivate this config", "fix endpoint IDs for APH".
---

# Proposing changes

Load `msp-api` first. **You cannot make changes directly — you propose them.** Every change goes:

1. **Propose** (you, via `propose_change`) → a preview with the exact requests and the current state.
2. **Submit** — an Editor signs in on the approval page and confirms.
3. **Final approval** — an **Approver** approves; only then does anything run. Results land on the same page.

## How to do it
1. `list_commands` (or `describe_command`) to pick the right command; prefer a specific command over
   `vendor_call`. Resolve the client first (`search_clients` / `get_client`) and devices (`find_device`,
   `client_devices`) so parameters are exact IDs — never guess IDs.
2. `propose_change(command, params)`. If it errors (validation, policy, "nothing to change"), explain why
   in plain words and fix the parameters — don't retry blindly.
3. Show the user: change number, title, risk, each step in one line, any warnings, and the
   **approval_url**. Say clearly: *"Nothing has run yet. Open the link (office/VPN), sign in with
   Microsoft 365 and Submit; an Approver gives final approval."*
4. If asked later, `get_change` for status/results; `cancel_change` only for your own unsubmitted ones.

## Commands
| Need | Command |
|---|---|
| New ticket / update / note / time | `ticket_create`, `ticket_update`, `ticket_add_note`, `ticket_add_time` |
| Push approved standard name to PSA, S1, every SC session | `apply_standard_name` |
| Inactivate duplicate PSA configurations (keeps the Asio-linked one) | `psa_inactivate_duplicates` |
| Fill missing PSA Endpoint IDs from Asio (after duplicates are resolved) | `fix_psa_endpoint_ids` |
| Inactivate a PSA configuration (never delete) | `psa_inactivate_configuration` |
| Set one PSA Endpoint ID | `psa_set_endpoint_id` |
| Decommission S1 agent(s) / remove Asio endpoint | `s1_decommission_agent`, `asio_remove_endpoint` |
| Retire a machine everywhere | `offboard_device` or `playbook:offboard-device` |
| Standardize a client in one go | `playbook:standardize-client` |
| Anything else in a vendor's API spec | `vendor_call` (policy blocks users/roles/security/billing/agent uninstall) |

## Rules
- Deletes/decommissions are high risk: state exactly what will be removed and from which tool.
- `apply_standard_name` requires the client's standard name to be approved in the registry first.
- One change per user intent; combine related steps with a playbook instead of many small changes.
- Never claim a change is done until `get_change` shows `done`.
