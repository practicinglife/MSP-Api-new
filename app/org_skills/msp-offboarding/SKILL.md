---
name: msp-offboarding
description: Remove a client — delete a test or mistaken client that has no history, or decide when a real client must be inactivated instead. Covers the PSA history checks, SentinelOne/ESET removal, the ESET suspend wait, and what stays manual (Asio, ScreenConnect). Use for "delete client X", "remove the test company", "offboard customer", "we lost this client".
---

# Offboarding / deleting a client

Load `msp-api` and `msp-changes` first. For one machine use `playbook:offboard-device` instead.

## Delete or inactivate?
- **Delete** (`offboard_client`) only when the client has **no history in PSA**: no tickets, time entries,
  invoices, configurations or projects — test companies and onboarding mistakes. The command checks this when
  proposing and again right before deleting; with any history it refuses.
- **Real former clients keep their history** — they are inactivated, not deleted. There is no command for that
  yet; tell the user it's done in PSA by hand (status Inactive, cancel the agreement) and offer to note it.

## Deleting
1. `get_client(code)` — confirm it's the right client and show what's linked.
2. `propose_change("offboard_client", {"client_code": "<CODE>"})` — delete risk: the Editor must type DELETE.
3. What it does, in order: PSA agreement lines → agreement → company (PSA keeps the default contact hidden
   with the deleted company — expected) → SentinelOne site (only if it has no agents) or ESET company
   (**suspend, then waits 1–3 min until ESET allows the delete** — the change runs in the background and its
   page refreshes) → registry entry. Anything already gone is skipped with a note.
4. **Manual:** Asio has no delete API — remove the company in Asio (the Platform). ScreenConnect sessions are
   removed in ScreenConnect.

## Partial clean-ups
If a step failed, fix the cause and propose `offboard_client` again — it only plans what still exists.
