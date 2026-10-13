"""Password hashing (scrypt) and TOTP (RFC 6238) for local break-glass accounts — standard library only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1
_MIN_PASSWORD = 12


def hash_password(password: str) -> str:
    if len(password) < _MIN_PASSWORD:
        raise ValueError(f"Password must be at least {_MIN_PASSWORD} characters")
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, maxmem=64 * 1024 * 1024)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(dk_b64)
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p),
                            maxmem=64 * 1024 * 1024, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


# A fixed hash used to spend the same time on unknown usernames (no user-enumeration by timing).
DUMMY_HASH = "scrypt$32768$8$1$AAAAAAAAAAAAAAAAAAAAAA==$" + base64.b64encode(b"\0" * 64).decode()


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def totp_now(secret_b32: str, at: float | None = None) -> str:
    return _hotp(secret_b32, int((at if at is not None else time.time()) // 30))


def verify_totp(secret_b32: str, code: str, at: float | None = None, window: int = 1) -> int | None:
    """Return the matching 30-second step (for replay protection) or None."""
    code = (code or "").strip().replace(" ", "")
    if not (len(code) == 6 and code.isdigit()):
        return None
    step = int((at if at is not None else time.time()) // 30)
    for s in range(step - window, step + window + 1):
        if hmac.compare_digest(_hotp(secret_b32, s), code):
            return s
    return None


def totp_uri(secret_b32: str, username: str, issuer: str) -> str:
    label = quote(f"{issuer}:{username}")
    return f"otpauth://totp/{label}?secret={secret_b32}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"


def totp_qr_data_uri(uri: str) -> str:
    """QR code for an ``otpauth://`` URI as an SVG data URI, drawn locally (the secret never leaves the server)."""
    import segno

    # Black on solid white with the standard 4-module quiet zone: readable by phone cameras in dark mode too.
    return segno.make(uri, error="m").svg_data_uri(scale=6, border=4, dark="#000", light="#fff")


def totp_qr_terminal(uri: str) -> None:
    """Print the QR code to the terminal (for the CLI)."""
    import segno

    segno.make(uri, error="m").terminal(compact=True)
