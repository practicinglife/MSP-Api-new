---
name: msp-health-and-sync
description: How fresh the MSP data is, when devices and company lists were last refreshed, and how to refresh the client registry. Use for "is the data current?", "when was the last scan?", "refresh client list", "why doesn't X show up yet?".
---

# Data freshness and refresh

Load `msp-api` first.

`data_freshness` returns:
- **Company lists** (client registry) — last `refresh_client_registry`. Cheap; ~5 seconds.
- **Devices** — last reconciliation. Expensive; freezes the server 15–25 minutes; admins only.
- Recent run history and errors.

Guidance:
- Client just onboarded / renamed in a tool → `refresh_client_registry`, then look again.
- New device not showing → it appears after the next reconciliation; tell the user the device data age.
- Device data older than ~24 h → mention it in answers about devices.
- Server not responding → most likely a reconciliation is running; try again in 15–25 minutes.
