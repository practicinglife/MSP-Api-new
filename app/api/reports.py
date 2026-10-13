"""Report generation endpoints — DB-first, multi-format.

Routes
------
``GET /api/reports/company/{name}`` — company device report
    Query params:
    - ``format``: ``pdf`` | ``html`` | ``json`` (default ``pdf``)
    - ``save``: persist a copy under ``/reports/`` (default ``false``)

The report data comes from the existing reconciled-from-DB endpoint, so
it returns in well under a second without touching any platform API.
"""

from __future__ import annotations

import json as _json
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.companies import _get_session, company_reconciled
from app.api.db_query import db_company as _db_company
from app.services.reports import (
    build_company_report_context,
    find_company,
    render_html,
    render_pdf,
    report_filename,
    reports_dir,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/company/{name}", summary="Generate a per-company device report")
async def company_report(
    name: str,
    format: str = Query("pdf", pattern="^(pdf|html|json)$", description="Output format"),
    save: bool = Query(False, description="Persist a copy under /reports/"),
    session: AsyncSession = Depends(_get_session),
) -> Response:
    """Build a cross-platform device report for the named company.

    Reads from the local ``platform_devices`` table via ``company_reconciled`` —
    no upstream API calls. If the company has no data the response is 404.
    """
    # Pull directly from platform_devices table — no live API calls, sub-20ms
    db_data = await _db_company(name=name, session=session)
    if not db_data.get("found"):
        raise HTTPException(status_code=404, detail=f"Company '{name}' not found in DB. Run POST /api/reconcile first.")

    # Use pre-split matched/unmatched directly from db_company
    # mismatched must be {platform: [devices]} for build_company_report_context
    unmatched_flat = db_data.get("unmatched", [])
    mismatched_buckets: dict[str, list] = {}
    for dev in unmatched_flat:
        plats = dev.get("platforms", [])
        key = plats[0] if plats else "unknown"
        mismatched_buckets.setdefault(key, []).append(dev)

    company_data = {
        "company": db_data.get("company", name),
        "matched": db_data.get("matched", []),
        "mismatched": mismatched_buckets,
        "psa_present": "connectwise_psa" in db_data.get("platforms_present", []),
        "assigned_av": db_data.get("assigned_av", ""),
    }
    context = build_company_report_context(company_data)
    log.info(
        "report.generated",
        company=context["company_name"],
        format=format,
        matched=context["matched_count"],
        mismatched=context["mismatched_count"],
    )

    if format == "json":
        body = _json.dumps(context, indent=2, default=str).encode("utf-8")
        if save:
            path = reports_dir() / report_filename(context["company_name"], "json")
            path.write_bytes(body)
        return JSONResponse(content=context)

    html = render_html(context)
    if format == "html":
        if save:
            path = reports_dir() / report_filename(context["company_name"], "html")
            path.write_text(html, encoding="utf-8")
        return HTMLResponse(content=html)

    # PDF (default)
    try:
        pdf_bytes = render_pdf(html)
    except Exception as exc:  # noqa: BLE001
        log.error("report.pdf_failed", error=str(exc))
        raise HTTPException(
            status_code=500,
            detail=f"PDF rendering failed (is WeasyPrint installed?): {exc}",
        )

    if save:
        path = reports_dir() / report_filename(context["company_name"], "pdf")
        path.write_bytes(pdf_bytes)

    filename = report_filename(context["company_name"], "pdf")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/all", summary="Generate reports for all companies")
async def all_companies_report(
    format: str = Query("json", pattern="^(pdf|html|json)$", description="Output format"),
    save: bool = Query(True, description="Persist a copy under /reports/ for each company"),
    mismatched_only: bool = Query(False, description="Only include companies with mismatched devices"),
    session: AsyncSession = Depends(_get_session),
) -> Response:
    """Build a report for every company in the local DB.

    - ``format=json``  — returns a JSON summary array of all companies inline.
    - ``format=pdf``   — saves one PDF per company to ``/reports/`` and returns
                         a manifest of the generated files.
    - ``format=html``  — saves one HTML file per company and returns a manifest.

    Use ``mismatched_only=true`` to skip fully-matched companies.
    """
    reconciled = await company_reconciled(session=session)

    if mismatched_only:
        reconciled = [c for c in reconciled if (c.get("mismatched_count") or 0) > 0]

    if not reconciled:
        raise HTTPException(status_code=404, detail="No company data in DB. Run reconciliation first.")

    if format == "json":
        # Lightweight summary — full context per company would be huge
        summary = [
            {
                "company": c.get("company"),
                "matched_count": c.get("matched_count", 0),
                "mismatched_count": c.get("mismatched_count", 0),
                "assigned_av": c.get("assigned_av", "none"),
                "psa_present": c.get("psa_present", False),
                "report_url": f"/api/reports/company/{c.get('company', '').replace(' ', '%20')}?format=json",
            }
            for c in reconciled
        ]
        return JSONResponse(content={
            "total_companies": len(summary),
            "mismatched_only": mismatched_only,
            "companies": summary,
            "note": "Call /api/reports/company/{name}?format=pdf to generate a full PDF for any company.",
        })

    # PDF / HTML — generate and save one file per company, return manifest
    generated: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    rdir = reports_dir()

    for company_data in reconciled:
        cname = company_data.get("company", "unknown")
        try:
            context = build_company_report_context(company_data)
            html = render_html(context)
            fname = report_filename(context["company_name"], format)
            fpath = rdir / fname

            if format == "html":
                fpath.write_text(html, encoding="utf-8")
            else:
                pdf_bytes = render_pdf(html)
                fpath.write_bytes(pdf_bytes)

            generated.append({
                "company": cname,
                "filename": fname,
                "url": f"/reports/{fname}",
                "size_bytes": fpath.stat().st_size,
                "matched_count": context["matched_count"],
                "mismatched_count": context["mismatched_count"],
            })
            log.info("report.bulk.generated", company=cname, format=format)
        except Exception as exc:  # noqa: BLE001
            log.warning("report.bulk.failed", company=cname, error=str(exc))
            errors.append({"company": cname, "error": str(exc)})

    manifest = {
        "total_companies": len(reconciled),
        "generated": len(generated),
        "failed": len(errors),
        "format": format,
        "mismatched_only": mismatched_only,
        "reports": generated,
        "errors": errors,
        "note": f"All {format.upper()} files saved to /reports/. Access via the url fields.",
    }
    return JSONResponse(content=manifest)


@router.get("/saved", summary="List previously saved reports")
async def list_saved_reports() -> list[dict[str, Any]]:
    """Return metadata for all previously persisted reports."""
    rdir = reports_dir()
    return [
        {
            "filename": p.name,
            "size_bytes": p.stat().st_size,
            "modified_at": p.stat().st_mtime,
            "url": f"/reports/{p.name}",
        }
        for p in sorted(rdir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)
        if p.is_file()
    ]
