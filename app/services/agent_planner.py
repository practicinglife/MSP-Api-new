"""Agent planner — natural-language → intent → workflow steps.

Stage 1 (smart, slow):
    1. Classify the intent of the NL request using rules.
    2. Either return a pre-canned step plan for a known intent OR
       fall back to suggesting catalog endpoints the caller can chain.

Stage 2 (learn):
    Successful Stage 1 plans are persisted as :class:`AgentWorkflow` rows
    so subsequent identical intents can skip the planner entirely.

Stage 3 (fast):
    The agent endpoint looks up an existing workflow first and replays it
    immediately — no LLM, no planning cost. See :mod:`app.api.agent`.

Intent classification is intentionally rule-based for now. An external
LLM agent (AnythingLLM, Copilot) can pre-classify and pass ``intent``
explicitly; otherwise ``classify_intent`` infers it from keywords.
"""

from __future__ import annotations

import re
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# ── Intent classification ────────────────────────────────────────


_INTENT_RULES: list[tuple[str, list[str]]] = [
    # (intent_name, keyword patterns — any match wins)
    ("company_report",       [r"\breport\b.*\bcompany\b", r"\bpdf\b.*\bcompany\b",
                              r"\bcompany\b.*\bbreakdown\b", r"\bgenerate\b.*\breport\b"]),
    ("find_company",         [r"\bfind\b.*\bcompany\b", r"\blook\s*up\b.*\bcompany\b",
                              r"\bsearch\b.*\bcompany\b"]),
    ("cleanup_mismatched",   [r"\bcleanup\b", r"\bresolve\b.*\bmismatch", r"\bfix\b.*\bmismatch",
                              r"\bremove\b.*\bduplicate"]),
    ("daily_health_check",   [r"\bhealth\b.*\bcheck\b", r"\bsystem\b.*\bstatus\b",
                              r"\ball\b.*\bplatforms?\b.*\bstatus\b"]),
    ("incident_response",    [r"\bthreat\b", r"\balert\b", r"\bincident\b",
                              r"\bsecurity\b.*\bissue\b"]),
    ("list_unmatched",       [r"\bunmatched\b", r"\bmismatched\b.*\bdevice"]),
    ("list_companies",       [r"\blist\b.*\ball\b.*\bcompanies\b",
                              r"\bshow\b.*\bcompanies\b"]),
    ("ticket_summary",       [r"\bticket\b.*\bsummary\b", r"\bopen\b.*\btickets\b"]),
]


def classify_intent(text: str) -> str:
    """Map natural language to a known intent slug. Returns ``"unknown"`` if no match."""
    if not text:
        return "unknown"
    lowered = text.lower().strip()
    for intent, patterns in _INTENT_RULES:
        for pat in patterns:
            if re.search(pat, lowered):
                return intent
    return "unknown"


# ── Parameter extraction ─────────────────────────────────────────


def extract_parameters(text: str, intent: str) -> dict[str, Any]:
    """Pull intent-specific parameters out of free text.

    For the MVP we extract only the ``company`` argument since that's what
    the most common intents need. Other params can be passed explicitly
    in the API call.
    """
    params: dict[str, Any] = {}
    if intent in {
        "company_report", "find_company", "cleanup_mismatched",
        "list_unmatched",
    }:
        company = _extract_company_phrase(text)
        if company:
            params["company"] = company
    return params


_COMPANY_PHRASE_PATTERNS = [
    r"(?:for|of|on|about)\s+([A-Z][\w&'\-. ]{2,80}?)(?=\s*(?:and|with|where|please|so|to|then|$|\.|,|;|!|\?))",
    r"(?:company|customer|client)\s+([A-Z][\w&'\-. ]{2,80}?)(?=\s*(?:and|with|where|please|so|to|then|$|\.|,|;|!|\?))",
    r"\"([^\"]+)\"",
    r"'([^']+)'",
]


def _extract_company_phrase(text: str) -> str | None:
    for pat in _COMPANY_PHRASE_PATTERNS:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            cand = m.group(1).strip(" .,;:!?")
            if 2 <= len(cand) <= 80:
                return cand
    return None


# ── Pre-canned step plans for known intents ──────────────────────
# Each step is one HTTP call against a *local* MSP Tools endpoint.
# The executor (workflow_executor.py) substitutes ``{{param}}`` tokens
# at run-time using the parameters passed in.


