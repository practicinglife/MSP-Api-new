# MSP pack — overview

> **This describes the MSP pack** (`MSP_PACKS=msp`): the ConnectWise PSA / Asio / ScreenConnect / SentinelOne / ESET
> integration, its local database and its dashboard. For the app as a whole see the [README](../README.md).


A **Python 3.12 / FastAPI** application that acts as a unified integration hub for a Managed Service Provider (MSP). It connects to **7 different MSP platforms** (plus a generic REST adapter), pulls company and device data from each, and **reconciles** (matches) records across platforms so operators can see a single consolidated view of all managed companies and devices.

---

## The Problem It Solves

An MSP manages dozens of client companies, each appearing separately in ConnectWise PSA, SentinelOne, ESET, ScreenConnect, and ConnectWise Asio — often with slightly different names, different device inventories, and no built-in cross-referencing. This tool bridges that gap by:

- Normalizing company and device names across all platforms
- Fuzzy-matching companies that use different naming conventions
- Cross-referencing devices by endpoint ID, hostname, and MAC address
- Surfacing gaps — devices missing from a platform that should have coverage
- Enabling remediation — installing/uninstalling agents remotely
- Auto-discovering and cataloguing platform API endpoints from OpenAPI specs
- Managing ConnectWise Manage tickets with automatic triage and classification

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                          Docker Compose                              │
│                                                                      │
│  ┌───────────────────┐      ┌──────────────────────────────────────┐ │
│  │  PostgreSQL 16     │◄────│  FastAPI App (uvicorn :8000)         │ │
│  │  (msp_tools DB)    │     │                                      │ │
│  └───────────────────┘      │  ┌── Connectors ──────────────────┐  │ │
│                              │  │ • ConnectWise PSA  (Manage)    │  │ │
│                              │  │ • ConnectWise Asio (RMM)       │  │ │
│                              │  │ • SentinelOne      (EDR)       │  │ │
│                              │  │ • ESET MSP Portal  (AV)        │  │ │
│                              │  │ • ESET Connect     (AV detail) │  │ │
│                              │  │ • ScreenConnect    (Remote)    │  │ │
│                              │  │ • Avast Business Hub (opt.)    │  │ │
│                              │  │ • Generic REST     (any API)   │  │ │
│                              │  └────────────────────────────────┘  │ │
│                              │                                      │ │
│                              │  ┌── Services ────────────────────┐  │ │
│                              │  │ • Company Matcher  (RapidFuzz) │  │ │
│                              │  │ • Device Identity Resolution   │  │ │
│                              │  │ • Reconciliation Engine        │  │ │
│                              │  │ • Gap Analysis                 │  │ │
│                              │  │ • Ticket Triage (rule-based)   │  │ │
│                              │  │ • Remediation Orchestrator     │  │ │
│                              │  │ • CWM Gateway (ticket CRUD)   │  │ │
│                              │  │ • OpenAPI Spec Parser          │  │ │
│                              │  │ • Connector Registry           │  │ │
│                              │  └────────────────────────────────┘  │ │
│                              │                                      │ │
│                              │  ┌── Frontend ────────────────────┐  │ │
│                              │  │ HTML/JS Operator Dashboard     │  │ │
│                              │  │ (app/static/index.html)        │  │ │
│                              │  └────────────────────────────────┘  │ │
│                              └──────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Platform Connectors

Each connector lives in `app/connectors/` and subclasses `AbstractPlatformConnector`. Every connector implements:

| Method             | Purpose                                              |
| :----------------- | :--------------------------------------------------- |
| `check_config()`   | Validates API keys/secrets are present in `.env`     |
| `authenticate()`   | Obtains OAuth tokens or API sessions                 |
| `get_companies()`  | Returns normalized `CompanyRecord` list              |
| `get_devices()`    | Returns normalized `DeviceRecord` list               |

Optional capability flags (`supports_scan`, `supports_alerts`, `supports_install`, `supports_tickets`, etc.) advertise what each connector can do beyond basic data fetching.

### Connector Summary

