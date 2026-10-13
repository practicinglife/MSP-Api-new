"""Device matching service using tiered scoring."""

from dataclasses import dataclass, field
from enum import Enum

from app.models.device import DeviceRecord
from app.services.normalizers import SERIAL_BLOCKLIST, normalize_company_name

SCORE_SERIAL_EXACT = 100
SCORE_MAC_OVERLAP = 60
SCORE_COMPANY_HOSTNAME = 50
SCORE_HOSTNAME_EXACT = 30

AUTO_LINK_THRESHOLD = 90
REVIEW_LOWER_THRESHOLD = 30


class DeviceMatchStatus(str, Enum):
    """Outcome of a device match attempt."""

    AUTO_LINKED = "auto_linked"
    NEEDS_REVIEW = "needs_review"
    UNMATCHED = "unmatched"


@dataclass
class DeviceMatchResult:
    """Result of matching two device records."""

    source: DeviceRecord
    target: DeviceRecord | None
    score: int
    status: DeviceMatchStatus
    reasons: list[str] = field(default_factory=list)


def _serial_valid(serial: str) -> bool:
    """Return True when *serial* is a non-placeholder value."""
    return bool(serial) and serial.upper() not in SERIAL_BLOCKLIST


def _score_pair(a: DeviceRecord, b: DeviceRecord) -> tuple[int, list[str]]:
    """Compute a match score between two device records.

    Scoring rules:
    - Serial exact match (valid serials only): +100 (immediately dominant)
    - MAC address overlap (any common MAC):    +60
    - Hostname exact match:                   +30
    """
    score = 0
    reasons: list[str] = []

    # Serial exact
    if (
        _serial_valid(a.serial_number)
        and _serial_valid(b.serial_number)
        and a.serial_number == b.serial_number
    ):
        score += SCORE_SERIAL_EXACT
        reasons.append("serial_exact")

    # MAC overlap
    set_a = set(a.mac_addresses)
    set_b = set(b.mac_addresses)
    if set_a and set_b and set_a & set_b:
        score += SCORE_MAC_OVERLAP
        reasons.append("mac_overlap")

    # Hostname exact
    if a.hostname and b.hostname and a.hostname == b.hostname:
        score += SCORE_HOSTNAME_EXACT
        reasons.append("hostname_exact")

    # Company + hostname: same hostname within the same company is a strong signal
    company_a = normalize_company_name(a.company_name or a.company_platform_id or "")
    company_b = normalize_company_name(b.company_name or b.company_platform_id or "")
    if (
        company_a
        and company_b
        and company_a == company_b
        and a.hostname
        and b.hostname
        and a.hostname == b.hostname
    ):
        score += SCORE_COMPANY_HOSTNAME
        reasons.append("company_hostname")

    return score, reasons


def match_device(source: DeviceRecord, candidates: list[DeviceRecord]) -> DeviceMatchResult:
    """Find the best matching device from *candidates* for *source*.

    Uses tiered scoring:
    - Score >= :data:`AUTO_LINK_THRESHOLD` → ``AUTO_LINKED``
    - score in review range → ``NEEDS_REVIEW``
    - Score < :data:`REVIEW_LOWER_THRESHOLD` → ``UNMATCHED``
    """
    if not candidates:
        return DeviceMatchResult(
            source=source, target=None, score=0, status=DeviceMatchStatus.UNMATCHED
        )

    best_score = 0
    best_candidate: DeviceRecord | None = None
    best_reasons: list[str] = []

    for candidate in candidates:
        # Do not match a record against itself
        if candidate.platform == source.platform and candidate.platform_id == source.platform_id:
            continue
        score, reasons = _score_pair(source, candidate)
        if score > best_score:
            best_score = score
            best_candidate = candidate
            best_reasons = reasons

    status: DeviceMatchStatus
    if best_score >= AUTO_LINK_THRESHOLD:
        status = DeviceMatchStatus.AUTO_LINKED
    elif best_score >= REVIEW_LOWER_THRESHOLD:
        status = DeviceMatchStatus.NEEDS_REVIEW
    else:
        status = DeviceMatchStatus.UNMATCHED
        best_candidate = None

    return DeviceMatchResult(
        source=source,
        target=best_candidate,
        score=best_score,
        status=status,
        reasons=best_reasons,
    )
