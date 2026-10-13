"""Abstract base class for all platform connectors."""

from abc import ABC, abstractmethod
from typing import Any

from app.models.company import CompanyRecord
from app.models.device import DeviceRecord


class ConnectorConfigError(Exception):
    """Raised when a connector is missing required configuration."""

    pass


class AbstractPlatformConnector(ABC):
    """Base class every platform connector must subclass.

    Subclasses must implement :attr:`platform_name` and all three async methods.
    Optional capability methods return ``None`` when unsupported.
    """

    platform_name: str

    # --- Capability flags (override in subclass to advertise features) ---
    supports_scan: bool = False
    supports_alerts: bool = False
    supports_install: bool = False
    supports_uninstall: bool = False
    supports_repair: bool = False
    supports_offboard: bool = False
    supports_company_create: bool = False
    supports_tickets: bool = False

    # --- OpenAPI / Swagger spec URL (override in subclass if available) ---
    swagger_url: str = ""

    @abstractmethod
    def check_config(self) -> list[str]:
        """Return a list of missing required configuration field names.

        Returns an empty list if all required configuration is present.
        """
        ...

    def validate_config(self) -> None:
        """Raise :class:`ConnectorConfigError` if required configuration is missing."""
        missing = self.check_config()
        if missing:
            raise ConnectorConfigError(
                f"{self.platform_name}: missing configuration — {', '.join(missing)}"
            )

    @abstractmethod
    async def authenticate(self) -> None:
        """Authenticate with the remote platform and cache credentials."""
        ...

    @abstractmethod
    async def get_companies(self) -> list[CompanyRecord]:
        """Return a list of normalised company records from the platform."""
        ...

    @abstractmethod
    async def get_devices(self) -> list[DeviceRecord]:
        """Return a list of normalised device records from the platform."""
        ...

    async def get_device_ids(self) -> list[dict[str, str]]:
        """Return a lightweight list of device identity stubs for delta detection.

        Each stub must contain at least:
          - ``platform_id``   — unique ID on this platform
          - ``company_name``  — raw company/site name from the platform
          - ``hostname``      — device hostname (may be empty)
          - ``updated_at``    — ISO8601 timestamp of last change (empty string if unknown)

        Default implementation falls back to ``get_devices()`` and strips fields.
        Connectors should override this with a minimal-field API call for speed.
        """
        devices = await self.get_devices()
        return [
            {
                "platform_id":  d.platform_id or "",
                "company_name": d.company_name or "",
                "hostname":     d.hostname or "",
                "updated_at":   "",
            }
            for d in devices
        ]

    async def get_devices_for_ids(self, platform_ids: list[str]) -> list[DeviceRecord]:
        """Return full device records for a specific subset of platform IDs.

        Default implementation fetches ALL devices and filters client-side.
        Connectors should override with an ID-filtered API call for speed.
        """
        all_devices = await self.get_devices()
        id_set = set(platform_ids)
        return [d for d in all_devices if d.platform_id in id_set]

    # --- Optional action capabilities ---

    async def initiate_scan(self, device_ids: list[str]) -> dict[str, Any] | None:
        """Trigger an antivirus / EDR scan on the specified devices.

        Returns a summary dict or ``None`` if unsupported.
        """
        return None

    async def get_scan_status(self, scan_id: str) -> dict[str, Any] | None:
        """Return the status of a previously triggered scan."""
        return None

    async def get_alerts(
        self, *, limit: int = 50, severity: str | None = None
    ) -> list[dict[str, Any]] | None:
        """Return recent security alerts / threats.

        Returns ``None`` if the platform does not support alerts.
        """
        return None

    async def run_command(
        self, device_ids: list[str], command: str, **kwargs: Any
    ) -> dict[str, Any] | None:
        """Execute a remote command (install, script, etc.) on devices.

        Returns ``None`` if unsupported.
        """
        return None

    async def get_tickets(
        self, *, company_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]] | None:
        """Return service tickets, optionally filtered by company.

        Returns ``None`` if the platform does not support tickets.
        """
        return None

    # --- Remediation capabilities ---

    async def install_agent(
        self, device_ids: list[str], *, company_id: str = "", **kwargs: Any
    ) -> dict[str, Any] | None:
        """Install or deploy the platform agent on target devices.

        Returns a result dict with ``queued``, ``failed``, ``errors`` keys,
        or ``None`` if unsupported.
        """
        return None

    async def uninstall_agent(
        self, device_ids: list[str], **kwargs: Any
    ) -> dict[str, Any] | None:
        """Uninstall / remove the platform agent from target devices.

        Returns ``None`` if unsupported.
        """
        return None

    async def repair_agent(
        self, device_ids: list[str], **kwargs: Any
    ) -> dict[str, Any] | None:
        """Repair or reinstall the platform agent on target devices.

        Returns ``None`` if unsupported.
        """
        return None

    async def offboard_device(
        self, device_ids: list[str], **kwargs: Any
    ) -> dict[str, Any] | None:
        """Fully offboard / decommission devices from the platform.

        Returns ``None`` if unsupported.
        """
        return None

    async def create_company_on_platform(
        self, name: str, **kwargs: Any
    ) -> dict[str, Any] | None:
        """Create a new company / site / tenant on the platform.

        Returns created company info dict, or ``None`` if unsupported.
        """
        return None

    async def list_scripts(
        self, *, company_id: str | None = None
    ) -> list[dict[str, Any]] | None:
        """List available automation scripts on the platform.

        Returns ``None`` if unsupported.
        """
        return None

    async def run_script(
        self,
        script_id: str,
        device_ids: list[str],
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """Execute a named automation script on target devices.

        Returns ``None`` if unsupported.
        """
        return None

    def capabilities(self) -> list[str]:
        """Return a list of supported optional capability names."""
        caps = []
        if self.supports_scan:
            caps.append("scan")
        if self.supports_alerts:
            caps.append("alerts")
        if self.supports_install:
            caps.append("install")
        if self.supports_uninstall:
            caps.append("uninstall")
        if self.supports_repair:
            caps.append("repair")
        if self.supports_offboard:
            caps.append("offboard")
        if self.supports_company_create:
            caps.append("company_create")
        if self.supports_tickets:
            caps.append("tickets")
        return caps
