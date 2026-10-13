---
name: msp-naming-standards
description: The company client client naming standard and device naming standard — standard names, client codes, site names, hostnames, and how each tool should be named. Use for "what should this client be called", "is this name compliant", "naming report", "what hostname should this new laptop get", onboarding a client or device.
---

# Naming standards

## Client names
- **PSA is the system of record.** Create the client in PSA first; every tool copies the PSA values.
- **Standard Name**: ≤ 30 characters (Asio truncates), letters/digits/spaces/hyphen only, `&` → `and`,
  no commas/periods/apostrophes, no LLC/Inc/LLP/PC. Real acronyms stay upper case (YMCA, CPA, DDS).
- **Client Code**: 2–4 upper-case letters, unique, stored in PSA *Company ID*. Used in hostnames.
- **Site**: `Main` for single-location; city/location name otherwise. Never repeat the company name.

| Tool | Company field | Site field |
|---|---|---|
| PSA | Company Name = Standard Name, Company ID = Code | Site |
| Asio | Company = Standard Name | Site |
| ScreenConnect | CustomProperty1 = Standard Name | CustomProperty2 = Site only; CP5 = Asio Site ID |
| SentinelOne | Site = Standard Name | Group = Site |
| ESET | Company = Standard Name | Group = Site |

## Device names
Hostname `<CODE><TYPE><N>-<MMDDYY>`, max **15** characters, e.g. `ARVLAP1-100625`.
TYPE: `DT` desktop, `LAP` laptop, `NUC` mini PC, `AIO`, `SRV` server, `VM`, `HV` hypervisor.
Servers may use `<CODE>SRV-<ROLE>` (e.g. `ARVSRV-DC01`). Never leave `DESKTOP-XXXXXXX`.
To suggest a hostname: `get_client` for the code, today's date, next free number (check `find_device`).

## Checking
- `naming_report` — clients whose name differs in any tool, and standard names that break the rules.
- `get_client` — compliance issues include `name-drift` and `standard-name:` problems.
Renames and approvals are made by admins in the dashboard / client registry.
