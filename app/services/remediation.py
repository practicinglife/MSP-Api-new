"""Remediation service — orchestrates install, uninstall, repair, offboard,
and company creation actions across all platform connectors.

This service is the single entry-point for all remediation operations.
It resolves the appropriate connector(s) for each action and delegates
to the connector's implementation.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog

from app.api.connector_health import get_connectors
from app.connectors.base import AbstractPlatformConnector
from app.org_profile import SC_REPAIR_SCRIPT
from app.services.normalizers import normalize_company_name

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_connector(platform: str) -> AbstractPlatformConnector | None:
    """Locate a registered connector by platform name."""
    for c in get_connectors():
        if c.platform_name == platform:
            return c
    return None


def _find_connector_strict(platform: str) -> AbstractPlatformConnector:
    """Locate a connector or raise ValueError."""
    c = _find_connector(platform)
    if c is None:
        raise ValueError(f"Platform not found or not configured: {platform}")
    return c


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

class RemediationResult:
    """Lightweight container for remediation action results."""

    def __init__(
        self,
        action: str,
        platform: str,
        success: bool,
        detail: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        self.action = action
        self.platform = platform
        self.success = success
        self.detail = detail or {}
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "platform": self.platform,
            "success": self.success,
            "detail": self.detail,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Orchestration functions
# ---------------------------------------------------------------------------

async def install_agent(
    platform: str,
    device_ids: list[str],
    *,
    company_id: str = "",
    via_screenconnect: bool = False,
    **kwargs: Any,
) -> RemediationResult:
    """Install an agent on target devices.

    Platform-specific routing:

    - **asio**: Always via ScreenConnect — fetches installer URL from
      Asio API, then sends the PowerShell download+install command through
      ScreenConnect's remote command capability.

    - **eset**: Preferred path is the per-company **ESET Live Installer**
      fetched from the ESET Connect API (same links as the Protect console's
      Installers page).  If ``download_url`` is provided it is used directly;
      otherwise the system auto-discovers the newest usable installer for
      ``company_name``.  The install command downloads into ``C:\\installs``
      (overwriting any stale installer) and runs it silently via
      ScreenConnect.  Legacy fallback: per-company Asio automation scripts
      (``script_id`` or script name containing ``"eset"``).

    - **other**: Uses the platform's own ``install_agent()`` method, or
      routes through ScreenConnect if ``via_screenconnect`` is True.
    """
    action = f"install_{platform}"
    try:
        # ── ASIO installs always go through ScreenConnect ──
        if platform == "asio":
            asio = _find_connector_strict("asio")
            await asio.authenticate()
            # Get installer info (URL, filename, token) from ASIO API
            info = await asio.install_agent(
                device_ids, company_id=company_id, **kwargs,
            )
            if info and info.get("error"):
                return RemediationResult(action, platform, False, info, info["error"])

            # Now send the install command via ScreenConnect
            sc = _find_connector_strict("screenconnect")
            await sc.authenticate()
            result = await sc.install_agent(
                device_ids,
                company_id=company_id,
                agent_type="asio",
                download_url=info.get("download_url", ""),
                msi_filename=info.get("msi_filename", "asio_agent.msi"),
            )
            if result and result.get("error"):
                return RemediationResult(action, platform, False, result, result["error"])

            # Merge ASIO info with ScreenConnect result
            combined = {**(info or {}), **(result or {})}
            log.info("remediation.install.asio_via_sc", devices=len(device_ids))
            return RemediationResult(action, platform, True, combined)

        # ── ESET installs ──
        if platform == "eset":
            download_url = kwargs.get("download_url", "")
            company_name = kwargs.get("company_name", "")
            installer_meta: dict[str, Any] = {}

            # Step 1: Auto-discover ESET Live Installer URL from ESET Connect
            if not download_url and company_name:
                try:
                    installers = await get_eset_installers(company_name)
                    usable = [
                        i for i in installers
                        if i["usable"] and i["download_url"]
                    ]
                    usable.sort(
                        key=lambda i: i.get("create_time", ""), reverse=True,
                    )
                    if usable:
                        download_url = usable[0]["download_url"]
                        installer_meta = usable[0]
                        log.info(
                            "remediation.install.eset_installer_found",
                            group=installer_meta.get("group_name"),
                            installer=installer_meta.get("display_name"),
                            company_name=company_name,
                        )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "remediation.install.eset_installer_lookup_failed",
                        company_name=company_name,
                        error=str(exc),
                    )

            if not download_url:
                return RemediationResult(
                    action, platform, False,
                    error="No ESET Live Installer URL found from ESET Connect. "
                          "Ensure company_name matches ESET Connect and has active installers.",
                )

            # Step 2: Get ASIO connector
            asio = _find_connector_strict("asio")
            await asio.authenticate()

            provided_company_id = bool(company_id)
            endpoint_company_ids: dict[str, str] = {}
            endpoint_company_names: dict[str, str] = {}
            # Avoid expensive full Asio device inventory lookup when caller already
            # provided an explicit company_id from the breakdown selection.
            if device_ids and not provided_company_id:
                try:
                    asio_devices = await asio.get_devices()
                    endpoint_company_ids = {
                        str(d.endpoint_id).lower(): str(d.company_platform_id)
                        for d in asio_devices
                        if getattr(d, "endpoint_id", "") and getattr(d, "company_platform_id", "")
                    }
                    endpoint_company_names = {
                        str(d.endpoint_id).lower(): str(d.company_name or "")
                        for d in asio_devices
                        if getattr(d, "endpoint_id", "")
                    }
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "remediation.install.eset_endpoint_company_lookup_failed",
                        endpoint_count=len(device_ids),
                        error=str(exc),
                    )

            # If company_id not provided, resolve from endpoint IDs first (most reliable).
            if not company_id and device_ids and endpoint_company_ids:
                resolved_ids = [
                    endpoint_company_ids.get(str(eid).lower(), "")
                    for eid in device_ids
                ]
                resolved_ids = [cid for cid in resolved_ids if cid]
                if resolved_ids:
                    company_id = resolved_ids[0]
                    unique_ids = set(resolved_ids)
                    if len(unique_ids) > 1:
                        log.warning(
                            "remediation.install.eset_mixed_company_ids",
                            endpoint_count=len(device_ids),
                            company_ids=list(unique_ids),
                        )

            # If still missing, resolve from company_name via ASIO companies list.
            if not company_id and company_name:
                try:
                    companies = await asio.get_companies()
                    target_raw = company_name.lower().strip()
                    target_norm = normalize_company_name(company_name)

                    # 1) Exact normalized match (handles punctuation/casing drift).
                    for comp in companies:
                        comp_name = str(comp.get("name", ""))
                        if normalize_company_name(comp_name) == target_norm:
                            company_id = str(comp.get("id", ""))
                            break

                    # 2) Exact raw case-insensitive match.
                    if not company_id:
                        for comp in companies:
                            comp_name = str(comp.get("name", "")).lower().strip()
                            if comp_name == target_raw:
                                company_id = str(comp.get("id", ""))
                                break

                    # 3) Substring fallback (normalized first, then raw).
                    if not company_id:
                        for comp in companies:
                            comp_name = str(comp.get("name", ""))
                            comp_norm = normalize_company_name(comp_name)
                            comp_raw = comp_name.lower().strip()
                            if (target_norm and target_norm in comp_norm) or (target_raw and target_raw in comp_raw):
                                company_id = str(comp.get("id", ""))
                                break
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "remediation.install.eset_company_lookup_failed",
                        company_name=company_name,
                        error=str(exc),
                    )

            if not company_id:
                return RemediationResult(
                    action,
                    platform,
                    False,
                    detail={
                        "device_ids": device_ids,
                        "endpoint_company_ids_found": {
                            eid: endpoint_company_ids.get(str(eid).lower(), "")
                            for eid in device_ids
                        },
                    },
                    error=f"Could not resolve company_id from company_name '{company_name}'. "
                          "Ensure company exists in ASIO.",
                )

            # Validate selected endpoints belong to resolved company context.
            if device_ids and endpoint_company_ids:
                missing_endpoints = [
                    str(eid) for eid in device_ids
                    if not endpoint_company_ids.get(str(eid).lower(), "")
                ]
                wrong_company = [
                    str(eid) for eid in device_ids
                    if endpoint_company_ids.get(str(eid).lower(), "")
                    and endpoint_company_ids.get(str(eid).lower(), "") != company_id
                ]
                if missing_endpoints or wrong_company:
                    return RemediationResult(
                        action,
                        platform,
                        False,
                        detail={
                            "company_id": company_id,
                            "company_name": company_name,
                            "missing_endpoints": missing_endpoints,
                            "wrong_company_endpoints": wrong_company,
                            "endpoint_company_ids_found": {
                                eid: endpoint_company_ids.get(str(eid).lower(), "")
                                for eid in device_ids
                            },
                            "endpoint_company_names_found": {
                                eid: endpoint_company_names.get(str(eid).lower(), "")
                                for eid in device_ids
                            },
                        },
                        error="Endpoint/company validation failed before triggering ESET install."
                    )

            # Step 3: Find ASIO script named exactly "eset web installer".
            script_id = str(kwargs.get("script_id", "") or "").strip()
            script_name = ""
            try:
                scripts = await asio.list_scripts(company_id=company_id)
                if scripts and not script_id:
                    match = next(
                        (
                            s for s in scripts
                            if s.get("name", "").lower().strip() == "eset web installer"
                        ),
                        None,
                    )
                    if match:
                        script_id = str(match.get("id", ""))
                        script_name = str(match.get("name", ""))
                        log.info(
                            "remediation.install.eset_script_found",
                            script_name=script_name,
                            script_id=script_id,
                            company_id=company_id,
                        )
                    else:
                        available = [s.get("name", "") for s in scripts]
                        return RemediationResult(
                            action, platform, False,
                            error=f"No script named 'eset web installer' found for company {company_id}. "
                                  f"Available: {available}",
                        )
                elif script_id:
                    found = next((s for s in scripts if str(s.get("id", "")) == script_id), None)
                    script_name = str(found.get("name", "")) if found else ""
                    if script_name.lower().strip() != "eset web installer":
                        return RemediationResult(
                            action, platform, False,
                            error=f"Provided script_id resolves to '{script_name}', expected 'eset web installer'.",
                        )
            except Exception as exc:  # noqa: BLE001
                return RemediationResult(
                    action, platform, False,
                    error=f"Failed to list scripts for company {company_id}: {str(exc)}",
                )

            if not script_id:
                return RemediationResult(
                    action, platform, False,
                    error="Could not find script 'eset web installer' for this company.",
                )

            # Step 4: Execute the script with installer URL parameters.
            try:
                log.info(
                    "remediation.install.eset_executing_asio_script",
                    script_id=script_id,
                    company_id=company_id,
                    company_name=company_name,
                    device_count=len(device_ids),
                    installer_url=download_url[:80],
                )

                def _parse_iso(ts: str) -> datetime | None:
                    if not ts:
                        return None
                    try:
                        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except Exception:  # noqa: BLE001
                        return None

                triggered_at = datetime.now(UTC)
                result = await asio.run_script(
                    script_id,
                    device_ids,
                    parameters={
                        "INSTALLERURL": download_url,
                    }
                )

                run_payload = result.get("result", {}) if isinstance(result, dict) else {}
                run_task_id = str(
                    run_payload.get("taskID")
                    or run_payload.get("taskId")
                    or run_payload.get("id")
                    or ""
                ).strip()
                result_http_status = result.get("http_status") if isinstance(result, dict) else None
                result_request_id = result.get("request_id") if isinstance(result, dict) else ""
                result_error = result.get("error") if isinstance(result, dict) else ""
                result_response_body = result.get("response_body") if isinstance(result, dict) else ""
                result_run_attempts = result.get("run_attempts") if isinstance(result, dict) else []
                result_run_api_version = result.get("run_api_version") if isinstance(result, dict) else ""
                result_run_ids_field = result.get("run_ids_field") if isinstance(result, dict) else ""
                result_run_script_in_path = result.get("run_script_in_path") if isinstance(result, dict) else None

                # Quick verification only (avoid long request timeouts):
                # observe whether a fresh endpoint task appears within ~12s.
                task_summaries_by_endpoint: dict[str, list[dict[str, Any]]] = {}
                for endpoint_id in device_ids:
                    matched: list[dict[str, Any]] = []
                    for _ in range(3):
                        summaries = await asio.get_endpoint_tasks_summary(endpoint_id)
                        if summaries:
                            for t in summaries:
                                tid = str(t.get("taskID") or t.get("taskId") or "").strip()
                                tname = str(t.get("name") or "").strip().lower()
                                created_on = str(t.get("createdOn") or "")
                                created_dt = _parse_iso(created_on)

                                if run_task_id and tid == run_task_id:
                                    matched.append(t)
                                    continue

                                if not run_task_id:
                                    is_eset_script = tname == "eset web installer"
                                    is_recent = bool(created_dt and created_dt >= triggered_at)
                                    if is_eset_script and is_recent:
                                        matched.append(t)

                            if matched:
                                break
                        await asyncio.sleep(4)

                    if matched:
                        task_summaries_by_endpoint[endpoint_id] = [
                            {
                                "taskID": t.get("taskID") or t.get("taskId"),
                                "taskInstanceID": t.get("taskInstanceID") or t.get("taskInstanceId"),
                                "name": t.get("name"),
                                "state": t.get("state"),
                                "executionStatus": t.get("executionStatus"),
                                "createdOn": t.get("createdOn"),
                                "executedOn": t.get("executedOn"),
                            }
                            for t in matched[:5]
                        ]

                accepted_by_asio = bool(result and not result.get("error"))
                task_observed = len(task_summaries_by_endpoint) > 0

                detail = {
                    "method": "asio_automation_script",
                    "script_id": script_id,
                    "script_name": script_name or "eset web installer",
                    "company_id": company_id,
                    "company_name": company_name,
                    "download_url": download_url,
                    "installer": installer_meta,
                    "devices": len(device_ids),
                    "run_task_id": run_task_id,
                    "accepted_by_asio": accepted_by_asio,
                    "task_observed": task_observed,
                    "task_summaries_by_endpoint": task_summaries_by_endpoint,
                    "http_status": result_http_status,
                    "request_id": result_request_id,
                    "run_error": result_error,
                    "run_response_body": result_response_body,
                    "run_attempts": result_run_attempts,
                    "run_api_version": result_run_api_version,
                    "run_ids_field": result_run_ids_field,
                    "run_script_in_path": result_run_script_in_path,
                    **(result or {}),
                }

                if task_observed:
                    log.info(
                        "remediation.install.eset_via_asio",
                        script_id=script_id,
                        company_id=company_id,
                        company_name=company_name,
                        device_count=len(device_ids),
                        run_task_id=run_task_id,
                        endpoints_with_tasks=len(task_summaries_by_endpoint),
                    )
                    return RemediationResult(action, platform, True, detail)

                # If ASIO accepted the run call, treat this as accepted/pending
                # instead of a hard failure. Task summary visibility can lag.
                if accepted_by_asio:
                    detail["pending_verification"] = True
                    return RemediationResult(
                        action,
                        platform,
                        True,
                        detail,
                        "ASIO accepted run request; endpoint task not yet visible in quick verification window.",
                    )

                rejection_parts: list[str] = []
                if result_http_status:
                    rejection_parts.append(f"http={result_http_status}")
                if result_request_id:
                    rejection_parts.append(f"requestId={result_request_id}")
                if result_error:
                    rejection_parts.append(f"error={result_error}")
                if result_response_body:
                    rejection_parts.append(f"response={str(result_response_body)[:180]}")
                if result_run_api_version:
                    rejection_parts.append(f"runApi={result_run_api_version}")
                if result_run_ids_field:
                    rejection_parts.append(f"idsField={result_run_ids_field}")
                if isinstance(result_run_attempts, list) and result_run_attempts:
                    rejection_parts.append(f"attemptVariants={len(result_run_attempts)}")
                if result_run_script_in_path is True:
                    rejection_parts.append("scriptSelector=path")
                elif result_run_script_in_path is False:
                    rejection_parts.append("scriptSelector=body")

                if not rejection_parts:
                    result_type = type(result).__name__
                    rejection_parts.append(f"connectorResultType={result_type}")
                    if isinstance(result, dict):
                        rejection_parts.append(f"connectorKeys={','.join(sorted(result.keys()))}")
                    elif result is None:
                        rejection_parts.append("connectorResult=none")

                rejection_suffix = f" ({'; '.join(rejection_parts)})" if rejection_parts else ""
                diag_marker = "diag-eset-run-accept-v2"
                return RemediationResult(
                    action,
                    platform,
                    False,
                    detail,
                    f"ASIO run request was not accepted [{diag_marker}]{rejection_suffix}.",
                )
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "remediation.install.eset_asio_execution_failed",
                    script_id=script_id,
                    company_id=company_id,
                    error=str(exc),
                )
                return RemediationResult(
                    action, platform, False,
                    error=f"ASIO script execution failed: {str(exc)}",
                )

        # ── Other platforms ──
        if via_screenconnect:
            sc = _find_connector_strict("screenconnect")
            await sc.authenticate()
            result = await sc.install_agent(
                device_ids, company_id=company_id,
                agent_type=platform, **kwargs,
            )
        else:
            connector = _find_connector_strict(platform)
            await connector.authenticate()
            result = await connector.install_agent(
                device_ids, company_id=company_id, **kwargs,
            )

        if result and result.get("error"):
            return RemediationResult(action, platform, False, result, result["error"])

        log.info("remediation.install", platform=platform, devices=len(device_ids))
        return RemediationResult(action, platform, True, result or {})

    except Exception as exc:  # noqa: BLE001
        log.error("remediation.install.error", platform=platform, error=str(exc))
        return RemediationResult(action, platform, False, error=str(exc))


async def uninstall_agent(
    platform: str,
    device_ids: list[str],
    *,
    via_screenconnect: bool = False,
    **kwargs: Any,
) -> RemediationResult:
    """Uninstall / remove an agent from target devices."""
    action = f"uninstall_{platform}"
    try:
        if via_screenconnect:
            sc = _find_connector_strict("screenconnect")
            await sc.authenticate()
            result = await sc.uninstall_agent(
                device_ids, agent_type=platform, **kwargs,
            )
        else:
            connector = _find_connector_strict(platform)
            await connector.authenticate()
            result = await connector.uninstall_agent(device_ids, **kwargs)

        if result and result.get("error"):
            return RemediationResult(action, platform, False, result, result["error"])

        log.info("remediation.uninstall", platform=platform, devices=len(device_ids))
        return RemediationResult(action, platform, True, result or {})

    except Exception as exc:  # noqa: BLE001
        log.error("remediation.uninstall.error", platform=platform, error=str(exc))
        return RemediationResult(action, platform, False, error=str(exc))


async def repair_agent(
    platform: str,
    device_ids: list[str],
    **kwargs: Any,
) -> RemediationResult:
    """Repair / reinstall an agent on target devices.

    For **ScreenConnect** repair, the real MSP workflow uses an Asio
    automation script (default name from ``MSP_SC_REPAIR_SCRIPT``) that
    reinstalls the agent into the correct company context.  A generic
    PowerShell MSI reinstall does NOT preserve company assignment.

    Flow for ScreenConnect:
    1. Look up the Asio automation script by name (or use ``script_id``).
    2. Run the script on the target endpoints via Asio ``run_script()``.

    For other platforms, falls back to the platform connector's own
    ``repair_agent()`` method.
    """
    action = f"repair_{platform}"
    try:
        # ── ScreenConnect repair via the Asio repair script (MSP_SC_REPAIR_SCRIPT) ──
        if platform == "screenconnect":
            company_id = kwargs.get("company_id", "")
            script_id = kwargs.get("script_id", "")
            script_name = (kwargs.get("script_name", "") or SC_REPAIR_SCRIPT).strip()
            if not script_id and not script_name:
                # An empty name would substring-match every Asio script — refuse instead.
                return RemediationResult(
                    action, platform, False,
                    error="No ScreenConnect repair script configured — set MSP_SC_REPAIR_SCRIPT "
                          "or pass script_name / script_id",
                )

            asio = _find_connector("asio")
            if asio is None:
                return RemediationResult(
                    action, platform, False,
                    error="Asio connector not configured — required for ScreenConnect repair "
                          "(the repair script runs as an Asio automation script)",
                )

            await asio.authenticate()

            # Resolve script ID by name if not provided directly
            if not script_id:
                scripts = await asio.list_scripts(company_id=company_id or None)
                if not scripts:
                    return RemediationResult(
                        action, platform, False,
                        error="No Asio automation scripts found. Ensure 'platform.automation.read' "
                              "scope is configured and scripts exist.",
                    )
                match = next(
                    (s for s in scripts
                     if script_name.lower() in s.get("name", "").lower()),
                    None,
                )
                if match is None:
                    available = [s.get("name", "") for s in scripts[:10]]
                    return RemediationResult(
                        action, platform, False,
                        detail={"available_scripts": available},
                        error=f"Script '{script_name}' not found in Asio. "
                              f"Available: {available}",
                    )
                script_id = match["id"]
                log.info(
                    "remediation.repair.sc_script_found",
                    script_name=match.get("name"),
                    script_id=script_id,
                )

            result = await asio.run_script(script_id, device_ids)
            if result and result.get("error"):
                return RemediationResult(action, platform, False, result, result["error"])

            log.info(
                "remediation.repair.screenconnect_via_asio",
                script_id=script_id,
                devices=len(device_ids),
            )
            return RemediationResult(action, platform, True, {
                "method": "asio_automation_script",
                "script_name": script_name,
                "script_id": script_id,
                **(result or {}),
            })

        # ── Other platforms: use connector's own repair_agent() ──
        connector = _find_connector_strict(platform)
        await connector.authenticate()
        result = await connector.repair_agent(device_ids, **kwargs)

        if result is None:
            return RemediationResult(
                action, platform, False,
                error=f"{platform} does not support repair",
            )
        if result.get("error"):
            return RemediationResult(action, platform, False, result, result["error"])

        log.info("remediation.repair", platform=platform, devices=len(device_ids))
        return RemediationResult(action, platform, True, result)

    except Exception as exc:  # noqa: BLE001
        log.error("remediation.repair.error", platform=platform, error=str(exc))
        return RemediationResult(action, platform, False, error=str(exc))


async def offboard_device(
    device_ids: list[str],
    platforms: list[str] | None = None,
    **kwargs: Any,
) -> list[RemediationResult]:
    """Offboard devices — real MSP workflow.

    Multi-step process:
    1. **Asio offboard** (max 5 devices per batch) — removes SentinelOne +
       ScreenConnect via the Asio uninstall automation.
    2. **ESET removal** (separate) — searches endpoints for ESET products
       via ``get_endpoint_applications``, then triggers Asio uninstall script
       or flags for manual removal.
    3. **Other platforms** — fallback loop for any connector with
       ``supports_offboard`` (e.g. SentinelOne direct API).

    Args:
        device_ids: Endpoint IDs to offboard (max 5 enforced by API model).
        platforms: Optional filter — only offboard from these platforms.
        **kwargs: Extra args; ``company_id`` required for Asio/ESET steps,
                  ``include_eset`` (bool) to trigger ESET removal step.
    """
    results: list[RemediationResult] = []
    connectors = get_connectors()
    company_id: str = kwargs.get("company_id", "")
    include_eset: bool = kwargs.get("include_eset", False)

    # ------------------------------------------------------------------
    # Step 1: Asio offboard — removes S1 + ScreenConnect
    # ------------------------------------------------------------------
    asio = _find_connector("asio", connectors)
    if asio and (not platforms or "asio" in platforms):
        action = "offboard_asio"
        try:
            await asio.authenticate()

            # Asio accepts max 5 devices per trigger — batch accordingly
            batch_size = 5
            for i in range(0, len(device_ids), batch_size):
                batch = device_ids[i : i + batch_size]
                log.info(
                    "remediation.offboard.asio_batch",
                    batch=len(batch),
                    offset=i,
                    total=len(device_ids),
                    company_id=company_id,
                )
                # Use run_script if company_id provided, otherwise
                # fall back to connector offboard_device
                if company_id and hasattr(asio, "list_scripts"):
                    # Look for an uninstall / offboard script
                    scripts = await asio.list_scripts(company_id)
                    offboard_script = None
                    for s in (scripts or []):
                        sname = (s.get("name") or s.get("Name") or "").lower()
                        if any(kw in sname for kw in ("uninstall", "offboard", "remove")):
                            offboard_script = s
                            break

                    if offboard_script:
                        script_id = str(
                            offboard_script.get("id")
                            or offboard_script.get("Id")
                            or offboard_script.get("scriptId")
                            or ""
                        )
                        result = await asio.run_script(
                            script_id=script_id,
                            endpoint_ids=batch,
                        )
                        results.append(RemediationResult(
                            action, "asio", True,
                            {
                                "batch": batch,
                                "script": offboard_script.get("name"),
                                "result": result,
                            },
                        ))
                    else:
                        results.append(RemediationResult(
                            action, "asio", False,
                            {"available_scripts": [s.get("name") for s in (scripts or [])]},
                            error=(
                                "No uninstall/offboard script found for this company. "
                                "Available scripts: "
                                + ", ".join(s.get("name", "?") for s in (scripts or []))
                            ),
                        ))
                else:
                    # Direct connector offboard (no company_id)
                    result = await asio.offboard_device(batch)
                    if result and result.get("error"):
                        results.append(RemediationResult(
                            action, "asio", False, result, result["error"],
                        ))
                    else:
                        results.append(RemediationResult(
                            action, "asio", True,
                            {"batch": batch, "result": result or {}},
                        ))

        except Exception as exc:  # noqa: BLE001
            log.error("remediation.offboard.asio_error", error=str(exc))
            results.append(RemediationResult(
                action, "asio", False, error=str(exc),
            ))

    # ------------------------------------------------------------------
    # Step 2: ESET removal (separate from Asio offboard)
    # ------------------------------------------------------------------
    if include_eset and company_id:
        action = "offboard_eset"
        try:
            # Use Asio to search for ESET products on the endpoints
            if asio and hasattr(asio, "get_endpoint_applications"):
                await asio.authenticate()
                eset_found: list[dict[str, Any]] = []
                for eid in device_ids:
                    apps = await asio.get_endpoint_applications(
                        endpoint_id=eid,
                        app_name_filter="ESET",
                    )
                    if apps:
                        eset_found.append({"endpoint_id": eid, "apps": apps})

                if eset_found:
                    # Look for an ESET uninstall script in Asio
                    scripts = await asio.list_scripts(company_id)
                    eset_script = None
                    for s in (scripts or []):
                        sname = (s.get("name") or s.get("Name") or "").lower()
                        if "eset" in sname and any(
                            kw in sname for kw in ("uninstall", "remove")
                        ):
                            eset_script = s
                            break

                    if eset_script:
                        script_id = str(
                            eset_script.get("id")
                            or eset_script.get("Id")
                            or eset_script.get("scriptId")
                            or ""
                        )
                        result = await asio.run_script(
                            script_id=script_id,
                            endpoint_ids=device_ids,
                        )
                        results.append(RemediationResult(
                            action, "eset", True,
                            {
                                "eset_endpoints": eset_found,
                                "script": eset_script.get("name"),
                                "result": result,
                            },
                        ))
                    else:
                        # No ESET uninstall script — flag for manual removal
                        results.append(RemediationResult(
                            action, "eset", False,
                            {
                                "eset_endpoints": eset_found,
                                "available_scripts": [
                                    s.get("name") for s in (scripts or [])
                                ],
                            },
                            error=(
                                "ESET found on endpoints but no ESET uninstall script "
                                "available. Manual removal required via ESET console."
                            ),
                        ))
                else:
                    results.append(RemediationResult(
                        action, "eset", True,
                        {"detail": "No ESET products found on specified endpoints"},
                    ))
            else:
                results.append(RemediationResult(
                    action, "eset", False,
                    error="Asio connector unavailable — cannot search for ESET products",
                ))

        except Exception as exc:  # noqa: BLE001
            log.error("remediation.offboard.eset_error", error=str(exc))
            results.append(RemediationResult(
                action, "eset", False, error=str(exc),
            ))

    # ------------------------------------------------------------------
    # Step 3: Other platforms with supports_offboard (e.g. SentinelOne)
    # ------------------------------------------------------------------
    for connector in connectors:
        # Skip Asio (handled above) and ESET (handled above)
        if connector.platform_name in ("asio", "eset"):
            continue
        if platforms and connector.platform_name not in platforms:
            continue
        if not connector.supports_offboard:
            continue
        action = f"offboard_{connector.platform_name}"
        try:
            await connector.authenticate()
            result = await connector.offboard_device(device_ids, **kwargs)
            if result and result.get("error"):
                results.append(RemediationResult(
                    action, connector.platform_name, False, result, result["error"],
                ))
            else:
                results.append(RemediationResult(
                    action, connector.platform_name, True, result or {},
                ))
                log.info(
                    "remediation.offboard",
                    platform=connector.platform_name,
                    devices=len(device_ids),
                )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "remediation.offboard.error",
                platform=connector.platform_name,
                error=str(exc),
            )
            results.append(RemediationResult(
                action, connector.platform_name, False, error=str(exc),
            ))

    return results


async def create_company(
    name: str,
    platforms: list[str] | None = None,
    **kwargs: Any,
) -> list[RemediationResult]:
    """Create a company across multiple platforms simultaneously.

    If ``platforms`` is None, creates on all platforms that support it.
    This ensures uniform naming across S1, ESET, Asio, CW PSA, etc.
    """
    results: list[RemediationResult] = []
    connectors = get_connectors()

    for connector in connectors:
        if platforms and connector.platform_name not in platforms:
            continue
        if not connector.supports_company_create:
            continue
        action = f"create_company_{connector.platform_name}"
        try:
            await connector.authenticate()
            result = await connector.create_company_on_platform(name, **kwargs)
            if result and result.get("error"):
                results.append(RemediationResult(
                    action, connector.platform_name, False, result, result["error"],
                ))
            else:
                results.append(RemediationResult(
                    action, connector.platform_name, True, result or {},
                ))
                log.info(
                    "remediation.create_company",
                    platform=connector.platform_name,
                    name=name,
                )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "remediation.create_company.error",
                platform=connector.platform_name,
                error=str(exc),
            )
            results.append(RemediationResult(
                action, connector.platform_name, False, error=str(exc),
            ))

    return results


async def list_platform_scripts(
    platform: str,
    *,
    company_id: str | None = None,
) -> list[dict[str, Any]]:
    """List available automation scripts for a platform (e.g. Asio)."""
    connector = _find_connector_strict(platform)
    await connector.authenticate()
    scripts = await connector.list_scripts(company_id=company_id)
    return scripts or []


async def run_platform_script(
    platform: str,
    script_id: str,
    device_ids: list[str],
    **kwargs: Any,
) -> RemediationResult:
    """Execute a specific automation script on target devices."""
    action = f"run_script_{platform}"
    try:
        connector = _find_connector_strict(platform)
        await connector.authenticate()
        result = await connector.run_script(script_id, device_ids, **kwargs)

        if result and result.get("error"):
            return RemediationResult(action, platform, False, result, result["error"])

        log.info("remediation.run_script", platform=platform, script_id=script_id)
        return RemediationResult(action, platform, True, result or {})

    except Exception as exc:  # noqa: BLE001
        log.error("remediation.run_script.error", platform=platform, error=str(exc))
        return RemediationResult(action, platform, False, error=str(exc))


async def get_remediation_capabilities() -> list[dict[str, Any]]:
    """Return each connector's remediation capabilities."""
    return [
        {
            "platform": c.platform_name,
            "capabilities": c.capabilities(),
            "configured": len(c.check_config()) == 0,
            "supports_install": c.supports_install,
            "supports_uninstall": c.supports_uninstall,
            "supports_repair": c.supports_repair,
            "supports_offboard": c.supports_offboard,
            "supports_company_create": c.supports_company_create,
        }
        for c in get_connectors()
    ]


