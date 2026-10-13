# Copilot Instructions

## Project Guidelines
- User wants a user-friendly visual interface to view companies, company names, matching status, complete end-to-end logging visibility, per-API integration connectivity tests, and normalized data views.
- Companies use ONE AV: either SentinelOne OR ESET, never both. Badge on breakdown page should be company-level (S1 or ESET). Endpoint Protection column should show the version like the CSV export (e.g., "ESET 12.1.2076.1" or "SentinelOne 25.1.4.434"). No combo badges.

## API Integration
- ConnectWise PSA PATCH API uses `application/json` content type (NOT `application/json-patch+json`) with an array of `{"op","path","value"}` operations. Use the existing `cwm_gateway.api_request("PATCH", ...)` which handles this correctly.
- SentinelOne agent lookup uses `params={"ids": agent_id}` (numeric ID), not `params={"uuid": ...}`.
- The `platform_id` field was added to both `_merge_device_data` (reconciler.py) and `_merge_best` (device_identity.py) to expose per-platform IDs (S1 agent ID, PSA config ID) to the UI.
- Never call a live server or real vendor APIs to validate changes. Test locally: `pytest` (vendor calls are mocked with respx; `tests/conftest.py` blocks `.env` and outbound network) or a local Docker instance with no real credentials.

## Device Management
- After cleanup actions (mark inactive / remove agent), devices are removed from local `allBreakdown` JS array via `_removeDeviceFromBreakdown()` instead of doing a full server reload.
- Duplicate `showResult` function was renamed to `showBreakdownResult` to avoid overwriting the ticket version.

## ESET Installation Guidance
- Follow the 5-step sequence for ESET test installation: 
  - Obtain OAuth token
  - Perform endpoint lookup
  - Look up script/template
  - Schedule task
  - Track execution endpoints
- Breakdown ESET install must use ESET Connect Live Installer URL and execute only the ASIO script named 'eset web installer'; do not use 'asio webinstaller' or ASIO agent installer flow.

## Conversation Management
- Preserve all conversation context across sessions/reboots.
- Use detailed conversation summaries with full technical inventory, code archaeology, progress tracking, and continuation plans so work can resume seamlessly after any interruption.

## Development Environment
- Local development: `docker compose up` serves the app on `https://localhost:8000` with live-mounted code (`./app:/app/app`).
- Docker volume mount `./app:/app/app` added to docker-compose.yml so code changes are live without rebuild.