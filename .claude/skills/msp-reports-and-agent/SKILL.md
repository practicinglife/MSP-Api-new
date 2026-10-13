---
name: msp-reports-and-agent
description: Generate customer reports (single company or all companies), list saved reports, and use the app's agent workflow engine (intents, saved workflows, replay, request history). Use for "make a report for <company>", "all companies report", "what workflows exist?", "replay workflow X", "what did the agent do recently?".
---

# Reports and agent workflows

Load the `msp-api` skill first.

## Reports
- One company: `company_report` (`GET /api/reports/company/{name}`) — expand abbreviations first.
- All companies: `all_companies_report` (`GET /api/reports/all`) — heavy; warn it may take a while.
- Saved: `list_saved_reports` (`GET /api/reports/saved`).
- Reconciled device export per company: `GET /api/companies/reconciled/export/{company_name}`.
Summarize the key numbers in chat; give the user the report link/file rather than pasting it whole.

## Agent workflow engine
- `list_intents` (`GET /api/agent/intents`) — what natural-language intents the app understands.
- `agent_request` (`POST /api/agent/request`) — route a request through the app's own agent. Treat as
  potentially [WRITE]: check which intent it maps to before sending anything that changes data.
- `list_workflows`, `get_workflow`, `GET /api/agent/workflows/intent/{intent}`.
- `replay_workflow` (`POST /api/agent/workflows/{id}/replay`) — re-executes; confirm first.
- `list_agent_requests` (`GET /api/agent/requests`) — history.