| Connector              | Platform                     | Auth                            | Companies | Devices | Notes                                                    |
| :--------------------- | :--------------------------- | :------------------------------ | :-------- | :------ | :------------------------------------------------------- |
| `connectwise_psa.py`   | ConnectWise Manage (PSA)     | API key + client ID             | ~76       | ~1,156  | **Source of truth** for AV assignment                    |
| `asio.py`              | ConnectWise Asio (RMM)       | OAuth 2.0 `client_credentials`  | ~74       | ~963    | 10 req/10 min rate limit; TTL cached with `asyncio.Lock` |
| `sentinelone.py`       | SentinelOne (EDR)            | API token                       | ~48       | ~490    | Uses `siteName` as company                               |
| `screenconnect.py`     | ScreenConnect / CW Control   | RESTful auth token              | ~71       | ~981    | Remote execution layer for remediation                   |
| `eset.py`              | ESET MSP Portal (EMA2)       | Username / password             | ~22       | —       | Company-level data only; no device endpoint in EMA2      |
| `eset_connect.py`      | ESET Connect (Protect Cloud) | OAuth 2.0 password grant        | —         | per-device | Per-device AV product versions and deployed components |
| `avast.py`             | Avast Business Hub           | OAuth 2.0                       | Optional  | Optional | —                                                        |
| `generic_rest.py`      | Any REST API                 | API key / Basic / Bearer / None | Configurable | Configurable | Connects to arbitrary REST endpoints with field mapping |

### Startup Behavior

At startup (`main.py`), only connectors with valid credentials are activated. Additional connectors can be registered at runtime via the `/api/connectors` API and persisted in PostgreSQL. The `connector_registry.py` service maps platform names to connector classes and handles credential injection.

---

## The Matching Pipeline

The heart of the system is **reconciliation** — matching the same company and device across different platforms that use different names and IDs.

### Company Matching (3 layers)

```
Raw Name → Normalization → Fuzzy Match → Device Hostname Overlap → Manual Override
```

1. **Normalization** (`app/services/normalizers.py`)
   - Strips legal suffixes (LLC, Inc, Corp, Ltd, LLP, LP, Co)
   - Converts `&` → `and`
   - Removes stop words (`the`, `of`, `and`, `an`)
   - Strips trailing US state abbreviations (e.g., `mn`, `tx`)
   - Removes all punctuation
   - Lowercases and collapses whitespace

2. **Fuzzy Matching** (`app/services/company_matcher.py`)
   - Uses `rapidfuzz.fuzz.token_sort_ratio`
   - **≥ 90%** → auto-linked (no human review needed)
   - **≥ 85%** → needs review (flagged for operator)
   - **< 85%** → unmatched
   - Platform priority order: `connectwise_psa` → `asio` → `sentinelone` → `screenconnect` → `eset`

3. **Device Hostname Overlap**
   - If two companies on different platforms share multiple devices with the same hostname, they are the same company regardless of name differences

### Alias Merge Order

```python
merged = {**fuzzy_aliases, **device_hostname_aliases, **manual_DB_aliases}
```

Manual DB overrides take highest priority, then device hostname matches, then fuzzy matches.

### Device Matching (4 tiers)

| Tier   | Method               | Description                                                            |
| :----- | :------------------- | :--------------------------------------------------------------------- |
| **0**  | Endpoint ID          | Asio `endpoint_id` is the golden key (same UUID stored in PSA custom field) |
| **1**  | Skeleton Hostname    | Normalized hostname within the same canonical company                  |
| **2**  | 15-char NetBIOS Prefix | Catches ScreenConnect's truncated hostnames                          |
| **3**  | MAC Address Fallback | Last resort for machines with different hostnames across platforms      |

### Device Match Scoring

| Signal         | Points | Notes                                |
| :------------- | -----: | :----------------------------------- |
| Serial exact   |    100 | Excludes placeholder serials         |
| MAC overlap    |     60 | Any common MAC address               |
| Company + host |     50 | Same canonical company + hostname    |
| Hostname exact |     30 | Hostname match across any company    |

Auto-link threshold: **≥ 90** · Review threshold: **≥ 30**

---

## API Endpoints

All routes are defined in `app/api/` and served under `/api/`. FastAPI auto-generates interactive docs at `/docs` (Swagger) and `/redoc` (ReDoc).

