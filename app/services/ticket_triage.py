"""Ticket triage and routing service.

Provides rule-based ticket classification, priority assessment, and
technician routing.  Inspired by TicketFlow
(https://github.com/kevindzit/TicketFlow) but integrated directly into
the FastAPI backend without requiring a local LLM.

The triage engine uses keyword matching and configurable rules to:
- Classify tickets by category (network, server, workstation, etc.)
- Assess urgency (critical / high / medium / low)
- Suggest the best technician based on skills and current workload
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger("ticket_triage")


# ── Category definitions ─────────────────────────────────────────

@dataclass
class CategoryRule:
    """A keyword-based rule that maps ticket text to a category."""
    name: str
    keywords: list[str]
    weight: int = 1


DEFAULT_CATEGORIES: list[CategoryRule] = [
    CategoryRule("network", [
        "network", "internet", "wifi", "wi-fi", "dns", "dhcp", "vpn",
        "firewall", "switch", "router", "connectivity", "latency",
        "packet loss", "wan", "lan", "vlan", "bandwidth",
    ], weight=2),
    CategoryRule("server", [
        "server", "domain controller", "active directory", "ad",
        "exchange", "sql server", "hyper-v", "vmware", "esxi",
        "raid", "disk space", "reboot", "blue screen", "bsod",
        "backup", "restore", "replication",
    ], weight=2),
    CategoryRule("workstation", [
        "workstation", "laptop", "desktop", "pc", "computer",
        "slow", "frozen", "crash", "won't boot", "won't start",
        "blue screen", "update", "windows update",
    ]),
    CategoryRule("email", [
        "email", "outlook", "mailbox", "calendar", "teams",
        "office 365", "microsoft 365", "m365", "exchange online",
        "spam", "phishing", "quarantine",
    ]),
    CategoryRule("printer", [
        "printer", "print", "scanner", "scan", "fax", "copier",
        "paper jam", "toner", "ink",
    ]),
    CategoryRule("security", [
        "security", "virus", "malware", "ransomware", "breach",
        "compromised", "phishing", "mfa", "password reset",
        "locked out", "lockout", "unauthorized", "suspicious",
        "sentinelone", "eset", "antivirus", "endpoint protection",
    ], weight=3),
    CategoryRule("onboarding", [
        "new user", "onboarding", "new hire", "new employee",
        "setup", "provision", "new account",
    ]),
    CategoryRule("offboarding", [
        "offboarding", "termination", "disable account",
        "remove access", "departing", "former employee",
    ]),
    CategoryRule("cloud", [
        "azure", "aws", "cloud", "saas", "iaas", "paas",
        "sharepoint", "onedrive", "intune", "entra",
    ]),
    CategoryRule("phone", [
        "phone", "voip", "sip", "pbx", "ring central",
        "teams calling", "softphone", "headset",
    ]),
]


# ── Urgency rules ────────────────────────────────────────────────

URGENCY_KEYWORDS: dict[str, list[str]] = {
    "critical": [
        "down", "outage", "emergency", "ransomware", "breach",
        "all users", "entire office", "production down",
        "can't work", "cannot work", "everyone affected",
    ],
    "high": [
        "urgent", "asap", "important", "critical", "server down",
        "multiple users", "security", "compromised", "data loss",
    ],
    "medium": [
        "slow", "intermittent", "error", "issue", "problem",
        "not working", "broken",
    ],
    "low": [
        "question", "how to", "request", "enhancement",
        "when you get a chance", "low priority", "fyi",
    ],
}


# ── Triage functions ─────────────────────────────────────────────

def classify_ticket(
    summary: str,
    description: str = "",
    categories: list[CategoryRule] | None = None,
) -> dict[str, Any]:
    """Classify a ticket by category using keyword matching.

    Returns a dict with:
    - ``category``: best-matching category name
    - ``confidence``: 0.0–1.0 score
    - ``scores``: all category scores for transparency
    """
    categories = categories or DEFAULT_CATEGORIES
    text = f"{summary} {description}".lower()

    scores: dict[str, int] = {}
    for rule in categories:
        score = 0
        for kw in rule.keywords:
            if kw in text:
                score += rule.weight
        if score > 0:
            scores[rule.name] = score

    if not scores:
        return {"category": "general", "confidence": 0.0, "scores": {}}

    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    total = sum(scores.values())
    confidence = round(scores[best] / total, 2) if total > 0 else 0.0

    return {
        "category": best,
        "confidence": confidence,
        "scores": scores,
    }


def assess_urgency(
    summary: str,
    description: str = "",
) -> dict[str, Any]:
    """Assess ticket urgency from text.

    Returns a dict with:
    - ``urgency``: critical / high / medium / low
    - ``triggers``: list of matched keywords
    """
    text = f"{summary} {description}".lower()
    triggers: list[str] = []

    for level in ("critical", "high", "medium", "low"):
        for kw in URGENCY_KEYWORDS[level]:
            if kw in text:
                triggers.append(kw)
                return {"urgency": level, "triggers": triggers}

    return {"urgency": "medium", "triggers": []}


def triage_ticket(
    summary: str,
    description: str = "",
) -> dict[str, Any]:
    """Full triage: classify + assess urgency.

    Returns a combined dict with category, urgency, and all details.
    """
    classification = classify_ticket(summary, description)
    urgency = assess_urgency(summary, description)

    return {
        **classification,
        **urgency,
    }


# ── Technician routing ───────────────────────────────────────────

@dataclass
class Technician:
    """Represents a technician with skills and workload."""
    id: str
    name: str
    skills: list[str] = field(default_factory=list)
    current_tickets: int = 0
    max_tickets: int = 15
    available: bool = True


def route_ticket(
    triage_result: dict[str, Any],
    technicians: list[Technician],
) -> dict[str, Any]:
    """Suggest the best technician for a triaged ticket.

    Scoring:
    - +10 for each matching skill
    - -1 for each current open ticket (workload penalty)
    - Unavailable technicians are excluded
    """
    category = triage_result.get("category", "general")
    candidates: list[dict[str, Any]] = []

    for tech in technicians:
        if not tech.available:
            continue
        if tech.current_tickets >= tech.max_tickets:
            continue

        score = 0
        skill_matches = []
        for skill in tech.skills:
            if skill.lower() == category.lower():
                score += 10
                skill_matches.append(skill)

        # Workload penalty
        score -= tech.current_tickets

        candidates.append({
            "technician_id": tech.id,
            "technician_name": tech.name,
            "score": score,
            "skill_matches": skill_matches,
            "current_tickets": tech.current_tickets,
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)

    if not candidates:
        return {"assigned": False, "reason": "No available technicians"}

    best = candidates[0]
    return {
        "assigned": True,
        "technician_id": best["technician_id"],
        "technician_name": best["technician_name"],
        "score": best["score"],
        "skill_matches": best["skill_matches"],
        "all_candidates": candidates[:5],
    }
