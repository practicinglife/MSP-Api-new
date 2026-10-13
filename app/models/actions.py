"""Pydantic models for cross-platform actions (scans, alerts, commands)."""

from pydantic import BaseModel


class ScanRequest(BaseModel):
    """Request to trigger a scan on one or more devices."""

    platform: str
    device_ids: list[str]


class ScanResult(BaseModel):
    """Result of a scan initiation."""

    platform: str
    status: str
    detail: dict = {}  # type: ignore[type-arg]


class AlertRecord(BaseModel):
    """Normalised security alert from any platform."""

    platform: str
    alert_id: str
    severity: str = "unknown"
    title: str = ""
    description: str = ""
    device_hostname: str = ""
    device_id: str = ""
    company_name: str = ""
    created_at: str = ""
    raw: dict = {}  # type: ignore[type-arg]


class TicketRecord(BaseModel):
    """Normalised service ticket from a PSA platform."""

    platform: str
    ticket_id: str
    summary: str = ""
    status: str = ""
    priority: str = ""
    company_name: str = ""
    created_at: str = ""
    raw: dict = {}  # type: ignore[type-arg]


class CoverageRow(BaseModel):
    """One company's device coverage across all connected platforms."""

    company_name: str
    platforms: dict[str, int]  # platform_name → device count
    total_devices: int = 0
    missing_platforms: list[str] = []