> **81 endpoints** across 12 tag groups.

### Companies & Reconciliation

| Method | Path | Description |
| :----- | :--- | :---------- |
| `GET` | `/api/companies` | List all stored company match results |
| `GET` | `/api/companies/summary` | Per-company device counts with platform badges, AV status, and match status |
| `GET` | `/api/companies/by-platform` | Every company name reported by each platform with cross-platform match info |
| `GET` | `/api/companies/breakdown` | Hierarchical Company → Platform → Site → Device tree |
| `GET` | `/api/companies/reconciled` | Cross-platform device reconciliation view (`?force=true` to bypass 15-min cache) |
| `GET` | `/api/companies/reconciled/export` | Download full reconciled dataset as CSV |
| `GET` | `/api/companies/reconciled/export/{company_name}` | Download single-company CSV with summary header |
| `POST` | `/api/companies/scan` | Pull all connectors, persist devices + company mappings to DB, detect changes |
| `GET` | `/api/companies/av-map` | Normalised company name → assigned AV product from PSA agreement additions |
| `GET` | `/api/companies/aliases` | List all company name aliases (merge rules) |
| `POST` | `/api/companies/merge` | Create an alias so *source_name* merges into *target_name* |
| `DELETE` | `/api/companies/aliases/{alias_id}` | Delete a company alias by ID |
| `GET` | `/api/companies/{match_id}` | Get a single company match by ID |

### Devices & AV Detection

| Method | Path | Description |
| :----- | :--- | :---------- |
| `GET` | `/api/devices` | List all stored device match results |
| `GET` | `/api/devices/unmatched` | Devices present on one platform but missing from others |
| `GET` | `/api/devices/av-status` | Per-device AV status from all detection sources (`?company=` filter) |
| `POST` | `/api/devices/av-detect` | Trigger AV detection on endpoints via ScreenConnect PowerShell script |
| `POST` | `/api/devices/av-report` | Receive AV detection results posted back by managed endpoints |
| `POST` | `/api/devices/av-import` | Bulk import AV detection results from any source |
| `POST` | `/api/devices/av-scan-asio` | Scan ASIO endpoints for installed AV products via the applications API |
| `POST` | `/api/devices/av-scan-asio-groups` | Detect ESET endpoints via ASIO device groups (requires `platform.deviceGroups.read` scope) |
| `POST` | `/api/devices/av-scan-eset-connect` | Fetch per-device ESET product versions from ESET Connect (Protect Cloud) |
| `GET` | `/api/devices/{match_id}` | Get a single device match by ID |

### Connectors

| Method | Path | Description |
| :----- | :--- | :---------- |
| `GET` | `/api/connectors` | List registered connectors with config status and health |
| `GET` | `/api/connectors/platforms` | List all available platform definitions with their credential fields |
| `GET` | `/api/connectors/configs` | List all saved connector configurations (secrets redacted) |
| `POST` | `/api/connectors/register` | Register a new connector — save credentials, optionally test, and activate |
| `PUT` | `/api/connectors/configs/{platform}` | Update credentials for an existing connector |
| `DELETE` | `/api/connectors/configs/{platform}` | Remove a saved connector configuration and deactivate it |
| `POST` | `/api/connectors/test` | Run connectivity tests against all registered connectors |
| `POST` | `/api/connectors/test/{platform_name}` | Run a connectivity test against a single connector |

### Coverage (Gap Analysis)

| Method | Path | Description |
| :----- | :--- | :---------- |
| `GET` | `/api/coverage` | Full coverage matrix — device counts per company per platform |
| `GET` | `/api/coverage/missing` | Companies missing devices on at least one platform |
| `GET` | `/api/coverage/summary` | High-level cross-platform coverage summary |

### Actions (Cross-Platform)