_BASE_PLANS: dict[str, dict[str, Any]] = {
    "company_report": {
        "name": "Company Device Report (PDF)",
        "description": (
            "Generate a per-company cross-platform device report with "
            "mismatched devices highlighted and suggested actions."
        ),
        "parameters_schema": {
            "type": "object",
            "required": ["company"],
            "properties": {
                "company": {"type": "string", "description": "Company name (fuzzy)"},
                "format":  {"type": "string", "enum": ["pdf", "html", "json"], "default": "pdf"},
                "save":    {"type": "boolean", "default": True},
            },
        },
        "steps": [
            {
                "name": "render_report",
                "method": "GET",
                "endpoint": "/api/reports/company/{{company}}",
                "query": {"format": "{{format|default:pdf}}", "save": "{{save|default:true}}"},
                "requires_approval": False,
                "destructive": False,
                "expect_binary": True,
            },
        ],
    },
    "find_company": {
        "name": "Find Company",
        "description": "Look up a company across all platforms.",
        "parameters_schema": {
            "type": "object",
            "required": ["company"],
            "properties": {"company": {"type": "string"}},
        },
        "steps": [
            {
                "name": "search_companies",
                "method": "GET",
                "endpoint": "/api/companies/summary",
                "filter_response": "company_contains:{{company}}",
                "requires_approval": False,
                "destructive": False,
            },
        ],
    },
    "cleanup_mismatched": {
        "name": "Cleanup Mismatched Devices",
        "description": "Identify mismatched devices for a company and propose cleanup actions.",
        "parameters_schema": {
            "type": "object",
            "required": ["company"],
            "properties": {
                "company":  {"type": "string"},
                "auto_apply": {"type": "boolean", "default": False,
                               "description": "If true, apply suggested actions (destructive)."},
            },
        },
        "steps": [
            {
                "name": "fetch_breakdown",
                "method": "GET",
                "endpoint": "/api/reports/company/{{company}}",
                "query": {"format": "json"},
                "requires_approval": False,
                "destructive": False,
            },
            # Note: the executor handles per-device suggested actions when
            # ``auto_apply=true``; we don't enumerate them here because they
            # depend on the breakdown contents (dynamic).
        ],
    },
    "daily_health_check": {
        "name": "Daily Health Check",
        "description": "Verify all connectors are reachable and report platform status.",
        "parameters_schema": {"type": "object", "properties": {}},
        "steps": [
            {
                "name": "test_connectors",
                "method": "POST",
                "endpoint": "/api/connectors/test",
                "requires_approval": False,
                "destructive": False,
            },
        ],
    },
    "list_unmatched": {
        "name": "List Unmatched Devices",
        "description": "Return mismatched devices, optionally filtered by company.",
        "parameters_schema": {
            "type": "object",
            "properties": {"company": {"type": "string"}},
        },
        "steps": [
            {
                "name": "fetch_reconciled",
                "method": "GET",
                "endpoint": "/api/companies/reconciled",
                "filter_response": "mismatched_only",
                "requires_approval": False,
                "destructive": False,
            },
        ],
    },
    "list_companies": {
        "name": "List All Companies",
        "description": "Return cross-platform summary of all companies.",
        "parameters_schema": {"type": "object", "properties": {}},
        "steps": [
            {
                "name": "fetch_summary",
                "method": "GET",
                "endpoint": "/api/companies/summary",
                "requires_approval": False,
                "destructive": False,
            },
        ],
    },
    "incident_response": {
        "name": "Incident Response — list active threats",
        "description": "Pull active SentinelOne threats. Ticket creation requires approval.",
        "parameters_schema": {"type": "object", "properties": {}},
        "steps": [
            {
                "name": "list_threats",
                "method": "GET",
                "endpoint": "/api/proxy/sentinelone/web/api/v2.1/threats",
                "query": {"limit": 50, "resolved": "false"},
                "requires_approval": False,
                "destructive": False,
            },
        ],
    },
}


def get_base_plan(intent: str) -> dict[str, Any] | None:
    """Return the pre-canned plan for an intent (or None if unknown)."""
    return _BASE_PLANS.get(intent)


def known_intents() -> list[str]:
    """All intents the planner knows about out-of-the-box."""
    return list(_BASE_PLANS.keys())
