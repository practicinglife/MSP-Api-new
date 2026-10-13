"""Encryption at rest for stored vendor credentials (AES-256-GCM).

The master key comes from ``MSP_MASTER_KEY`` (32 random bytes, url-safe base64) — generate one with
``python -m app.cli gen-master-key`` and keep it outside the database. Losing it means re-entering every saved
connector's credentials; leaking it together with a database dump exposes them.

``EncryptedJSON`` is a column type: values are encrypted on write and decrypted on read. Rows written before
encryption existed (plain JSON) are still readable and get encrypted the next time they are saved
(``reencrypt_plaintext_rows`` does all of them at startup).
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from functools import lru_cache
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import JSON
from sqlalchemy.types import TypeDecorator

_MARK = "__enc__"
_VERSION = "v1"
_AAD = b"msp-api-tools:credentials:v1"


class MasterKeyError(RuntimeError):
    """Raised when credentials must be encrypted/decrypted but no usable master key is configured."""


def generate_master_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


@lru_cache(maxsize=1)
def _key() -> bytes | None:
    raw = os.environ.get("MSP_MASTER_KEY", "").strip()
    if not raw:
        return None
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except ValueError as exc:
        raise MasterKeyError("MSP_MASTER_KEY is not valid url-safe base64") from exc
    if len(key) != 32:
        raise MasterKeyError("MSP_MASTER_KEY must decode to exactly 32 bytes")
    return key


def master_key_configured() -> bool:
    try:
        return _key() is not None
    except MasterKeyError:
        return False


def reset_key_cache() -> None:
    """For tests / key rotation tooling."""
    _key.cache_clear()


def is_encrypted(value: Any) -> bool:
    return isinstance(value, dict) and value.get(_MARK) == _VERSION and "n" in value and "c" in value


def encrypt_value(value: Any) -> dict[str, str]:
    key = _key()
    if key is None:
        raise MasterKeyError(
            "Saving credentials requires MSP_MASTER_KEY. Generate one with `python -m app.cli gen-master-key`, "
            "put it in .env and restart."
        )
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, json.dumps(value, separators=(",", ":")).encode(), _AAD)
    return {_MARK: _VERSION, "n": base64.b64encode(nonce).decode(), "c": base64.b64encode(ct).decode()}


def decrypt_value(stored: Any) -> Any:
    if not is_encrypted(stored):
        return stored  # legacy plaintext row
    key = _key()
    if key is None:
        raise MasterKeyError("Stored credentials are encrypted but MSP_MASTER_KEY is not set")
    pt = AESGCM(key).decrypt(base64.b64decode(stored["n"]), base64.b64decode(stored["c"]), _AAD)
    return json.loads(pt)


class EncryptedJSON(TypeDecorator):  # type: ignore[type-arg]
    """JSON column whose value is stored AES-GCM encrypted."""

    impl = JSON
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None or is_encrypted(value):
            return value
        return encrypt_value(value)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        return decrypt_value(value)


async def reencrypt_plaintext_rows() -> int:
    """Encrypt connector credentials still stored as plain JSON. Returns how many rows were converted."""
    if not master_key_configured():
        return 0
    from sqlalchemy import select, text
    from sqlalchemy.orm.attributes import flag_modified

    from app.db.repository import get_session_factory
    from app.db.schema import ConnectorConfig

    converted = 0
    async with get_session_factory()() as session:
        raw_rows = (await session.execute(text("SELECT id, credentials FROM connector_configs"))).all()
        for row_id, raw in raw_rows:
            value = json.loads(raw) if isinstance(raw, str) else raw
            if value is None or is_encrypted(value):
                continue
            obj = (await session.execute(select(ConnectorConfig).where(ConnectorConfig.id == row_id))).scalar_one()
            obj.credentials = dict(value)
            flag_modified(obj, "credentials")  # equal value → force the UPDATE so it goes through EncryptedJSON
            converted += 1
        if converted:
            await session.commit()
    return converted
