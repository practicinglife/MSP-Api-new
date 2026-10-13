"""Overrides admin API router."""

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository import OverrideRepository, get_session_factory

router = APIRouter(prefix="/admin/overrides", tags=["overrides"])


async def _get_session() -> AsyncSession:  # type: ignore[return]
    """Dependency that yields a database session."""
    factory = get_session_factory()
    async with factory() as session:
        yield session  # type: ignore[misc]


class CompanyOverrideCreate(BaseModel):
    """Request body for creating a company override."""

    platform_a: str
    platform_id_a: str
    platform_b: str
    platform_id_b: str
    note: str | None = None


class DeviceOverrideCreate(BaseModel):
    """Request body for creating a device override."""

    platform_a: str
    platform_id_a: str
    platform_b: str
    platform_id_b: str
    note: str | None = None


@router.post("/companies", response_model=dict[str, Any], status_code=201)
async def create_company_override(
    body: CompanyOverrideCreate,
    session: AsyncSession = Depends(_get_session),
) -> dict[str, Any]:
    """Create a manual company link override."""
    repo = OverrideRepository(session)
    obj = await repo.create_company_override(
        platform_a=body.platform_a,
        platform_id_a=body.platform_id_a,
        platform_b=body.platform_b,
        platform_id_b=body.platform_id_b,
        note=body.note,
    )
    return {
        "id": obj.id,
        "platform_a": obj.platform_a,
        "platform_id_a": obj.platform_id_a,
        "platform_b": obj.platform_b,
        "platform_id_b": obj.platform_id_b,
        "note": obj.note,
    }


@router.get("/companies", response_model=list[dict[str, Any]])
async def list_company_overrides(
    session: AsyncSession = Depends(_get_session),
) -> list[dict[str, Any]]:
    """Return all company overrides."""
    repo = OverrideRepository(session)
    overrides = await repo.list_company_overrides()
    return [
        {
            "id": o.id,
            "platform_a": o.platform_a,
            "platform_id_a": o.platform_id_a,
            "platform_b": o.platform_b,
            "platform_id_b": o.platform_id_b,
            "note": o.note,
        }
        for o in overrides
    ]


@router.post("/devices", response_model=dict[str, Any], status_code=201)
async def create_device_override(
    body: DeviceOverrideCreate,
    session: AsyncSession = Depends(_get_session),
) -> dict[str, Any]:
    """Create a manual device link override."""
    repo = OverrideRepository(session)
    obj = await repo.create_device_override(
        platform_a=body.platform_a,
        platform_id_a=body.platform_id_a,
        platform_b=body.platform_b,
        platform_id_b=body.platform_id_b,
        note=body.note,
    )
    return {
        "id": obj.id,
        "platform_a": obj.platform_a,
        "platform_id_a": obj.platform_id_a,
        "platform_b": obj.platform_b,
        "platform_id_b": obj.platform_id_b,
        "note": obj.note,
    }


@router.get("/devices", response_model=list[dict[str, Any]])
async def list_device_overrides(
    session: AsyncSession = Depends(_get_session),
) -> list[dict[str, Any]]:
    """Return all device overrides."""
    repo = OverrideRepository(session)
    overrides = await repo.list_device_overrides()
    return [
        {
            "id": o.id,
            "platform_a": o.platform_a,
            "platform_id_a": o.platform_id_a,
            "platform_b": o.platform_b,
            "platform_id_b": o.platform_id_b,
            "note": o.note,
        }
        for o in overrides
    ]