# ---------------------------------------------------------------------------
# ASIO site & installer helpers
# ---------------------------------------------------------------------------

async def get_asio_sites(company_id: str) -> list[dict[str, Any]]:
    """Return all sites for an ASIO company."""
    asio = _find_connector_strict("asio")
    await asio.authenticate()
    return await asio.get_sites(company_id)


async def get_asio_installer_info(
    company_id: str,
    site_id: str | None = None,
) -> dict[str, Any]:
    """Return ASIO agent installer details (URL, token, filename) for a company/site."""
    asio = _find_connector_strict("asio")
    await asio.authenticate()
    info = await asio.get_agent_installer_info(company_id, site_id)
    return info or {"error": "No installer info returned"}


async def get_asio_endpoint_tasks_summary(endpoint_id: str) -> list[dict[str, Any]]:
    """Return task summary for one ASIO endpoint."""
    asio = _find_connector_strict("asio")
    await asio.authenticate()
    return await asio.get_endpoint_tasks_summary(endpoint_id)


# ---------------------------------------------------------------------------
# ESET Live Installer helpers
# ---------------------------------------------------------------------------

async def get_eset_installers(
    company_name: str = "",
) -> list[dict[str, Any]]:
    """Return ESET Live Installer packages from the ESET Connect API.

    Each entry is a simplified installer record:
      - ``group_name`` — the ESET device-group / company it enrolls into
      - ``display_name`` — installer name shown in the Protect console
      - ``download_url`` — the Live Installer link (redirector.eset.systems)
      - ``usable`` — whether ESET considers the installer valid
      - ``expire_time`` / ``create_time`` — validity window
      - ``products`` — activated product display names

    If ``company_name`` is given, results are filtered to groups whose
    name contains it (case-insensitive) or vice versa.
    """
    from app.connectors.eset_connect import EsetConnectConnector

    connector = EsetConnectConnector()
    missing = connector.check_config()
    if missing:
        raise ValueError(
            f"ESET Connect not configured — missing: {', '.join(missing)}"
        )

    raw = await connector.get_installers()
    results: list[dict[str, Any]] = []
    needle = company_name.strip().lower()

    for inst in raw:
        enrollment = inst.get("deviceEnrollment") or {}
        group = enrollment.get("groupDisplayName", "")
        if needle:
            hay = group.strip().lower()
            if not hay or (needle not in hay and hay not in needle):
                continue
        results.append({
            "uuid": inst.get("uuid", ""),
            "group_name": group,
            "security_group_uuid": enrollment.get("securityGroupUuid", ""),
            "display_name": inst.get("displayName", ""),
            "download_url": inst.get("downloadUrl", ""),
            "usable": bool(inst.get("usable")),
            "expire_time": inst.get("expireTime", ""),
            "create_time": inst.get("createTime", ""),
            "products": [
                p.get("productDisplayName", "")
                for p in inst.get("activatedProducts", [])
            ],
        })

    # Newest first, then stable-sort so usable installers with a URL rank first
    results.sort(key=lambda i: i.get("create_time", ""), reverse=True)
    results.sort(key=lambda i: not (i["usable"] and i["download_url"]))
    return results
