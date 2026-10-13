---
name: msp-onboarding
description: Onboard a brand-new MSP client — check for duplicates, pick the standard name and client code, and propose one change that creates the PSA company, main contact, managed-services agreement with additions, the registry entry, the SentinelOne or ESET account, and the Asio company; finish partial runs with onboard_finish. Use for "new client", "onboard <company>", "set up a new customer", "add a company".
---

# Onboarding a new client

Load `msp-api` and `msp-changes` first. The fastest way for people is the form at
`/onboard` on the MSP Tools server (office/VPN). In chat, do the same steps:

1. **Check first:** `check_new_client(legal_name)`. If `likely_duplicate` is true, show the matches and stop
   until the user confirms it's a different company (then pass `allow_similar: true`).
2. **Collect everything** (ask for anything missing, never invent):
   - Legal name (PSA name, invoices) · standard name (suggested, ≤30, no punctuation) · client code (suggested,
     2–4 letters, = PSA Company ID)
   - Street, suite, city, state, ZIP, main phone
   - Main contact: first, last, email, direct phone — **goes to PSA only** (ticketing)
   - Managed workstations, servers, network (yes/no), ScreenConnect clients
   - Antivirus: SentinelOne **or** ESET (exactly one — the form has a checkbox for each), seats (default
     workstations + servers)
   - Prices: catalog by default, any line can be changed (`prices: {workstations: 22.5, eset: 3.5}`); agreement
     start (default first of next month)
3. **Read it back** in a short table and get a yes.
4. `propose_change("onboard_client", {...})` and hand over the approval link (an Editor submits, an Approver approves).

What it creates: PSA company (name = legal name, Company ID = code, Main site), main contact as default contact,
agreement `<CODE> - <MSP_PSA_AGREEMENT_LABEL>` (type MSP_PSA_AGREEMENT_TYPE_ID) with an addition per line, the registry client, a SentinelOne
site (Control bundle, licenses = seats) or ESET company — with **the MSP alert mailbox (MSP_ALERT_EMAIL)** as the alert
contact, never the customer — and last the **Asio company** (standard name, Main site, the time zone in MSP_DEFAULT_TIMEZONE; if Asio
rejects the phone number it is created without it).

## After it runs
- Check with `run_recipe("onboarding-status", {client: "<CODE>"})`.
- Anything refused (AV or Asio) → fix the cause, then `propose_change("onboard_finish", {client_code})` — it
  creates only what's still missing.
- **Map the Asio company to the PSA company in the ConnectWise integration** (the API can't set the mapping).
- ScreenConnect installer: CP1 = standard name, CP2 = Main, CP5 = Asio site id.
- Asio errors: "client has no access" = API client permission (needs scope `platform.sites.write`).
