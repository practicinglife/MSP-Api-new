"""ScreenConnect (ConnectWise Control) connector.

Uses the RESTful API Manager extension (v1.0.8+).
Docs: https://docs.connectwise.com/ConnectWise_ScreenConnect/Developers/RESTful_API_Manager

Auth:     Custom ``CTRLAuthHeader`` header with the ``RESTfulAuthenticationSecret``
Endpoint: POST /App_Extensions/{ext_id}/Service.ashx/GetSessionsByFilter
Body:     JSON array of parameters, e.g. ``[""]`` for all sessions
"""

from typing import Any

import httpx
import structlog

from app.config import Settings, get_settings
from app.connectors.base import AbstractPlatformConnector
from app.models.company import CompanyRecord
from app.models.device import DeviceRecord
from app.services.normalizers import (
    normalize_company_name,
    normalize_hostname,
    normalize_mac,
    normalize_serial,
)

log = structlog.get_logger(__name__)


class ScreenConnectConnector(AbstractPlatformConnector):
    """Connector for ScreenConnect / ConnectWise Control.

    Discovers sessions via the RESTful API Manager extension and extracts
    company and device data from the returned session objects.

    Auth: ``CTRLAuthHeader`` custom HTTP header whose value must match the
    ``RESTfulAuthenticationSecret`` configured in the extension settings.
    """

    platform_name = "screenconnect"
    supports_install = True
    supports_repair = True

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None
        self._last_auth_status: str = "not_attempted"
        self._last_auth_detail: str = ""
        self._last_api_url: str = ""
        self._last_api_status: int = 0

    # ── configuration helpers ──────────────────────────────────────

    def check_config(self) -> list[str]:
        missing: list[str] = []
        if not self._settings.screenconnect_base_url:
            missing.append("SCREENCONNECT_BASE_URL")
        if not self._settings.screenconnect_auth_token:
            missing.append("SCREENCONNECT_AUTH_TOKEN (RESTfulAuthenticationSecret)")
        return missing

    def _base_url(self) -> str:
        return self._settings.screenconnect_base_url.rstrip("/")

    def _ext_id(self) -> str:
        return self._settings.screenconnect_extension_id

    def _service_url(self, method: str) -> str:
        """Full URL for a REST extension service method."""
        return (
            f"{self._base_url()}/App_Extensions/"
            f"{self._ext_id()}/Service.ashx/{method}"
        )

    def _auth_headers(self) -> dict[str, str]:
        """Build request headers with the CTRLAuthHeader.

        Per the ScreenConnect RESTful API Manager docs, requests must
        include a ``CTRLAuthHeader`` whose value matches the
        ``RESTfulAuthenticationSecret`` extension setting.

        The ``Origin`` header is also required when the extension has
        ``RESTfulAllowedOrigin`` configured — we set it to the instance
        base URL so it always matches.
        """
        return {
            "Content-Type": "application/json",
            "CTRLAuthHeader": self._settings.screenconnect_auth_token,
            "Origin": self._base_url(),
        }

    # ── persistent HTTP client ─────────────────────────────────────

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Return the persistent HTTP client, creating if needed."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=60.0,
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        """Close the persistent HTTP client and release resources."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ── authenticate ───────────────────────────────────────────────

    async def authenticate(self) -> None:
        """Validate credentials by making a lightweight API call.

        Calls ``GetSessionsByFilter`` with a wildcard filter to verify the
        ``CTRLAuthHeader`` secret is accepted.
        """
        client = await self._ensure_client()
        url = self._service_url("GetSessionsByFilter")
        self._last_api_url = url

        log.info(
            "screenconnect.authenticate.start",
            url=url,
            auth_token_len=len(self._settings.screenconnect_auth_token or ""),
        )

        try:
            response = await client.post(
                url,
                headers=self._auth_headers(),
                json=["SessionType = 'Access' OR SessionType = 'Meeting' OR SessionType = 'Support'"],
            )
            self._last_api_status = response.status_code

            if response.status_code == 401:
                self._last_auth_status = "failed (401 Unauthorized)"
                self._last_auth_detail = (
                    "CTRLAuthHeader rejected — verify the secret matches "
                    "RESTfulAuthenticationSecret in the extension settings"
                )
                log.error(
                    "screenconnect.authenticate.unauthorized",
                    url=url,
                    status=401,
                )
                raise RuntimeError(
                    "ScreenConnect 401 Unauthorized — check that "
                    "SCREENCONNECT_AUTH_TOKEN matches the "
                    "RESTfulAuthenticationSecret in the extension settings"
                )

            response.raise_for_status()
            self._last_auth_status = "ok"
            self._last_auth_detail = f"HTTP {response.status_code}"
            log.info(
                "screenconnect.authenticate.ok",
                status=response.status_code,
            )
        except httpx.ConnectError as exc:
            self._last_auth_status = "connection_error"
            self._last_auth_detail = str(exc)
            log.error("screenconnect.authenticate.connect_error", error=str(exc))
            raise RuntimeError(
                f"Cannot connect to ScreenConnect: {self._base_url()} — {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            self._last_auth_status = "timeout"
            self._last_auth_detail = str(exc)
            log.error("screenconnect.authenticate.timeout", error=str(exc))
            raise RuntimeError(
                f"Timeout connecting to ScreenConnect: {self._base_url()}"
            ) from exc

    # ── data fetching ──────────────────────────────────────────────

    async def _fetch_sessions(self, session_filter: str = " ") -> list[dict[str, Any]]:
        """Fetch sessions via POST GetSessionsByFilter.

        Per the extension docs, body data is passed as a JSON array of
        parameters: ``["<sessionFilter>"]``.

        Note: A single space ``" "`` is used as the default wildcard filter.
        The extension rejects truly empty strings with a 500 error.
        """
        client = await self._ensure_client()
        url = self._service_url("GetSessionsByFilter")
        self._last_api_url = url

        response = await client.post(
            url,
            headers=self._auth_headers(),
            json=[session_filter],
        )
        self._last_api_status = response.status_code
        response.raise_for_status()
        data = response.json()

        if isinstance(data, list):
            log.info("screenconnect.fetch_sessions", count=len(data))
            return data

        log.warning(
            "screenconnect.fetch_sessions.unexpected_format",
            type=type(data).__name__,
        )
        return []

    # ── diagnostics ────────────────────────────────────────────────

    def get_diagnostics(self) -> dict[str, Any]:
        """Return a diagnostic dict for debugging connection issues."""
        return {
            "base_url": self._base_url(),
            "extension_id": self._ext_id(),
            "sessions_url": self._service_url("GetSessionsByFilter"),
            "auth_method": "CTRLAuthHeader (RESTfulAuthenticationSecret)",
            "auth_token_set": bool(self._settings.screenconnect_auth_token),
            "auth_token_length": len(self._settings.screenconnect_auth_token or ""),
            "last_auth_status": self._last_auth_status,
            "last_auth_detail": self._last_auth_detail,
            "last_api_url": self._last_api_url,
            "last_api_status": self._last_api_status,
        }

    # ── company / device extraction ────────────────────────────────

    async def get_companies(self) -> list[CompanyRecord]:
        """Return unique company names derived from session CustomPropertyValues."""
        sessions = await self._fetch_sessions()

        seen: set[str] = set()
        records: list[CompanyRecord] = []
        for item in sessions:
            company_name = self._extract_company(item)
            norm = normalize_company_name(company_name)
            if norm and norm not in seen:
                seen.add(norm)
                records.append(
                    CompanyRecord(
                        platform=self.platform_name,
                        platform_id=norm,
                        name=norm,
                        raw=item,
                    )
                )
        log.info("screenconnect.get_companies", count=len(records))
        return records

    async def get_devices(self) -> list[DeviceRecord]:
        """Return sessions as device records.

        Extracts Company (CP1), Site (CP2), and Asio Site ID (CP5) from
        ``CustomPropertyValues`` to populate the hierarchical fields.
        """
        sessions = await self._fetch_sessions()

        records: list[DeviceRecord] = []
        for session in sessions:
            session_id = str(session.get("SessionID", ""))
            hostname = session.get("GuestMachineName", "")
            # Fall back to session Name when GuestMachineName is empty
            if not hostname:
                hostname = session.get("Name", "")
            company = self._extract_company(session)
            site = self._extract_site(session)
            asio_site_id = self._extract_asio_site_id(session)
            serial = session.get("GuestMachineSerialNumber", "")

            # MAC addresses may be a list or semicolon/comma-separated string
            macs_raw = session.get("GuestHardwareNetworkAddress", "")
            if isinstance(macs_raw, list):
                mac_list = macs_raw
            elif isinstance(macs_raw, str) and macs_raw:
                mac_list = [m.strip() for m in macs_raw.replace(";", ",").split(",") if m.strip()]
            else:
                mac_list = []
            macs = [normalize_mac(m) for m in mac_list if m]

            records.append(
                DeviceRecord(
                    platform=self.platform_name,
                    platform_id=session_id,
                    hostname=normalize_hostname(hostname),
                    serial_number=normalize_serial(serial),
                    mac_addresses=[m for m in macs if m],
                    company_platform_id=normalize_company_name(company),
                    company_name=normalize_company_name(company),
                    site_id=asio_site_id,
                    site_name=site,
                    raw=session,
                )
            )
        log.info("screenconnect.get_devices", count=len(records))
        return records

    @staticmethod
    def _extract_company(session: dict[str, Any]) -> str:
        """Extract company name from a session dict.

        ConnectWise Control stores company in CustomProperty1 (index 0).
        """
        cp_vals = session.get("CustomPropertyValues")
        if isinstance(cp_vals, list) and cp_vals:
            return str(cp_vals[0]) if cp_vals[0] else ""
        return ""

    @staticmethod
    def _extract_site(session: dict[str, Any]) -> str:
        """Extract site name from CustomProperty2 (index 1)."""
        cp_vals = session.get("CustomPropertyValues")
        if isinstance(cp_vals, list) and len(cp_vals) > 1:
            return str(cp_vals[1]) if cp_vals[1] else ""
        return ""

    @staticmethod
    def _extract_asio_site_id(session: dict[str, Any]) -> str:
        """Extract the Asio Site ID stored in CustomProperty5 (index 4).

        This numeric ID is used as a cross-reference identifier to link
        ScreenConnect sessions back to their Asio site.
        """
        cp_vals = session.get("CustomPropertyValues")
        if isinstance(cp_vals, list) and len(cp_vals) > 4:
            val = cp_vals[4]
            return str(val) if val else ""
        return ""

    @staticmethod
    def _extract_custom_property(session: dict[str, Any], index: int) -> str:
        """Extract a custom property value by zero-based index."""
        cp_vals = session.get("CustomPropertyValues")
        if isinstance(cp_vals, list) and len(cp_vals) > index:
            val = cp_vals[index]
            return str(val) if val else ""
        return ""

    # ── Remote command execution ───────────────────────────────────

    async def run_command(
        self, device_ids: list[str], command: str, **kwargs: Any
    ) -> dict[str, Any] | None:
        """Send a command to ScreenConnect sessions for execution.

        Uses the RESTful API Manager ``SendCommandToSession`` method.
        ``device_ids`` are ScreenConnect session GUIDs.

        Parameters
        ----------
        device_ids : list[str]
            Session GUIDs to send the command to.
        command : str
            The command string to execute on the remote endpoint.

        Returns
        -------
        dict with ``sent``, ``failed``, and ``errors`` keys.
        """
        client = await self._ensure_client()
        url = self._service_url("SendCommandToSession")
        sent = 0
        failed = 0
        errors: list[dict[str, str]] = []

        for session_id in device_ids:
            try:
                response = await client.post(
                    url,
                    headers=self._auth_headers(),
                    json=[session_id, command],
                )
                response.raise_for_status()
                sent += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append({"session_id": session_id, "error": str(exc)})
                log.warning(
                    "screenconnect.run_command.failed",
                    session_id=session_id,
                    error=str(exc),
                )

        log.info(
            "screenconnect.run_command.done",
            total=len(device_ids),
            sent=sent,
            failed=failed,
        )
        return {"sent": sent, "failed": failed, "errors": errors}

    async def update_session_custom_properties(
        self,
        session_id: str,
        properties: list[str],
    ) -> bool:
        """Update custom property values on a ScreenConnect session.

        Uses the RESTful API Manager ``UpdateSessionCustomProperties``
        method.  ``properties`` is a list of string values corresponding
        to CustomProperty1…N (index 0…N-1).

        Parameters
        ----------
        session_id : str
            The session GUID.
        properties : list[str]
            New custom property values (all 8 positions, use empty string
            to leave unchanged).

        Returns
        -------
        True on success, False on failure.
        """
        client = await self._ensure_client()
        url = self._service_url("UpdateSessionCustomProperties")

        try:
            response = await client.post(
                url,
                headers=self._auth_headers(),
                json=[session_id, properties],
            )
            response.raise_for_status()
            log.info(
                "screenconnect.update_custom_properties.ok",
                session_id=session_id,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "screenconnect.update_custom_properties.failed",
                session_id=session_id,
                error=str(exc),
            )
            return False

    async def get_session_details(self, session_id: str) -> dict[str, Any] | None:
        """Fetch details for a single session by GUID.

        Uses ``GetSessionDetails`` to retrieve the full session object
        including custom properties (which may contain AV detection results).
        """
        client = await self._ensure_client()
        url = self._service_url("GetSessionDetails")

        try:
            response = await client.post(
                url,
                headers=self._auth_headers(),
                json=[session_id],
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "screenconnect.get_session_details.failed",
                session_id=session_id,
                error=str(exc),
            )
            return None

    # --- Remediation capabilities ---

    # PowerShell templates for common remediation actions.
    # These are sent via the existing run_command() infrastructure.

    _SC_REINSTALL_CMD = (
        '$msi = "$env:TEMP\\ScreenConnect.ClientSetup.msi"; '
        'if (Test-Path $msi) {{ '
        'msiexec /x $msi /qn; Start-Sleep -Seconds 5 }}; '
        'Invoke-WebRequest -Uri "{download_url}" -OutFile $msi; '
        'msiexec /i $msi /qn /norestart'
    )

    _S1_INSTALL_CMD = (
        '$installer = "$env:TEMP\\SentinelOneInstaller.exe"; '
        'Invoke-WebRequest -Uri "{download_url}" -OutFile $installer; '
        'Start-Process -FilePath $installer -ArgumentList "/SITE_TOKEN={site_token} /quiet" '
        '-Wait -NoNewWindow'
    )

    _ESET_INSTALL_CMD = (
        '#!ps\n'
        '#timeout=600000\n'
        '$dir = "C:\\installs"\n'
        'if (!(Test-Path $dir)) {{ New-Item -ItemType Directory -Path $dir | Out-Null }}\n'
        '$installer = Join-Path $dir "{installer_filename}"\n'
        '# Overwrite any stale installer — GUIDs/tokens change per company\n'
        'if (Test-Path $installer) {{ Remove-Item $installer -Force }}\n'
        'try {{\n'
        '    Invoke-WebRequest -Uri "{download_url}" -OutFile $installer -UseBasicParsing -ErrorAction Stop\n'
        '    Write-Host "Downloaded ESET Live Installer: $installer"\n'
        '}} catch {{\n'
        '    Write-Error "Failed to download ESET installer: $_"\n'
        '    exit 1\n'
        '}}\n'
        'try {{\n'
        '    Write-Host "Starting silent ESET install..."\n'
        '    Start-Process -FilePath $installer -ArgumentList "--silent --accepteula" -Wait -NoNewWindow\n'
        '    Write-Host "ESET install completed."\n'
        '}} catch {{\n'
        '    Write-Error "ESET install failed: $_"\n'
        '    exit 1\n'
        '}}'
    )

    _ASIO_INSTALL_CMD = (
        '#!ps\n'
        '#timeout=220000\n'
        '$url = "{download_url}"\n'
        '$destination = "C:\\Temp\\{msi_filename}"\n'
        'if (!(Test-Path -Path "C:\\Temp")) {{ New-Item -ItemType Directory -Path "C:\\Temp" | Out-Null }}\n'
        'try {{\n'
        '    Invoke-WebRequest -Uri $url -OutFile $destination -UseBasicParsing -ErrorAction Stop\n'
        '    Write-Host "Download successful: $destination"\n'
        '}} catch {{\n'
        '    Write-Error "Failed to download MSI: $_"\n'
        '    exit 1\n'
        '}}\n'
        'if (Test-Path $destination) {{\n'
        '    try {{\n'
        '        Write-Host "Starting silent installation..."\n'
        '        Start-Process -FilePath "msiexec.exe" -ArgumentList "/i `"$destination`" /qn /norestart" -Wait -NoNewWindow\n'
        '        Write-Host "Installation completed successfully."\n'
        '    }} catch {{\n'
        '        Write-Error "Installation failed: $_"\n'
        '        exit 1\n'
        '    }}\n'
        '}} else {{\n'
        '    Write-Error "MSI file not found at $destination"\n'
        '    exit 1\n'
        '}}'
    )

    async def install_agent(
        self, device_ids: list[str], *, company_id: str = "", **kwargs: Any
    ) -> dict[str, Any] | None:
        """Install an agent on endpoints via ScreenConnect remote command.

        Supported ``agent_type`` values in kwargs:
        - ``sentinelone``: Requires ``download_url`` and ``site_token``.
        - ``eset``: Requires ``download_url`` (per-company ESET Live Installer
          link).  Downloads into ``C:\\installs`` (overwriting any stale
          installer) and runs it silently.  Optional ``installer_filename``.
        - ``screenconnect``: Requires ``download_url`` for the SC installer.
        - ``custom``: Requires ``command`` with the raw PowerShell to execute.

        ``device_ids`` are ScreenConnect session GUIDs.
        """
        agent_type = kwargs.get("agent_type", "custom")
        command = kwargs.get("command", "")

        if agent_type == "sentinelone":
            download_url = kwargs.get("download_url", "")
            site_token = kwargs.get("site_token", "")
            if not download_url or not site_token:
                return {"error": "download_url and site_token are required for SentinelOne install"}
            command = self._S1_INSTALL_CMD.format(
                download_url=download_url, site_token=site_token,
            )
        elif agent_type == "asio":
            download_url = kwargs.get("download_url", "")
            msi_filename = kwargs.get("msi_filename", "asio_agent.msi")
            if not download_url:
                return {"error": "download_url is required for ASIO agent install"}
            command = self._ASIO_INSTALL_CMD.format(
                download_url=download_url, msi_filename=msi_filename,
            )
        elif agent_type == "eset":
            download_url = kwargs.get("download_url", "")
            installer_filename = kwargs.get("installer_filename", "") or "eset_live_installer.exe"
            if not download_url:
                return {"error": "download_url is required for ESET install"}
            command = self._ESET_INSTALL_CMD.format(
                download_url=download_url, installer_filename=installer_filename,
            )
        elif agent_type == "screenconnect":
            download_url = kwargs.get("download_url", "")
            if not download_url:
                return {"error": "download_url is required for ScreenConnect install"}
            command = self._SC_REINSTALL_CMD.format(download_url=download_url)
        elif not command:
            return {"error": "command or agent_type is required"}

        log.info(
            "screenconnect.install_agent",
            agent_type=agent_type,
            devices=len(device_ids),
        )
        return await self.run_command(device_ids, command)

    async def repair_agent(
        self, device_ids: list[str], **kwargs: Any
    ) -> dict[str, Any] | None:
        """Repair / reinstall ScreenConnect on endpoints (uninstall + MSI reinstall).

        Uninstalls the existing ScreenConnect MSI, then downloads and installs
        a fresh copy from the provided URL.

        Requires ``download_url`` in kwargs pointing to the SC client MSI.
        ``device_ids`` are ScreenConnect session GUIDs.
        """
        download_url = kwargs.get("download_url", "")
        if not download_url:
            # Use the instance URL to build a default download link
            download_url = f"{self._base_url()}/Bin/ScreenConnect.ClientSetup.msi"

        command = self._SC_REINSTALL_CMD.format(download_url=download_url)
        log.info("screenconnect.repair_agent", devices=len(device_ids))
        return await self.run_command(device_ids, command)

    async def uninstall_agent(
        self, device_ids: list[str], **kwargs: Any
    ) -> dict[str, Any] | None:
        """Uninstall an agent from endpoints via ScreenConnect remote command.

        Supported ``agent_type`` values in kwargs:
        - ``sentinelone``: Sends S1 uninstall passphrase command.
        - ``eset``: Sends ESET uninstaller command.
        - ``screenconnect``: Uninstalls the SC client MSI.
        - ``custom``: Raw PowerShell command.
        """
        agent_type = kwargs.get("agent_type", "custom")
        command = kwargs.get("command", "")

        if agent_type == "sentinelone":
            passphrase = kwargs.get("passphrase", "")
            command = (
                f'"C:\\Program Files\\SentinelOne\\Sentinel Agent *\\SentinelCtl.exe" '
                f'uninstall --passphrase="{passphrase}"'
                if passphrase
                else 'echo "SentinelOne passphrase required for uninstall"'
            )
        elif agent_type == "eset":
            command = (
                'msiexec /x {ESET_GUID} /qn /norestart; '
                'if (Test-Path "C:\\Program Files\\ESET") { '
                'Remove-Item -Path "C:\\Program Files\\ESET" -Recurse -Force }'
            )
        elif agent_type == "screenconnect":
            command = (
                '$msi = "$env:TEMP\\ScreenConnect.ClientSetup.msi"; '
                'if (Test-Path $msi) { msiexec /x $msi /qn /norestart }'
            )
        elif not command:
            return {"error": "command or agent_type is required"}

        log.info(
            "screenconnect.uninstall_agent",
            agent_type=agent_type,
            devices=len(device_ids),
        )
        return await self.run_command(device_ids, command)
