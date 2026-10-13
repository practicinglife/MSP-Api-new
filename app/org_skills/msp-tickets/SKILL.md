---
name: msp-tickets
description: Read ConnectWise Manage (PSA) service tickets — open tickets, tickets for a client, by board or priority, a single ticket with notes. Use for "show open tickets", "high priority tickets for <client>", "tickets on the Help Desk board", "what's ticket 12345 about".
---

# Tickets (ConnectWise PSA — live reads)

Load `msp-api` first. Tickets are read **live** from PSA (they change constantly), unlike the rest
of the data.

`search_tickets(conditions, limit)` — `conditions` is a CW Manage conditions string:
- Open: `closedFlag=false` (default)
- One client: first `get_client` to get the PSA company id, then `company/id=12345 and closedFlag=false`
  (more reliable than names); by name: `company/name like "%Heating%"`
- Board: `board/name="Help Desk"` (`ticket_lookups(kind="boards")` for names)
- Priority: `priority/name like "%High%"` (`ticket_lookups(kind="priorities")`)
- Recent: `dateEntered > [2026-09-09T00:00:00Z]` — compute from today

`get_ticket(ticket_id, include_notes=true)` for one ticket.

Summarize: total; by status, board, priority (🔴 critical 🟠 high 🟡 medium 🟢 low); then the 10 most
recent with number, summary, client, board, status, priority, entered date.
Creating/updating tickets is not available in the org tools — do it in PSA.
