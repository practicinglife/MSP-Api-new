"""
Reconciler service for the MSP Company Breakdown report.

Terminology
-----------
platform        : one of the four integrations tracked (asio, connectwise_psa,
                  sentinelone, screenconnect).
platforms_found : the set of platforms a specific device is present on.
expected_platforms : the union of all platforms found across *every* device in
                   the company.  A device is only considered "fully matched"
                   when it is present on every platform that any other device
                   in the same company is on.

Match-type rules
----------------
  full    – device is present on ALL expected platforms for its company.
  partial – device is present on SOME (but not all) expected platforms.
  none    – device is not present on any tracked platform.

Matched / Mismatched bucket rules
----------------------------------
  matched   – only "full" devices.
  mismatched – "partial" and "none" devices.

Previous (incorrect) logic (lines 120-121 of the old reconciler):
    if len(platforms_found) >= 3: match_type = "full"
    elif len(platforms_found) >= 2: match_type = "partial"

  This used an arbitrary count threshold instead of comparing against the
  actual set of platforms expected for each company, causing:
    • HEATHER-LAPTOP (3 platforms, missing SC) → incorrectly labelled "full"
    • LAPTOP-O577LVAU (2 platforms) → labelled "partial" but counted as
      matched, hiding its gaps from the remediation view.
"""

# ── Platform column mapping ───────────────────────────────────────────────────
# Maps internal platform key → the "In_*" column in the raw device record.
PLATFORM_COLUMNS = {
    "asio": "In_ASIO",
    "connectwise_psa": "In_CW_PSA",
    "sentinelone": "In_S1",
    "screenconnect": "In_SC",
}


def _platforms_for_device(device: dict) -> set:
    """Return the set of platform keys this device is present on."""
    return {
        platform
        for platform, column in PLATFORM_COLUMNS.items()
        if device.get(column, "No").strip().lower() == "yes"
    }


def _classify_device(device: dict, expected_platforms: set) -> dict:
    """
    Classify a single device against the expected platform set.

    Returns the device dict augmented with:
      platforms_found   – list of platforms the device is on
      missing_platforms – list of platforms it should be on but isn't
      match_type        – "full" | "partial" | "none"
    """
    platforms_found = _platforms_for_device(device)
    missing = expected_platforms - platforms_found

    # ── FIX: compare against the company's expected platforms, not a count ──
    # Old (broken) logic that this replaces (lines 120-121):
    #   if len(platforms_found) >= 3:
    #       match_type = "full"
    #   elif len(platforms_found) >= 2:
    #       match_type = "partial"
    if not platforms_found:
        match_type = "none"
    elif missing:
        match_type = "partial"
    else:
        match_type = "full"

    return {
        **device,
        "platforms_found": sorted(platforms_found),
        "missing_platforms": sorted(missing),
        "match_type": match_type,
    }


def reconcile_company(company_name: str, devices: list) -> dict:
    """
    Reconcile all devices for a single company.

    Args:
        company_name : display name of the company.
        devices      : list of raw device dicts for this company.

    Returns:
        dict with keys:
          company           – company name
          expected_platforms – sorted list of platforms expected for this company
          matched           – list of fully-matched device records
          mismatched        – list of partial / unmatched device records
          matched_count     – len(matched)
          mismatched_count  – len(mismatched)
    """
    # Step 1: Determine expected platforms as the union across all devices.
    expected_platforms: set = set()
    for device in devices:
        expected_platforms |= _platforms_for_device(device)

    # Step 2: Classify each device against the expected set.
    matched = []
    mismatched = []
    for device in devices:
        classified = _classify_device(device, expected_platforms)
        if classified["match_type"] == "full":
            matched.append(classified)
        else:
            mismatched.append(classified)

    return {
        "company": company_name,
        "expected_platforms": sorted(expected_platforms),
        "matched": matched,
        "mismatched": mismatched,
        "matched_count": len(matched),
        "mismatched_count": len(mismatched),
    }


def reconcile_all(devices: list) -> list:
    """
    Group raw device records by company and reconcile each group.

    Args:
        devices : flat list of raw device dicts (each must have a 'company' key).

    Returns:
        List of reconciled company dicts, sorted alphabetically by company name.
    """
    # Group by company
    by_company: dict = {}
    for device in devices:
        company = device.get("company", "Unknown")
        by_company.setdefault(company, []).append(device)

    return [
        reconcile_company(name, devs)
        for name, devs in sorted(by_company.items())
    ]
