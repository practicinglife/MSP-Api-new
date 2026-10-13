---
name: msp-discrepancies
description: Find and resolve where Asio, ScreenConnect, ConnectWise PSA configurations and antivirus disagree — per-company count mismatches and the machines behind them (missing ScreenConnect, not in PSA, orphaned/duplicate PSA configs, missing Asio, wrong company, antivirus missing or on Hyper-V/SQL/DC servers). Use for "discrepancies", "numbers don't match", "what's out of sync", "clean up <client>".
---

# Discrepancies — make the tools agree

Load `msp-api` and `msp-changes` first. Page: `/discrepancies` on the MSP Tools server.

## The standard
Every managed machine is in **Asio + ScreenConnect + an active PSA configuration** (Asio syncs agents into PSA),
and has the client's antivirus — **except Hyper-V hosts, SQL servers (server OS with SQL Server) and domain
controllers, which must have no antivirus**. No tool is the source of truth; every disagreement is listed.

## Read the report
`run_recipe("client-discrepancies")` → per company: Asio · ScreenConnect · PSA active · AV expected · AV installed ·
no-AV servers · stale. Counts that differ from Asio are the headline. Then `{client: "<CODE>"}` for machines.

| Issue | What to do |
|---|---|
| missing ScreenConnect | install ScreenConnect on the machine |
| not in PSA | the Asio→PSA sync missed it — check that company's integration mapping |
| PSA duplicate / PSA without machine | `propose_change("psa_inactivate_orphans", {"client_code": ...})` (Inactive, kept for history) |
| PSA matched by name only | `propose_change("fix_psa_endpoint_ids", {"client": ...})` writes the Asio Endpoint ID |
| missing Asio | install the Asio agent (Remediation tab / ScreenConnect) |
| wrong company | the machine is under a different company in that tool — fix it there (e.g. ScreenConnect CP1) |
| AV missing | Antivirus review → `av_remediate` per client |
| AV on excluded server | remove the antivirus from that Hyper-V host / SQL server / DC |
| stale | no Asio contact 30+ days — confirm retired, then offboard (`playbook:offboard-device`) |

Server roles are detected from Asio (Hyper-V / AD DS roles; SQL Server engine on server OS). If a detection is
wrong, correct it on the discrepancies page (role dropdown — Editor sign-in); the correction wins.
