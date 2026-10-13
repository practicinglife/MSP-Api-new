"""Confirmation gating for destructive actions (AI-agent safety).

Destructive endpoints (mark-inactive, remove agent, uninstall, offboard, …) can
be triggered by AI agents over MCP. To make those flows safe by default we use a
two-phase preview -> confirm handshake:

1. ``preview`` returns a human/agent-readable impact summary plus a one-time
   ``confirm_token`` scoped to the exact ``(action, target)`` pair.
2. ``execute`` requires that token; the server re-derives the signature and
   rejects tampered, mismatched, or expired tokens.

Tokens are **stateless HMAC-signed strings** (no DB / shared memory) so they
survive worker restarts and work across processes. The signing secret comes from
``Settings.confirm_secret``, else is derived from ``MSP_MASTER_KEY``, else is random per process.

The browser UI keeps working unchanged: a signed-in person supplies their own confirmation in the page.
Every *non-browser* caller (connection keys, OAuth tokens, MCP tools, workflows) and anything sending
``X-MSP-Agent`` must present a token. When a ``confirm_token`` is supplied by any caller it is always validated.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

from fastapi import HTTPException, Request

from app.config import get_settings

__all__ = [
    "AGENT_HEADER",
    "ConfirmationRequired",
    "issue_token",
    "verify_token",
    "is_agent_request",
    "enforce_confirmation",
    "build_preview",
]

#: Header an MCP/AnythingLLM agent sets to opt into strict confirmation gating.
AGENT_HEADER = "x-msp-agent"

_ALG = "sha256"


_PROCESS_SECRET = secrets.token_hex(32)


def _signing_key() -> bytes:
    """Return the HMAC signing key: CONFIRM_SECRET, else derived from MSP_MASTER_KEY, else random per process.

    (It used to be derived from the database URL, which is guessable when the default password is in use.)
    A per-process secret only means outstanding confirm tokens (5-minute lifetime) die on restart.
    """
    settings = get_settings()
    secret = (settings.confirm_secret or "").strip()
    if not secret:
        master = os.environ.get("MSP_MASTER_KEY", "").strip()
        secret = hmac.new(master.encode(), b"msp-confirm-v1", hashlib.sha256).hexdigest() if master \
            else _PROCESS_SECRET
    return secret.encode("utf-8")


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _scope(action: str, target: str) -> str:
    return f"{action}|{target}"


def issue_token(action: str, target: str, ttl_seconds: int | None = None) -> dict[str, Any]:
    """Issue a one-time confirm token bound to ``(action, target)``.

    Returns a dict with the opaque ``confirm_token`` plus expiry metadata so the
    caller can surface it in a preview response.
    """
    if not action or not target:
        raise ValueError("action and target are required to issue a confirm token")

    settings = get_settings()
    ttl = int(ttl_seconds if ttl_seconds is not None else settings.confirm_token_ttl_seconds)
    expires_at = int(time.time()) + ttl

    payload = {"a": action, "t": target, "exp": expires_at}
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64encode(payload_bytes)
    signature = hmac.new(_signing_key(), payload_b64.encode("ascii"), _ALG).digest()
    token = f"{payload_b64}.{_b64encode(signature)}"

    return {
        "confirm_token": token,
        "expires_at": expires_at,
        "expires_in_seconds": ttl,
    }


def verify_token(token: str, action: str, target: str) -> bool:
    """Return ``True`` only for a valid, unexpired token matching the scope."""
    if not token or "." not in token:
        return False
    payload_b64, sig_b64 = token.rsplit(".", 1)

    expected_sig = hmac.new(_signing_key(), payload_b64.encode("ascii"), _ALG).digest()
    try:
        provided_sig = _b64decode(sig_b64)
    except (ValueError, TypeError):
        return False
    if not hmac.compare_digest(expected_sig, provided_sig):
        return False

    try:
        payload = json.loads(_b64decode(payload_b64))
    except (ValueError, TypeError, json.JSONDecodeError):
        return False

    if payload.get("a") != action or payload.get("t") != target:
        return False
    if int(payload.get("exp", 0)) < int(time.time()):
        return False
    return True


def is_agent_request(request: Request | None) -> bool:
    """Return ``True`` for any caller that is not a person in the browser (or that flagged itself an agent)."""
    if request is None:
        return False
    if request.headers.get(AGENT_HEADER, "").strip().lower() in {"1", "true", "yes"}:
        return True
    principal = getattr(request.state, "principal", None)
    return principal is not None and principal.kind != "session"


class ConfirmationRequired(HTTPException):
    """Raised when a destructive action needs an explicit confirm token.

    Returns HTTP 409 with a fresh token and impact summary so the agent can
    re-issue the call with ``confirm_token`` set.
    """

    def __init__(self, action: str, target: str, preview: dict[str, Any]) -> None:
        token = issue_token(action, target)
        super().__init__(
            status_code=409,
            detail={
                "error": "confirmation_required",
                "message": (
                    "This is a destructive action. Review the impact, then retry "
                    "with the supplied confirm_token to proceed."
                ),
                "action": action,
                "target": target,
                "preview": preview,
                **token,
            },
        )


def build_preview(action: str, target: str, **summary: Any) -> dict[str, Any]:
    """Build a preview payload (impact summary + fresh confirm token)."""
    token = issue_token(action, target)
    return {
        "action": action,
        "target": target,
        "destructive": True,
        "summary": summary,
        **token,
    }


def enforce_confirmation(
    request: Request | None,
    action: str,
    target: str,
    confirm_token: str | None,
    *,
    preview: dict[str, Any] | None = None,
) -> None:
    """Gate a destructive endpoint.

    Rules:
    * If ``confirm_token`` is provided, it MUST be valid for ``(action, target)``
      regardless of caller — otherwise raise 403.
    * If no token is provided and the caller is an agent (``X-MSP-Agent``), raise
      :class:`ConfirmationRequired` (409) with a fresh token + preview.
    * If no token is provided and the caller is the trusted UI, allow through
      (the UI shows its own human confirmation).
    """
    if confirm_token:
        if not verify_token(confirm_token, action, target):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "invalid_confirm_token",
                    "message": "confirm_token is invalid, expired, or scoped to a different action/target.",
                    "action": action,
                    "target": target,
                },
            )
        return

    if is_agent_request(request):
        raise ConfirmationRequired(action, target, preview or {})