| Method | Path | Description |
| :----- | :--- | :---------- |
| `GET` | `/api/actions/capabilities` | List each connector's supported action capabilities |
| `POST` | `/api/actions/scan` | Trigger an AV/EDR scan on a platform's devices |
| `GET` | `/api/actions/alerts` | Aggregate security alerts from all platforms (`?limit=`) |
| `GET` | `/api/actions/alerts/{platform}` | Security alerts from a specific platform (`?limit=`, `?severity=`) |
| `GET` | `/api/actions/tickets` | Aggregate service tickets from all PSA platforms (`?limit=`) |
| `GET` | `/api/actions/tickets/{platform}` | Service tickets from a specific platform (`?company_id=`, `?limit=`) |
| `POST` | `/api/actions/command` | Run a remote command on specified platform devices |

### Tickets (ConnectWise Manage)

| Method | Path | Description |
| :----- | :--- | :---------- |
| `GET` | `/api/tickets` | List service tickets (`?conditions=`, `?limit=`, `?order_by=`) |
| `POST` | `/api/tickets` | Create a new ticket (with optional auto-triage) |
| `GET` | `/api/tickets/{ticket_id}` | Get a single ticket by ID |
| `PATCH` | `/api/tickets/{ticket_id}` | Update ticket fields (status, priority, owner, etc.) |
| `POST` | `/api/tickets/{ticket_id}/notes` | Add a note to a ticket |
| `GET` | `/api/tickets/{ticket_id}/notes` | List all notes for a ticket |
| `GET` | `/api/tickets/boards` | List available service boards |
| `GET` | `/api/tickets/boards/{board_id}/statuses` | List statuses for a given service board |
| `GET` | `/api/tickets/members` | List active CW Manage members (technicians) |
| `GET` | `/api/tickets/priorities` | List available ticket priorities |
| `POST` | `/api/tickets/triage` | Classify and assess urgency of ticket text without creating a ticket |
| `GET` | `/api/tickets/api-search` | Search the ConnectWise Manage API endpoint database (`?q=`) |
| `GET` | `/api/tickets/api-categories` | List all CW Manage API categories |
| `POST` | `/api/tickets/api-execute` | Execute an arbitrary CW Manage API call (pass-through) |

### Remediation

| Method | Path | Description |
| :----- | :--- | :---------- |
| `GET` | `/api/remediation/capabilities` | Remediation capabilities for every registered connector |
| `POST` | `/api/remediation/install` | Install an agent (SentinelOne, ESET, Asio, ScreenConnect) on devices |
| `POST` | `/api/remediation/uninstall` | Uninstall an agent from devices |
| `POST` | `/api/remediation/repair` | Repair / reinstall an agent (ScreenConnect routes via Asio automation script) |
| `POST` | `/api/remediation/offboard` | Offboard devices from platforms (Asio uninstall + optional ESET removal, max 5) |
| `POST` | `/api/remediation/create-company` | Create a company uniformly across platforms |
| `GET` | `/api/remediation/scripts/{platform}` | List available automation scripts for a platform (e.g. Asio) |
| `POST` | `/api/remediation/scripts/run` | Execute an automation script on target devices |
| `GET` | `/api/remediation/asio/sites/{company_id}` | Return all sites for an ASIO company |
| `GET` | `/api/remediation/asio/installer/{company_id}` | Return ASIO agent installer details (URL, token, filename) |

### API Discovery

| Method | Path | Description |
| :----- | :--- | :---------- |
| `POST` | `/api/discovery/scan` | Trigger OpenAPI spec discovery for all registered connectors |
| `GET` | `/api/discovery/specs` | List all stored API specs (without the full JSON blob) |
| `GET` | `/api/discovery/specs/{platform}/endpoints` | List all endpoints for a specific platform |
| `GET` | `/api/discovery/search` | Search endpoints across all platforms by keyword (`?q=`) |
| `POST` | `/api/discovery/execute` | Execute an API call on a connector platform (with optional TTL caching) |
| `GET` | `/api/discovery/cache` | List all cached API responses (`?platform=` filter) |
| `DELETE` | `/api/discovery/cache/expired` | Remove all expired cache entries |

### Runs

| Method | Path | Description |
| :----- | :--- | :---------- |
| `GET` | `/api/runs` | List recent sync runs with summary counts |
| `GET` | `/api/runs/{run_id}` | Get a single run with its connector events |

### Admin Overrides

