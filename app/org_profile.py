"""Organization profile — every value that belongs to one specific company.

Nothing in the code should hardcode a domain, person, office IP or PSA record id; it reads it from here, and
everything here comes from the environment (``.env``). Defaults are neutral so a fresh install for another
organization starts clean. An empty PSA setting turns the feature that depends on it off rather than guessing.
"""

from __future__ import annotations

import contextlib
import json
import os


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int = 0) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _list(name: str) -> list[str]:
    return [p.strip() for p in _env(name).split(",") if p.strip()]


# ── Identity / network ──────────────────────────────────────────────────────────────────────────────
ORG_NAME = _env("MSP_ORG_NAME", "MSP API Tools")
# Public hostname of the nginx edge (e.g. tools.example.com). Empty = no internet edge, LAN only.
EDGE_DOMAIN = _env("MSP_EDGE_DOMAIN").lower()
# Public URL used in OAuth metadata, sign-in redirects and links in change requests.
EXTERNAL_BASE_URL = (
    _env("MSP_EXTERNAL_BASE_URL") or (f"https://{EDGE_DOMAIN}" if EDGE_DOMAIN else "http://localhost:8000")
).rstrip("/")
# Office public IPs that count as "internal" when they arrive through the edge.
OFFICE_IPS = set(_list("MSP_OFFICE_IPS"))
# Mailbox new SentinelOne sites / ESET companies send alerts to.
ALERT_EMAIL = _env("MSP_ALERT_EMAIL")

# ── ConnectWise PSA (MSP pack) ──────────────────────────────────────────────────────────────────────
# Agreement type that marks a company as a managed client (e.g. "Managed Services - Standard").
PSA_MANAGED_AGREEMENT_TYPE = _env("MSP_PSA_MANAGED_AGREEMENT_TYPE")
# Extra agreement types that also make a company managed / carry AV additions.
PSA_AV_BILLING_AGREEMENT_TYPE = _env("MSP_PSA_AV_BILLING_AGREEMENT_TYPE")
PSA_S1_AGREEMENT_TYPE = _env("MSP_PSA_S1_AGREEMENT_TYPE")
# Configuration types that count as managed devices (case-insensitive, comma separated). Empty = every type.
PSA_DEVICE_TYPES = [t.lower() for t in _list("MSP_PSA_DEVICE_TYPES")] if "MSP_PSA_DEVICE_TYPES" in os.environ \
    else ["managed workstation", "managed workstations", "managed server", "managed servers"]
# Onboarding: agreement type id + template agreement copied for new clients (0 = onboarding disabled).
PSA_AGREEMENT_TYPE_ID = _int("MSP_PSA_AGREEMENT_TYPE_ID")
PSA_AGREEMENT_TEMPLATE_ID = _int("MSP_AGREEMENT_TEMPLATE_ID")
# Short label used for new agreements: "<CODE> - <label>".
PSA_AGREEMENT_LABEL = _env("MSP_PSA_AGREEMENT_LABEL", "Managed Services")
# Catalog product prefix for onboarding additions, e.g. "Managed:" → "Managed:Managed Workstation".
PSA_PRODUCT_PREFIX = _env("MSP_PSA_PRODUCT_PREFIX")
_DEFAULT_PRODUCT_LINES = {
    "workstations": "Managed Workstation",
    "servers": "Managed Server",
    "network": "Managed Network",
    "sc_clients": "SC Client",
    "sentinelone": "SentOne-Control",
    "eset": "ESET",
}


def _product_lines() -> dict[str, str]:
    """Onboarding line → PSA catalog identifier. ``MSP_PSA_PRODUCT_LINES`` (JSON) overrides any line."""
    lines = {k: f"{PSA_PRODUCT_PREFIX}{v}" for k, v in _DEFAULT_PRODUCT_LINES.items()}
    raw = _env("MSP_PSA_PRODUCT_LINES")
    if raw:
        with contextlib.suppress(ValueError, AttributeError):
            lines.update({str(k): str(v) for k, v in json.loads(raw).items()})
    return lines


PSA_PRODUCT_LINES = _product_lines()
# Catch-all PSA company that receives unassigned alert tickets (0 = helpdesk triage disabled).
PSA_CATCHALL_COMPANY_ID = _int("MSP_CATCHALL_COMPANY_ID")

# ── Asio (MSP pack) ─────────────────────────────────────────────────────────────────────────────────
# Asio automation script that reinstalls ScreenConnect into the right company context.
SC_REPAIR_SCRIPT = _env("MSP_SC_REPAIR_SCRIPT")


def missing_msp_settings() -> list[str]:
    """MSP-pack settings that are unset — logged at startup so a missing .env line is obvious."""
    checks = {
        "MSP_PSA_MANAGED_AGREEMENT_TYPE": PSA_MANAGED_AGREEMENT_TYPE,
        "MSP_CATCHALL_COMPANY_ID": PSA_CATCHALL_COMPANY_ID,
        "MSP_SC_REPAIR_SCRIPT": SC_REPAIR_SCRIPT,
        "MSP_ALERT_EMAIL": ALERT_EMAIL,
    }
    return [name for name, value in checks.items() if not value]
