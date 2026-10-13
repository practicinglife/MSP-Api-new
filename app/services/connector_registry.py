"""Connector registry — builds connector instances from DB-stored configurations."""

from typing import Any

import structlog

from app.config import Settings, get_settings
from app.connectors.asio import AsioConnector
from app.connectors.avast import AvastConnector
from app.connectors.base import AbstractPlatformConnector
from app.connectors.connectwise_psa import ConnectWisePsaConnector
from app.connectors.eset import EsetConnector
from app.connectors.eset_connect import EsetConnectConnector
from app.connectors.generic_rest import GenericRestConnector
from app.connectors.screenconnect import ScreenConnectConnector
from app.connectors.sentinelone import SentinelOneConnector

log = structlog.get_logger(__name__)

# Maps platform name → required credential field definitions for the UI
PLATFORM_DEFINITIONS: dict[str, dict[str, Any]] = {
    "avast": {
        "label": "Avast Business Hub",
        "fields": [
            {"key": "client_id", "label": "Client ID", "type": "text", "required": True},
            {"key": "client_secret", "label": "Client Secret", "type": "password", "required": True},
        ],
    },
    "eset": {
        "label": "ESET MSP Portal",
        "fields": [
            {"key": "username", "label": "Username", "type": "text", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True},
            {"key": "base_url", "label": "Base URL", "type": "text", "required": False,
             "default": "https://mspapi.eset.com"},
        ],
    },
    "eset_connect": {
        "label": "ESET Connect (Protect Cloud)",
        "fields": [
            {"key": "username", "label": "Username", "type": "text", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True},
            {"key": "base_url", "label": "Device Management URL", "type": "text", "required": False,
             "default": "https://us.device-management.eset.systems"},
            {"key": "auth_url", "label": "Auth URL", "type": "text", "required": False,
             "default": "https://us.business-account.iam.eset.systems"},
        ],
    },
    "connectwise_psa": {
        "label": "ConnectWise PSA",
        "fields": [
            {"key": "company_id", "label": "Company ID", "type": "text", "required": True},
            {"key": "public_key", "label": "Public Key", "type": "text", "required": True},
            {"key": "private_key", "label": "Private Key", "type": "password", "required": True},
            {"key": "client_id", "label": "Client ID", "type": "text", "required": True},
            {"key": "base_url", "label": "Base URL", "type": "text", "required": False,
             "default": "https://na.myconnectwise.net/v4_6_release/apis/3.0"},
        ],
    },
    "screenconnect": {
        "label": "ScreenConnect / CW Control",
        "fields": [
            {"key": "base_url", "label": "Instance URL", "type": "text", "required": True},
            {"key": "auth_token", "label": "RESTful Auth Secret", "type": "password", "required": True},
            {"key": "extension_id", "label": "REST Extension GUID", "type": "text", "required": False,
             "default": "2d558935-686a-4bd0-9991-07539f5fe749"},
        ],
    },
    "sentinelone": {
        "label": "SentinelOne",
        "fields": [
            {"key": "base_url", "label": "Base URL", "type": "text", "required": True},
            {"key": "api_token", "label": "API Token", "type": "password", "required": True},
        ],
    },
    "asio": {
        "label": "ConnectWise Asio",
        "fields": [
            {"key": "client_id", "label": "Client ID", "type": "text", "required": True},
            {"key": "client_secret", "label": "Client Secret", "type": "password", "required": True},
            {"key": "base_url", "label": "API Base URL", "type": "text", "required": False,
             "default": ""},
            {"key": "token_url", "label": "Token URL", "type": "text", "required": False,
             "default": ""},
            {"key": "scope", "label": "OAuth Scope", "type": "text", "required": False,
             "default": "platform.companies.read platform.devices.read platform.agent.read platform.asset.read platform.sites.read platform.tickets.read platform.tickets.create platform.tickets.update platform.automation.read platform.automation.create security.siem.read"},
            {"key": "region", "label": "Region (NA/EU/AU)", "type": "text", "required": False,
             "default": "NA"},
        ],
    },
    "generic_rest": {
        "label": "Generic REST API",
        "fields": [
            {"key": "platform_label", "label": "Platform Name (unique identifier)", "type": "text", "required": True},
            {"key": "base_url", "label": "Base URL", "type": "text", "required": True},
            {"key": "auth_method", "label": "Authentication Method", "type": "select", "required": True,
             "default": "bearer",
             "options": [
                 {"value": "none", "label": "None (No Auth)"},
                 {"value": "api_key", "label": "API Key (Custom Header)"},
                 {"value": "basic", "label": "Basic Auth (Username / Password)"},
                 {"value": "bearer", "label": "Bearer Token"},
                 {"value": "custom_header", "label": "Custom Header"},
             ]},
            {"key": "auth_header_name", "label": "Auth Header Name", "type": "text", "required": False,
             "default": "X-API-Key", "show_when_auth": ["api_key", "custom_header"]},
            {"key": "auth_header_value", "label": "Auth Header Value / API Key", "type": "password", "required": False,
             "show_when_auth": ["api_key", "custom_header"]},
            {"key": "auth_username", "label": "Username", "type": "text", "required": False,
             "show_when_auth": ["basic"]},
            {"key": "auth_password", "label": "Password", "type": "password", "required": False,
             "show_when_auth": ["basic"]},
            {"key": "auth_token", "label": "Bearer Token", "type": "password", "required": False,
             "show_when_auth": ["bearer"]},
            {"key": "companies_endpoint", "label": "Companies Endpoint (e.g. /api/companies)", "type": "text", "required": False},
            {"key": "devices_endpoint", "label": "Devices Endpoint (e.g. /api/devices)", "type": "text", "required": False},
            {"key": "company_name_field", "label": "Company Name JSON Field", "type": "text", "required": False, "default": "name"},
            {"key": "device_hostname_field", "label": "Device Hostname JSON Field", "type": "text", "required": False, "default": "hostname"},
            {"key": "device_serial_field", "label": "Device Serial JSON Field", "type": "text", "required": False, "default": "serial_number"},
            {"key": "device_mac_field", "label": "Device MAC JSON Field", "type": "text", "required": False, "default": "mac_address"},
            {"key": "device_company_field", "label": "Device Company JSON Field", "type": "text", "required": False, "default": "company"},
        ],
    },
}