| Method | Path | Description |
| :----- | :--- | :---------- |
| `GET` | `/admin/overrides/companies` | List all company overrides |
| `POST` | `/admin/overrides/companies` | Create a manual company link override |
| `GET` | `/admin/overrides/devices` | List all device overrides |
| `POST` | `/admin/overrides/devices` | Create a manual device link override |

### System

| Method | Path | Description |
| :----- | :--- | :---------- |
| `GET` | `/` | Serve the operator dashboard |
| `GET` | `/health` | Health check |
| `POST` | `/api/reconcile` | Trigger a full reconciliation run across all connectors |

---

## Database

**PostgreSQL 16** with async SQLAlchemy 2.0 + asyncpg. Schema defined in `app/db/schema.py`.

| Table                | Purpose                                                      |
| :------------------- | :----------------------------------------------------------- |
| `sync_runs`          | Audit log of each reconciliation run (timing, counts, errors, warnings) |
| `connector_events`   | Per-connector timing/error events per run                    |
| `company_matches`    | Persisted company match results with scores and status       |
| `device_matches`     | Persisted device match results with reasons                  |
| `company_overrides`  | Manual company-to-company link overrides                     |
| `device_overrides`   | Manual device-to-device link overrides                       |
| `company_aliases`    | Saved name aliases (e.g., `"acme"` → `"acme corp"`)         |
| `company_mappings`   | Resolved `(platform, raw_name)` → `canonical_name` lookup   |
| `platform_devices`   | Cached device records per platform (bulk-upserted on scan)   |
| `reconciled_devices` | Final merged device view after cross-platform matching       |
| `connector_configs`  | Persisted connector credentials (added via UI/API)           |
| `device_av_status`   | Per-device AV detection results from multiple sources        |
| `api_specs`          | Stored OpenAPI specs for platform APIs                       |
| `api_endpoints`      | Individual endpoints parsed from OpenAPI specs               |
| `api_response_cache` | Cached API responses with TTL for static data                |

---

## Frontend Dashboard

A single-page HTML/JS dashboard served at `http://localhost:8000/` providing:

- **Company summary table** — all companies with platform badges, device counts, match status
- **Per-company breakdown** — drill into any company to see every device across all platforms, matched/missing status
- **Endpoint Protection column** — AV product + version (e.g., `"ESET 12.1.2076.1"` or `"SentinelOne 25.1.4.434"`)
- **Company-level AV badge** — S1 or ESET badge per company (never both — each company uses one AV)
- **PSA presence indicator** — ⚠️ yellow badge for companies that exist in RMM/EDR but not in PSA
- **Connector health panel** — live connectivity test for each configured platform
- **Ticket management** — create, view, triage, and manage ConnectWise Manage tickets
- **Remediation UI** — install/uninstall/repair agents across platforms
- **CSV export** — download company and device data

---

## Key Files

