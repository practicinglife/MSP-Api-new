"""Device record Pydantic model."""


from pydantic import BaseModel


class DeviceRecord(BaseModel):
    """Normalised device record returned by any platform connector."""

    platform: str
    platform_id: str
    hostname: str
    serial_number: str = ""
    mac_addresses: list[str] = []
    company_platform_id: str = ""
    company_name: str = ""
    site_id: str = ""
    site_name: str = ""
    endpoint_id: str = ""
    raw: dict = {}  # type: ignore[type-arg]
