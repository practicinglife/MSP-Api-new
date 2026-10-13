"""Application configuration loaded from environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All platform credentials and configuration loaded from environment / .env file.

    Never set real defaults for secrets — use empty strings as placeholders only.
    """

    # Avast Business Hub
    avast_client_id: str = ""
    avast_client_secret: str = ""

    # ESET MSP Portal
    eset_username: str = ""
    eset_password: str = ""
    eset_base_url: str = "https://mspapi.eset.com"

    # ESET Connect (ESET Protect Cloud API — per-device details)
    eset_connect_username: str = ""
    eset_connect_password: str = ""
    eset_connect_base_url: str = "https://us.device-management.eset.systems"
    eset_connect_auth_url: str = "https://us.business-account.iam.eset.systems"

    # ConnectWise PSA
    cw_company_id: str = ""
    cw_public_key: str = ""
    cw_private_key: str = ""
    cw_client_id: str = ""
    cw_base_url: str = "https://na.myconnectwise.net/v4_6_release/apis/3.0"

    # ScreenConnect / ConnectWise Control
    screenconnect_base_url: str = ""
    screenconnect_auth_token: str = ""  # RESTfulAuthenticationSecret from the extension settings
    screenconnect_extension_id: str = "2d558935-686a-4bd0-9991-07539f5fe749"

    # SentinelOne
    s1_base_url: str = ""
    s1_api_token: str = ""

    # Asio (ConnectWise Asio)
    asio_client_id: str = ""
    asio_client_secret: str = ""
    asio_region: str = "NA"
    asio_base_url: str = ""
    asio_token_url: str = ""
    asio_scope: str = "platform.companies.read platform.devices.read platform.agent.read platform.agent.delete platform.asset.read platform.sites.read platform.tickets.read platform.tickets.create platform.tickets.update platform.automation.read platform.automation.create security.siem.read"

    # PostgreSQL
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/msp_tools"

    # Confirmation gating for destructive actions (AI-agent safety).
    # Optional dedicated secret used to sign one-time confirm tokens. If left
    # blank a stable secret is derived from the configured DB URL so tokens
    # cannot be forged. Set CONFIRM_SECRET in production for clean rotation.
    confirm_secret: str = ""
    confirm_token_ttl_seconds: int = 300

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
