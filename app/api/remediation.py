"""Remediation API — install, uninstall, repair, offboard, company creation,
and automation script endpoints.
"""

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.connector_health import get_connectors
from app.auth.write_mode import enforce_direct_write
from app.services import remediation as svc
from app.services.confirmation import build_preview, enforce_confirmation

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/remediation", tags=["remediation"])


def _target_platforms(requested: list[str] | None) -> list[str]:
    """Platforms a multi-platform action will touch (None = every configured connector)."""
    return list(requested) if requested else [c.platform_name for c in get_connectors()]


# ---- Request models ----


class InstallRequest(BaseModel):
    """POST body for installing an agent."""

    platform: str = Field(..., description="Target platform (sentinelone, eset, asio, screenconnect)")
    device_ids: list[str] = Field(..., min_length=1)
    company_id: str = ""
    company_name: str = Field("", description="Company name (used to auto-discover the per-company ESET Live Installer)")
    site_id: str = Field("", description="ASIO site ID (used to look up agent token for ASIO installs)")
    via_screenconnect: bool = Field(
        False,
        description="Send install command via ScreenConnect session instead of platform API",
    )
    download_url: str = ""
    msi_filename: str = Field("", description="MSI filename for ASIO agent install (derived from URL if omitted)")
    installer_filename: str = Field("", description="Filename for the ESET Live Installer in C:\\installs (default: eset_live_installer.exe)")
    site_token: str = ""
    script_id: str = ""
    confirm_token: str | None = Field(None, description="One-time token from the preview/confirmation step (required for AI agents)")


class UninstallRequest(BaseModel):
    """POST body for uninstalling an agent."""

    platform: str
    device_ids: list[str] = Field(..., min_length=1)
    via_screenconnect: bool = False
    passphrase: str = ""
    confirm_token: str | None = Field(None, description="One-time token from the preview/confirmation step (required for AI agents)")


class RepairRequest(BaseModel):
    """POST body for repairing / reinstalling an agent.

    For ScreenConnect repair, the real workflow uses an Asio automation
    script (``MSP_SC_REPAIR_SCRIPT``) that reinstalls the agent into the
    correct company context.  Provide ``company_id`` so the script can
    be auto-discovered, or pass ``script_name`` / ``script_id`` directly.
    """

    platform: str
    device_ids: list[str] = Field(..., min_length=1)
    company_id: str = Field("", description="Company ID — required for ScreenConnect repair (script lookup)")
    script_name: str = Field("", description="Asio script name override (default: MSP_SC_REPAIR_SCRIPT)")
    script_id: str = Field("", description="Asio script ID override (skip name lookup)")
    download_url: str = ""
    confirm_token: str | None = Field(None, description="One-time token from the preview/confirmation step (required for AI agents)")


class OffboardRequest(BaseModel):
    """POST body for offboarding devices.

    The real offboard workflow is a multi-step process:

    1. **Asio** uninstall (max 5 devices per batch) — this removes
       SentinelOne and ScreenConnect agents via the Asio platform.
    2. **ESET** uninstall is separate — searches the company for ESET
       products via the Asio applications API, then triggers removal.
       This requires ``company_id``.

    If more than 5 devices are submitted, they are batched automatically.
    """

    device_ids: list[str] = Field(..., min_length=1, max_length=5)
    company_id: str = Field("", description="Company ID — required for ESET removal (searches for ESET products)")
    platforms: list[str] | None = Field(
        None,
        description="Platforms to offboard from; None = all capable platforms",
    )
    include_eset: bool = Field(
        True,
        description="Also search for and uninstall ESET products (requires company_id)",
    )
    confirm_token: str | None = Field(None, description="One-time token from the preview/confirmation step (required for AI agents)")


class CreateCompanyRequest(BaseModel):
    """POST body for creating a company across platforms."""

    name: str = Field(..., min_length=1)
    platforms: list[str] | None = Field(
        None,
        description="Platforms to create on; None = all capable platforms",
    )
    address: str = ""
    phone: str = ""
    confirm_token: str | None = Field(None, description="One-time token from the preview/confirmation step (required for AI agents)")