def build_connector(platform: str, credentials: dict[str, str]) -> AbstractPlatformConnector:
    """Build a connector instance from a platform name and credential dict.

    Creates a Settings override so the connector uses the provided credentials
    instead of the global .env values.
    """
    base = get_settings()

    if platform == "avast":
        settings = base.model_copy(update={
            "avast_client_id": credentials.get("client_id", ""),
            "avast_client_secret": credentials.get("client_secret", ""),
        })
        return AvastConnector(settings=settings)

    if platform == "eset":
        settings = base.model_copy(update={
            "eset_username": credentials.get("username", ""),
            "eset_password": credentials.get("password", ""),
            "eset_base_url": credentials.get("base_url", "") or base.eset_base_url,
        })
        return EsetConnector(settings=settings)

    if platform == "eset_connect":
        settings = base.model_copy(update={
            "eset_connect_username": credentials.get("username", ""),
            "eset_connect_password": credentials.get("password", ""),
            "eset_connect_base_url": credentials.get("base_url", "") or base.eset_connect_base_url,
            "eset_connect_auth_url": credentials.get("auth_url", "") or base.eset_connect_auth_url,
        })
        return EsetConnectConnector(settings=settings)

    if platform == "connectwise_psa":
        settings = base.model_copy(update={
            "cw_company_id": credentials.get("company_id", ""),
            "cw_public_key": credentials.get("public_key", ""),
            "cw_private_key": credentials.get("private_key", ""),
            "cw_client_id": credentials.get("client_id", ""),
            "cw_base_url": credentials.get("base_url", "") or base.cw_base_url,
        })
        return ConnectWisePsaConnector(settings=settings)

    if platform == "screenconnect":
        settings = base.model_copy(update={
            "screenconnect_base_url": credentials.get("base_url", ""),
            "screenconnect_auth_token": credentials.get("auth_token", ""),
            "screenconnect_extension_id": credentials.get("extension_id", "") or base.screenconnect_extension_id,
        })
        return ScreenConnectConnector(settings=settings)

    if platform == "sentinelone":
        settings = base.model_copy(update={
            "s1_base_url": credentials.get("base_url", ""),
            "s1_api_token": credentials.get("api_token", ""),
        })
        return SentinelOneConnector(settings=settings)

    if platform == "asio":
        settings = base.model_copy(update={
            "asio_client_id": credentials.get("client_id", ""),
            "asio_client_secret": credentials.get("client_secret", ""),
            "asio_base_url": credentials.get("base_url", "") or base.asio_base_url,
            "asio_token_url": credentials.get("token_url", "") or base.asio_token_url,
            "asio_scope": credentials.get("scope", "") or base.asio_scope,
            "asio_region": credentials.get("region", "") or base.asio_region,
        })
        return AsioConnector(settings=settings)

    if platform == "generic_rest":
        return GenericRestConnector(
            platform_label=credentials.get("platform_label", "generic_rest"),
            base_url=credentials.get("base_url", ""),
            auth_method=credentials.get("auth_method", "none"),
            auth_header_name=credentials.get("auth_header_name", ""),
            auth_header_value=credentials.get("auth_header_value", ""),
            auth_username=credentials.get("auth_username", ""),
            auth_password=credentials.get("auth_password", ""),
            auth_token=credentials.get("auth_token", ""),
            companies_endpoint=credentials.get("companies_endpoint", ""),
            devices_endpoint=credentials.get("devices_endpoint", ""),
            company_name_field=credentials.get("company_name_field", "name"),
            device_hostname_field=credentials.get("device_hostname_field", "hostname"),
            device_serial_field=credentials.get("device_serial_field", "serial_number"),
            device_mac_field=credentials.get("device_mac_field", "mac_address"),
            device_company_field=credentials.get("device_company_field", "company"),
        )

    raise ValueError(f"Unknown platform: {platform}")


def redact_credentials(credentials: dict[str, str], platform: str) -> dict[str, str]:
    """Return credentials with password/secret fields masked for display."""
    definition = PLATFORM_DEFINITIONS.get(platform, {})
    secret_keys = {
        f["key"]
        for f in definition.get("fields", [])
        if f.get("type") == "password"
    }
    redacted = {}
    for key, value in credentials.items():
        if key in secret_keys and value:
            redacted[key] = "••••••••"
        else:
            redacted[key] = value
    return redacted
