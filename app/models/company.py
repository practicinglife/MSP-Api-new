"""Company record Pydantic model."""

from pydantic import BaseModel


class CompanyRecord(BaseModel):
    """Normalised company record returned by any platform connector."""

    platform: str
    platform_id: str
    name: str
    raw: dict = {}  # type: ignore[type-arg]