class RunScriptRequest(BaseModel):
    """POST body for executing an automation script."""

    platform: str
    script_id: str
    device_ids: list[str] = Field(..., min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    confirm_token: str | None = Field(None, description="One-time token from the preview/confirmation step (required for AI agents)")


# ---- Capabilities ----


@router.get("/capabilities")
async def get_capabilities() -> list[dict[str, Any]]:
    """Return remediation capabilities for every registered connector."""
    return await svc.get_remediation_capabilities()


# ---- Install / Uninstall / Repair ----


@router.post(
    "/install",
    summary="[WRITE] Install an agent on one or more devices. Requires confirmation.",
    openapi_extra={"x-destructive": True},
)
async def install_agent(body: InstallRequest, request: Request) -> dict[str, Any]:
    """Install an agent on one or more devices."""
    await enforce_direct_write(body.platform)
    target = f"{body.platform}:{','.join(body.device_ids)}"
    enforce_confirmation(
        request,
        action="remediation.install",
        target=target,
        confirm_token=body.confirm_token,
        preview=build_preview(
            "remediation.install",
            target,
            platform=body.platform,
            device_ids=body.device_ids,
            company_id=body.company_id,
            via_screenconnect=body.via_screenconnect,
            effect="Installs/deploys an agent on the target devices.",
        ),
    )
    kwargs: dict[str, Any] = {}
    if body.download_url:
        kwargs["download_url"] = body.download_url
    if body.site_token:
        kwargs["site_token"] = body.site_token
    if body.script_id:
        kwargs["script_id"] = body.script_id
    if body.site_id:
        kwargs["site_id"] = body.site_id
    if body.msi_filename:
        kwargs["msi_filename"] = body.msi_filename
    if body.company_name:
        kwargs["company_name"] = body.company_name
    if body.installer_filename:
        kwargs["installer_filename"] = body.installer_filename

    result = await svc.install_agent(
        body.platform,
        body.device_ids,
        company_id=body.company_id,
        via_screenconnect=body.via_screenconnect,
        **kwargs,
    )
    return result.to_dict()


@router.post(
    "/uninstall",
    summary="[WRITE] Uninstall/remove an agent from devices. Requires confirmation.",
    openapi_extra={"x-destructive": True},
)
async def uninstall_agent(body: UninstallRequest, request: Request) -> dict[str, Any]:
    """Uninstall / remove an agent from one or more devices."""
    await enforce_direct_write(body.platform)
    target = f"{body.platform}:{','.join(body.device_ids)}"
    enforce_confirmation(
        request,
        action="remediation.uninstall",
        target=target,
        confirm_token=body.confirm_token,
        preview=build_preview(
            "remediation.uninstall",
            target,
            platform=body.platform,
            device_ids=body.device_ids,
            via_screenconnect=body.via_screenconnect,
            reversible=False,
            effect="Uninstalls/removes the agent from the target devices.",
        ),
    )
    kwargs: dict[str, Any] = {}
    if body.passphrase:
        kwargs["passphrase"] = body.passphrase

    result = await svc.uninstall_agent(
        body.platform,
        body.device_ids,
        via_screenconnect=body.via_screenconnect,
        **kwargs,
    )
    return result.to_dict()


@router.post(
    "/repair",
    summary="[WRITE] Repair/reinstall an agent on devices. Requires confirmation.",
    openapi_extra={"x-destructive": True},
)
async def repair_agent(body: RepairRequest, request: Request) -> dict[str, Any]:
    """Repair / reinstall an agent on one or more devices.

    For ScreenConnect, this routes through the Asio repair
    automation script (``MSP_SC_REPAIR_SCRIPT``) which ensures the agent is reinstalled into the
    correct company context.
    """
    await enforce_direct_write(body.platform)
    if body.platform == "screenconnect":  # repaired by running an Asio automation script
        await enforce_direct_write("asio")
    target = f"{body.platform}:{','.join(body.device_ids)}"
    enforce_confirmation(
        request,
        action="remediation.repair",
        target=target,
        confirm_token=body.confirm_token,
        preview=build_preview(
            "remediation.repair",
            target,
            platform=body.platform,
            device_ids=body.device_ids,
            company_id=body.company_id,
            effect="Repairs/reinstalls the agent on the target devices.",
        ),
    )
    kwargs: dict[str, Any] = {}
    if body.download_url:
        kwargs["download_url"] = body.download_url
    if body.company_id:
        kwargs["company_id"] = body.company_id
    if body.script_name:
        kwargs["script_name"] = body.script_name
    if body.script_id:
        kwargs["script_id"] = body.script_id

    result = await svc.repair_agent(body.platform, body.device_ids, **kwargs)
    return result.to_dict()


# ---- Offboard ----


@router.post(
    "/offboard",
    summary="[WRITE] Offboard devices across platforms (S1 + ScreenConnect, optional ESET). Requires confirmation.",
    openapi_extra={"x-destructive": True},
)
async def offboard_device(body: OffboardRequest, request: Request) -> list[dict[str, Any]]:
    """Offboard devices from platforms.

    Asio uninstall (max 5) removes S1 + ScreenConnect.
    ESET removal is a separate step if ``include_eset`` is True.
    """
    for _platform in _target_platforms(body.platforms):
        await enforce_direct_write(_platform)
    target = ','.join(body.device_ids)
    enforce_confirmation(
        request,
        action="remediation.offboard",
        target=target,
        confirm_token=body.confirm_token,
        preview=build_preview(
            "remediation.offboard",
            target,
            device_ids=body.device_ids,
            company_id=body.company_id,
            platforms=body.platforms,
            include_eset=body.include_eset,
            reversible=False,
            effect="Removes agents (S1 + ScreenConnect via Asio, optional ESET) from the target devices.",
        ),
    )
    results = await svc.offboard_device(
        body.device_ids,
        platforms=body.platforms,
        company_id=body.company_id,
        include_eset=body.include_eset,
    )
    return [r.to_dict() for r in results]


# ---- Company creation ----


@router.post(
    "/create-company",
    summary="[WRITE] Create a company across platforms. Requires confirmation.",
    openapi_extra={"x-destructive": True},
)
async def create_company(body: CreateCompanyRequest, request: Request) -> list[dict[str, Any]]:
    """Create a company uniformly across platforms."""
    for _platform in _target_platforms(body.platforms):
        await enforce_direct_write(_platform)
    enforce_confirmation(
        request,
        action="remediation.create_company",
        target=body.name,
        confirm_token=body.confirm_token,
        preview=build_preview(
            "remediation.create_company",
            body.name,
            name=body.name,
            platforms=body.platforms,
            effect="Creates a new company record across the selected platforms.",
        ),
    )
    kwargs: dict[str, Any] = {}
    if body.address:
        kwargs["address"] = body.address
    if body.phone:
        kwargs["phone"] = body.phone

    results = await svc.create_company(
        body.name, platforms=body.platforms, **kwargs,
    )
    return [r.to_dict() for r in results]


# ---- Automation scripts ----


@router.get("/scripts/{platform}")
async def list_scripts(
    platform: str, company_id: str | None = None,
) -> list[dict[str, Any]]:
    """List available automation scripts for a platform (e.g. Asio)."""
    try:
        return await svc.list_platform_scripts(platform, company_id=company_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.error("remediation.list_scripts.error", platform=platform, error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/scripts/run",
    summary="[WRITE] Execute an automation script on target devices. Requires confirmation.",
    openapi_extra={"x-destructive": True},
)
async def run_script(body: RunScriptRequest, request: Request) -> dict[str, Any]:
    """Execute an automation script on target devices."""
    await enforce_direct_write(body.platform)
    target = f"{body.platform}:{body.script_id}:{','.join(body.device_ids)}"
    enforce_confirmation(
        request,
        action="remediation.run_script",
        target=target,
        confirm_token=body.confirm_token,
        preview=build_preview(
            "remediation.run_script",
            target,
            platform=body.platform,
            script_id=body.script_id,
            device_ids=body.device_ids,
            effect="Executes an automation script against the target devices.",
        ),
    )
    kwargs: dict[str, Any] = {}
    if body.parameters:
        kwargs["parameters"] = body.parameters

    result = await svc.run_platform_script(
        body.platform, body.script_id, body.device_ids, **kwargs,
    )
    return result.to_dict()


# ---- ASIO site & installer info ----


@router.get("/asio/sites/{company_id}")
async def get_asio_sites(company_id: str) -> list[dict[str, Any]]:
    """Return all sites for an ASIO company (used to select site for agent install)."""
    try:
        return await svc.get_asio_sites(company_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.error("remediation.asio_sites.error", company_id=company_id, error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/asio/installer/{company_id}")
async def get_asio_installer_info(
    company_id: str,
    site_id: str | None = None,
) -> dict[str, Any]:
    """Return ASIO agent installer details (URL, token, filename) for a company/site.

    Used by the frontend to preview the installer URL before triggering
    the install via ScreenConnect.
    """
    try:
        return await svc.get_asio_installer_info(company_id, site_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.error(
            "remediation.asio_installer.error",
            company_id=company_id,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/asio/tasks/summary/{endpoint_id}")
async def get_asio_tasks_summary(endpoint_id: str) -> list[dict[str, Any]]:
    """Return ASIO automation task summary for one endpoint."""
    try:
        return await svc.get_asio_endpoint_tasks_summary(endpoint_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.error(
            "remediation.asio_tasks_summary.error",
            endpoint_id=endpoint_id,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---- ESET Live Installers ----


@router.get("/eset/installers")
async def get_eset_installers(
    company_name: str | None = None,
) -> list[dict[str, Any]]:
    """Return per-company ESET Live Installer packages from the ESET Connect API.

    These are the same installer links shown in the ESET Protect console's
    Installers page.  Each record includes the enrollment group (company),
    download URL, usability flag, and expiry.

    Optionally filter by ``company_name`` (case-insensitive substring match
    against the ESET group name).  Usable installers sort first, newest first.
    """
    try:
        return await svc.get_eset_installers(company_name or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.error(
            "remediation.eset_installers.error",
            company_name=company_name,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc
