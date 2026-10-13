"""Layer 2 — delta-aware platform refresh.

Compares each company's per-platform device set against the last stored
fingerprint to decide whether a refresh is needed. Cheap-and-fast:

1. Build a fingerprint = ``(count, sha256(sorted(endpoint_ids|hostnames)))``
   from devices currently in the local ``platform_devices`` table.
2. Compare to the previously stored ``platform_fingerprints`` row.
3. Caller can decide: skip refresh, or refresh only changed companies.

The refresh itself is *not* implemented here — this module only answers
*what* changed. A future scheduler can poll connectors more cheaply for
delta-only data once the supporting per-platform queries land.
"""

from __future__ import annotations

import hashlib
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.agent_repository import PlatformFingerprintRepository
from app.db.repository import PlatformDeviceRepository

log = structlog.get_logger(__name__)


def compute_fingerprint(device_keys: list[str]) -> tuple[int, str]:
    """Return ``(count, hash)`` for a sorted list of device identifiers."""
    keys_clean = sorted({k.strip() for k in device_keys if k and k.strip()})
    digest = hashlib.sha256("|".join(keys_clean).encode("utf-8")).hexdigest()
    return len(keys_clean), digest


async def refresh_fingerprints(session: AsyncSession) -> dict[str, Any]:
    """Recompute fingerprints for every (platform, company) seen in the DB.

    Returns a summary::

        {
          "scanned": 42,                # company-platform pairs
          "changed": 3,                 # how many changed since last check
          "changes": [
            {"platform": "...", "company": "...", "old_count": 8, "new_count": 9},
            ...
          ]
        }
    """
    dev_repo = PlatformDeviceRepository(session)
    all_devices = await dev_repo.list_all()

    # Bucket: (platform, company) → list of identifying keys
    buckets: dict[tuple[str, str], list[str]] = {}
    for d in all_devices:
        company = (d.company_raw or "").strip().lower()
        if not company:
            continue
        # Prefer endpoint_id, then platform_id, then hostname for stability
        key = d.endpoint_id or d.platform_id or (d.hostname or "")
        if not key:
            continue
        buckets.setdefault((d.platform, company), []).append(key)

    fp_repo = PlatformFingerprintRepository(session)
    changes: list[dict[str, Any]] = []
    scanned = 0

    for (platform, company), keys in buckets.items():
        scanned += 1
        count, hashed = compute_fingerprint(keys)

        # Capture old state before upsert
        existing = await fp_repo.get(platform, company)
        old_count = existing.device_count if existing else 0
        old_hash = existing.devices_hash if existing else ""

        _, changed = await fp_repo.upsert(
            platform=platform, company=company,
            device_count=count, devices_hash=hashed,
        )
        if changed:
            changes.append({
                "platform": platform,
                "company": company,
                "old_count": old_count,
                "new_count": count,
                "old_hash": old_hash[:8],
                "new_hash": hashed[:8],
            })

    log.info("fingerprints.refreshed", scanned=scanned, changed=len(changes))
    return {"scanned": scanned, "changed": len(changes), "changes": changes}
