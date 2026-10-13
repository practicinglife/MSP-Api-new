"""Report generation service — DB-first PDF/HTML/JSON reports.

Reports are built from the **already-reconciled** in-memory data returned
by :func:`app.api.companies.company_reconciled`. Because that endpoint is
backed by ``platform_devices`` (the local DB cache) it is sub-second
without hitting any platform API. Pass ``max_age_hours`` to control how
stale the underlying data may be before a refresh is suggested.

Currently supported formats:
- ``json`` — machine-readable structured payload
- ``html`` — printable HTML preview (also the source for PDF rendering)
- ``pdf``  — production-ready report rendered with WeasyPrint

The Jinja2 template lives in ``app/templates/company_report.html``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from jinja2 import Environment, FileSystemLoader, select_autoescape

log = structlog.get_logger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_REPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "reports"
_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


# ── Public API ───────────────────────────────────────────────────


def find_company(reconciled: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """Locate a single company in the reconciled dataset by fuzzy name match."""
    needle = name.strip().lower()
    if not needle:
        return None

    # Exact match first
    for entry in reconciled:
        if entry.get("company", "").lower() == needle:
            return entry

    # Substring match
    for entry in reconciled:
        if needle in entry.get("company", "").lower():
            return entry

    return None


def build_company_report_context(company_data: dict[str, Any]) -> dict[str, Any]:
    """Normalise the reconciled company payload for the report template."""
    matched = company_data.get("matched", []) or []
    mismatched_buckets = company_data.get("mismatched", {}) or {}

    # Flatten mismatched into a single list with the platform tag preserved
    mismatched_flat: list[dict[str, Any]] = []
    for plat, devices in mismatched_buckets.items():
        for dev in devices:
            entry = dict(dev)
            entry["only_platform"] = plat
            mismatched_flat.append(entry)

    # Suggested actions per mismatched device
    for entry in mismatched_flat:
        entry["suggested_action"] = _suggest_action(entry)

    return {
        "company_name": company_data.get("company", "unknown"),
        "psa_present": company_data.get("psa_present", False),
        "assigned_av": company_data.get("assigned_av", ""),
        "matched": matched,
        "matched_count": len(matched),
        "mismatched": mismatched_flat,
        "mismatched_count": len(mismatched_flat),
        "platforms_present": sorted({
            p for dev in matched for p in dev.get("platforms", [])
        } | set(mismatched_buckets.keys())),
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    }


def _suggest_action(device: dict[str, Any]) -> dict[str, str]:
    """Return a human-readable suggested action for a mismatched device."""
    plat = device.get("only_platform", "")
    hostname = device.get("hostname", "unknown")

    if plat == "connectwise_psa":
        return {
            "label": "Mark Inactive in PSA",
            "endpoint": "/api/companies/devices/psa/mark-inactive",
            "method": "POST",
            "rationale": f"{hostname} exists only in PSA — likely a stale config.",
            "destructive": "true",
        }
    if plat == "sentinelone":
        return {
            "label": "Decommission S1 Agent",
            "endpoint": "/api/companies/devices/sentinelone/remove",
            "method": "POST",
            "rationale": f"{hostname} has an S1 agent but no PSA/Asio record — likely orphaned.",
            "destructive": "true",
        }
    if plat == "asio":
        return {
            "label": "Investigate Asio-only device",
            "endpoint": "",
            "method": "",
            "rationale": f"{hostname} is in Asio but missing from PSA — verify config exists.",
            "destructive": "false",
        }
    if plat == "screenconnect":
        return {
            "label": "Verify ScreenConnect session",
            "endpoint": "",
            "method": "",
            "rationale": f"{hostname} only has SC session — check if it's a transient remote-support box.",
            "destructive": "false",
        }
    return {
        "label": "Review",
        "endpoint": "",
        "method": "",
        "rationale": f"{hostname} appears on a single platform.",
        "destructive": "false",
    }


def render_html(context: dict[str, Any]) -> str:
    """Render the company report template to HTML."""
    template = _jinja_env.get_template("company_report.html")
    return template.render(**context)


def render_pdf(html: str, output_path: Path | None = None) -> bytes:
    """Render HTML to PDF using WeasyPrint. Returns PDF bytes."""
    # Imported lazily so the rest of the module works even if WeasyPrint
    # cannot be loaded (e.g. missing system libs in dev environments).
    from weasyprint import HTML  # type: ignore[import-untyped]

    pdf_bytes: bytes = HTML(string=html).write_pdf()
    if output_path is not None:
        output_path.write_bytes(pdf_bytes)
        log.info("report.pdf.saved", path=str(output_path), bytes=len(pdf_bytes))
    return pdf_bytes


def report_filename(company_name: str, ext: str) -> str:
    """Build a safe filename for a saved report."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in company_name.lower())
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{safe}-{timestamp}.{ext}"


def reports_dir() -> Path:
    """Return the directory where saved reports are written."""
    return _REPORTS_DIR