```
MSP-Api-Tools/
├── app/
│   ├── main.py                          # FastAPI entry point, lifespan, router registration
│   ├── config.py                        # Settings (Pydantic BaseSettings from .env)
│   ├── api/
│   │   ├── companies.py                 # Core reconciliation endpoints
│   │   ├── devices.py                   # Device search/detail
│   │   ├── connector_health.py          # Connector CRUD + health tests
│   │   ├── tickets.py                   # CW Manage ticket management (14 routes)
│   │   ├── remediation.py               # Install/uninstall/repair agents
│   │   ├── gap_analysis.py              # Coverage matrix endpoints
│   │   ├── overrides.py                 # Manual match overrides
│   │   ├── runs.py                      # Reconciliation run history
│   │   ├── actions.py                   # Cross-platform scans, alerts, commands
│   │   └── api_discovery.py             # OpenAPI spec fetch/parse/search
│   ├── connectors/
│   │   ├── base.py                      # AbstractPlatformConnector (interface)
│   │   ├── asio.py                      # ConnectWise Asio (RMM) — TTL cached
│   │   ├── connectwise_psa.py           # ConnectWise Manage (PSA)
│   │   ├── sentinelone.py               # SentinelOne (EDR)
│   │   ├── screenconnect.py             # ScreenConnect / CW Control
│   │   ├── eset.py                      # ESET MSP Portal (EMA2)
│   │   ├── eset_connect.py              # ESET Connect (Protect Cloud — per-device AV)
│   │   ├── avast.py                     # Avast Business Hub
│   │   └── generic_rest.py              # Generic REST API (any endpoint)
│   ├── services/
│   │   ├── normalizers.py               # Name/hostname/MAC/serial normalization
│   │   ├── company_matcher.py           # RapidFuzz fuzzy matching (thresholds 90/85)
│   │   ├── device_matcher.py            # Tiered device scoring (serial/MAC/hostname)
│   │   ├── device_identity.py           # Endpoint-ID-first resolution + DB persistence
│   │   ├── reconciliation.py            # Full reconciliation orchestrator
│   │   ├── reconciler.py                # Device reconciliation logic
│   │   ├── gap_analysis.py              # Coverage matrix builder
│   │   ├── remediation.py               # Remediation orchestration service
│   │   ├── cwm_gateway.py               # ConnectWise Manage API gateway (tickets)
│   │   ├── ticket_triage.py             # Rule-based ticket classification engine
│   │   ├── connector_registry.py        # Dynamic connector builder from DB configs
│   │   └── openapi_parser.py            # OpenAPI/Swagger spec fetcher and parser
│   ├── models/
│   │   ├── company.py                   # CompanyRecord (Pydantic)
│   │   ├── device.py                    # DeviceRecord (Pydantic)
│   │   └── actions.py                   # ScanRequest, AlertRecord, CoverageRow models
│   ├── db/
│   │   ├── schema.py                    # SQLAlchemy ORM table definitions (15 tables)
│   │   └── repository.py               # Data access layer (all repositories)
│   ├── specs/                           # Local OpenAPI spec files (shipped in image)
│   │   ├── asio.json
│   │   ├── connectwise_psa.json
│   │   ├── eset.json
│   │   ├── eset_connect_auth.json
│   │   ├── eset_connect_devices.json
│   │   ├── screenconnect.json
│   │   └── sentinelone.json
│   └── static/
│       └── index.html                   # Operator dashboard (HTML/JS SPA)
├── docker-compose.yml                   # app + postgres containers
├── Dockerfile                           # Python 3.12-slim, uvicorn
├── requirements.txt                     # 13 dependencies
├── .env                                 # Platform API credentials (not committed)
└── docs/
    └── PROJECT-OVERVIEW.md              # This file
```

---

## How It's Used (Typical Workflow)

### 1. Configure

Set API credentials in `.env` for each platform:

```env
# ConnectWise PSA (Manage)
CW_COMPANY_ID=yourcompany
CW_PUBLIC_KEY=abc123
CW_PRIVATE_KEY=xyz789
CW_CLIENT_ID=your-guid
CW_BASE_URL=https://na.myconnectwise.net/v4_6_release/apis/3.0

# SentinelOne
S1_BASE_URL=https://usea1-partners.sentinelone.net
S1_API_TOKEN=your-token

# ConnectWise Asio
ASIO_CLIENT_ID=your-client-id
ASIO_CLIENT_SECRET=your-secret
ASIO_REGION=NA

# ScreenConnect / CW Control
SCREENCONNECT_BASE_URL=https://your-instance.screenconnect.com
SCREENCONNECT_AUTH_TOKEN=your-restful-auth-secret

# ESET MSP Portal
ESET_USERNAME=your-user
ESET_PASSWORD=your-pass

# ESET Connect (optional — per-device AV details)
ESET_CONNECT_USERNAME=your-user
ESET_CONNECT_PASSWORD=your-pass

# PostgreSQL (override for non-Docker usage)
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/msp_tools
```

### 2. Start

```bash
docker compose up --build
```

Both containers (FastAPI app + PostgreSQL) start. The app auto-discovers which connectors have valid credentials and activates them. Connectors missing credentials are silently skipped.

### 3. Open Dashboard

Navigate to `http://localhost:8000` — the operator dashboard loads.

### 4. Scan

Click **"Scan"** (or call `POST /api/companies/scan`) to pull live data from all platforms concurrently. The system:

