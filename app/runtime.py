"""Install-level choices made in the app (not in .env), and restarting to apply them.

``data/settings.json`` (``MSP_DATA_DIR``) holds what the Get started wizard decided — today, which packs are on.
``.env`` / the environment always wins over it.

Turning a pack on or off changes which pages and APIs exist, so the app must be rebuilt. Under
``python -m app.cli run`` that happens in place (:func:`request_restart`); under Docker or a plain ``uvicorn``
the caller is told to restart.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any


def data_dir() -> Path:
    return Path(os.environ.get("MSP_DATA_DIR", "").strip() or "data")


def _settings_path() -> Path:
    return data_dir() / "settings.json"


def load_settings() -> dict[str, Any]:
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(**values: Any) -> None:
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**load_settings(), **values}, indent=2) + "\n", encoding="utf-8")


# Settings the wizard saves → the environment variables the app reads. Anything already set by .env or the
# environment when the app started wins; the wizard only fills what wasn't configured there.
_ENV_FROM_SETTINGS = {"external_url": "MSP_EXTERNAL_BASE_URL"}
_SSO_ENV = {
    "entra": {"tenant_id": "MSP_ENTRA_TENANT_ID", "client_id": "MSP_ENTRA_CLIENT_ID",
              "client_secret": "MSP_ENTRA_CLIENT_SECRET"},
    "oidc": {"issuer": "MSP_OIDC_ISSUER", "client_id": "MSP_OIDC_CLIENT_ID", "client_secret": "MSP_OIDC_CLIENT_SECRET",
             "display_name": "MSP_OIDC_DISPLAY_NAME", "roles_claim": "MSP_OIDC_ROLES_CLAIM",
             "role_map": "MSP_OIDC_ROLE_MAP"},
}
_ALL_SSO_VARS = {v for m in _SSO_ENV.values() for v in m.values()}
_started_with: set[str] | None = None
_applied: dict[str, str] = {}  # what apply_settings put into the environment (only these are ever undone)


def _set(var: str, value: str | None) -> None:
    if value:
        os.environ[var] = value
        _applied[var] = value
    elif var in _applied:
        if os.environ.get(var) == _applied.pop(var):
            os.environ.pop(var, None)


def _from_env_at_start() -> set[str]:
    """Variables the operator set (in .env or the environment) — captured once, before the wizard adds any."""
    global _started_with
    if _started_with is None:
        _started_with = {k for k, v in os.environ.items() if v.strip()}
    return _started_with


def set_by_operator(var: str) -> bool:
    return var in _from_env_at_start()


def sso_from_env() -> bool:
    return any(set_by_operator(v) for v in ("MSP_ENTRA_TENANT_ID", "MSP_OIDC_ISSUER"))


def apply_settings() -> None:
    """Put the wizard's saved choices into the environment (where .env didn't already decide). Safe to call
    again after a change: values the wizard set before are replaced; .env values never are."""
    _from_env_at_start()
    settings = load_settings()
    for key, var in _ENV_FROM_SETTINGS.items():
        if not set_by_operator(var):
            _set(var, str(settings[key]) if settings.get(key) else None)
    if sso_from_env() or os.environ.get("MSP_NO_SSO") == "1":  # .env decides, or `run --no-sso`
        return
    sso = settings.get("sso") or {}
    provider = sso.get("provider")
    wanted: dict[str, str] = {}
    for key, var in _SSO_ENV.get(provider, {}).items():
        value = sso.get(key)
        if key == "client_secret" and isinstance(sso.get("client_secret_enc"), dict):
            from app.auth.crypto import decrypt_value

            try:
                value = decrypt_value(sso["client_secret_enc"])
            except Exception:  # noqa: BLE001 — wrong key: sign-in stays off rather than crashing the app
                value = None
        if isinstance(value, dict):
            value = json.dumps(value)
        if value:
            wanted[var] = str(value)
    for var in _ALL_SSO_VARS:  # switching provider (or to local accounts) clears what the old one set
        _set(var, wanted.get(var))


_restart: Callable[[], None] | None = None


def set_restart_handler(handler: Callable[[], None] | None) -> None:
    global _restart
    _restart = handler


def can_restart() -> bool:
    """Is the app running under ``app.cli run`` (which can rebuild it in place)?"""
    return _restart is not None


def request_restart() -> bool:
    """Ask the local runner to rebuild the app. False when the app isn't running under ``app.cli run``."""
    if _restart is None:
        return False
    _restart()
    return True
