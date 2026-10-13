---
name: msp-av-remediation
description: Find and fix machines where SentinelOne or ESET didn't deploy (Asio plugin failures) — review the gaps, flag machines into the Asio "No Antivirus – Need Immediate Review" group, redeploy per client through Asio, verify; plus machines with antivirus but no Asio agent. Use for "machines without antivirus", "AV didn't deploy", "redeploy SentinelOne/ESET", "who's missing AV", "not in Asio".
---

# Antivirus gaps — review, flag, redeploy, verify

Load `msp-api` and `msp-changes` first. The page for people: `/av-review` on the MSP Tools server.

## How gaps are found
A nightly **AV sweep** (also "Refresh AV status" on the page) records, per machine in Asio: the antivirus Asio
itself detects, last contact/online (heartbeat), SentinelOne agents and ESET Connect devices. Expected AV is the
client's registry AV (exactly one of SentinelOne/ESET). Status: **no antivirus** (nothing, or Defender only),
**wrong antivirus** (the other vendor), **stale** (no Asio contact for 30+ days — not flagged).

## Steps
1. `run_recipe("av-gaps")` (or `{client: "<CODE>"}`) — show totals and per-client machines.
2. **Flag for review** (puts machines in the Asio dynamic group via the custom field "AV Status"):
   `propose_change("av_flag_for_review", {"client_codes": [...]})` — omit codes for every client.
3. **Redeploy one client:** `propose_change("av_remediate", {"client_code": "<CODE>"})`.
   - SentinelOne: Asio script **"sentinelone installer"** with the client's site token + the hosted installer link.
   - ESET: Asio script **"eset web installer"** with that company's newest ESET Connect Live Installer.
   - If it refuses, read the reason (missing link, no site token, no installer, script not in Asio) and fix that.
4. **Verify:** the next sweep checks each machine — AV found → flag cleared, status "fixed"; still missing after
   2 sweeps → "not fixed" on the change. Offline machines install when they come online (if Asio queues it).

## Machines with antivirus but no Asio agent
`run_recipe("missing-asio-agent")` — device + company; CSV at `/api/av/missing-asio?format=csv`. Install the Asio
agent from the Remediation tab or ScreenConnect.

## One-time setup (Approver)
- Asio: custom field **AV Status** (endpoint level, text) and a **dynamic device group "No Antivirus – Need
  Immediate Review"** where AV Status = "No Antivirus - Needs Review".
- Asio script **"sentinelone installer"** from `deploy/asio-scripts/sentinelone-installer.ps1` (parameters SITETOKEN,
  DOWNLOADURL). `.env`: `MSP_S1_INSTALLER_URL` = direct link to the SentinelOne Windows installer.
