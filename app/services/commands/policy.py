"""Default-deny areas for the generic vendor_call command.

Anything matching here can't be proposed through the catalog at all — use a dedicated command or the
vendor's own console. Patterns are matched case-insensitively against the request path.
"""

from __future__ import annotations

import re

DENY: dict[str, list[str]] = {
    "*": [
        r"/(users?|members?|roles?|permissions?|security|auth|token|apikeys?|api-?tokens?|sso|saml|oauth)(/|$)",
        r"/(billing|invoices?|payments?|subscriptions?|licen[cs]es?)(/|$)",
        r"/(webhooks?|integrations?|callbacks?)(/|$)",
    ],
    "connectwise_psa": [
        r"^/system/",          # members, security roles, API members, setup tables
        r"^/finance/",         # agreements, invoices, billing
        r"^/company/companies/\d+/merge",
    ],
    "sentinelone": [
        r"^/web/api/v2\.\d/(accounts|users|rbac|settings|config-override|exclusions|restrictions)",
        r"/agents/actions/(uninstall|reset-local-config|shutdown|restart-machine|disconnect)",
        r"/threats/",          # mitigation belongs in the S1 console
    ],
    "asio": [
        r"/(automation|scripts?)/",  # running scripts is a remediation command, not a catalog call
    ],
    "screenconnect": [
        r"/(Admin|Security|Extension)",
    ],
    "eset": [
        r"/(User|Admin|Security|Token)",
    ],
}


def denied(platform: str, path: str) -> str | None:
    """Return the matching rule if the path is denied, else None."""
    for pattern in DENY.get("*", []) + DENY.get(platform, []):
        if re.search(pattern, path, re.IGNORECASE):
            return pattern
    return None