1. Authenticates with each platform
2. Fetches all companies and devices in parallel (`asyncio.gather`)
3. Normalizes names, hostnames, serials, MACs
4. Runs the 3-layer company matching pipeline
5. Runs the 4-tier device matching pipeline
6. Persists results to PostgreSQL
7. Returns the consolidated view

### 5. Review

The dashboard shows every company with:

- ✅ **Matched** across platforms
- ⚠️ **Needs review** (fuzzy match, confidence 85–90%)
- ❌ **Unmatched** / missing on some platforms
- ⚠️ **No PSA** — company exists in RMM/EDR but not in PSA

### 6. Drill Down

Click a company to see the per-device breakdown:

- Every device across all platforms
- Which devices matched (same physical machine)
- Which are missing from specific platforms
- AV status and version per device
- Endpoint ID, hostname, serial, MAC addresses

### 7. Fix Gaps

Use the **Remediation API** to:

- Install SentinelOne on devices that only have Asio
- Deploy ESET via Asio automation scripts
- Install the Asio agent via ScreenConnect on new machines
- Uninstall or repair agents remotely

### 8. Manage Tickets

Create CW Manage tickets for issues found during gap analysis. The triage engine classifies ticket urgency automatically using keyword-based rules across categories: network, server, workstation, email, printer, and security.

### 9. Override

Manually link companies/devices that the fuzzy matcher couldn't resolve via the Overrides API or DB aliases.

### 10. Export

Download CSV reports for compliance reviews or client meetings.

---

## Dependencies

| Package            | Version   | Purpose                                     |
| :----------------- | :-------- | :------------------------------------------ |
| `fastapi`          | ≥ 0.115  | Web framework                               |
| `uvicorn`          | ≥ 0.30   | ASGI server                                 |
| `httpx`            | ≥ 0.27   | Async HTTP client for all connector calls   |
| `pydantic`         | ≥ 2.10   | Data validation and settings                |
| `pydantic-settings` | ≥ 2.3   | `.env` file loading                         |
| `sqlalchemy`       | ≥ 2.0.30 | Async ORM (with asyncio extension)          |
| `asyncpg`          | ≥ 0.29   | PostgreSQL async driver                     |
| `alembic`          | ≥ 1.13   | Database migrations                         |
| `rapidfuzz`        | ≥ 3.9    | Fuzzy string matching for company names     |
| `python-dotenv`    | ≥ 1.0    | Environment variable loading                |
| `tenacity`         | ≥ 8.3    | Retry logic (Asio 429 rate limit handling)  |
| `structlog`        | ≥ 24.1   | Structured logging                          |
| `pyyaml`           | ≥ 6.0    | OpenAPI spec YAML parsing                   |

---

## Key Design Decisions

| Decision                          | Rationale                                                                                       |
| :-------------------------------- | :---------------------------------------------------------------------------------------------- |
| **Aggressive Asio caching**       | 10 req/10 min rate limit requires 5-minute TTL caches with `asyncio.Lock` and double-checked locking |
| **Connector plugin architecture** | `AbstractPlatformConnector` base class makes adding new platforms a matter of implementing 4 methods |
| **Generic REST connector**        | Any REST API can be integrated without writing code — configure endpoints and field mappings via UI |
| **Normalization before matching** | Company names, hostnames, MACs, and serials are all normalized before any comparison to maximize match rates |
| **PSA as source of truth**        | ConnectWise PSA company assignments determine the canonical company-to-AV mapping                |
| **ScreenConnect as execution layer** | Remote agent install commands are sent via ScreenConnect sessions, not platform APIs directly  |
| **Endpoint ID as golden key**     | Asio endpoint UUID is the most reliable device identifier — deterministic, no fuzzy logic needed |
| **Multi-tier device matching**    | Graceful degradation from exact ID match → hostname → NetBIOS prefix → MAC address              |
| **Local OpenAPI specs**           | Specs shipped in `app/specs/` avoid network calls at startup; remote fetch is a fallback         |
| **Rule-based ticket triage**      | Keyword matching across 6 categories avoids LLM dependency while providing instant classification |

---

## Interactive API Docs

FastAPI auto-generates OpenAPI documentation at:

- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`
