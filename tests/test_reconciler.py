"""
Tests for app/services/reconciler.py

Covers:
  1. Version numbers: endpoint_protection is preserved through reconciliation.
  2. Match-type logic: compared against the company's expected platform set,
     not an arbitrary count threshold.
  3. Matched/mismatched buckets: only "full" devices count as matched.
"""

import pytest
from app.services.reconciler import (
    reconcile_company,
    reconcile_all,
    _platforms_for_device,
    _classify_device,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

GLOBEX_DEVICES = [
    # Present on all four platforms → full
    {
        "hostname": "GLOBEX-WS-01",
        "company": "Globex Industries",
        "In_ASIO": "Yes",
        "In_CW_PSA": "Yes",
        "In_S1": "Yes",
        "In_SC": "Yes",
        "endpoint_protection": "SentinelOne 25.1.4.434",
    },
    # Missing ScreenConnect → partial (mismatched)
    # OLD logic: 3 platforms → "full" (wrong)
    {
        "hostname": "HEATHER-LAPTOP",
        "company": "Globex Industries",
        "In_ASIO": "Yes",
        "In_CW_PSA": "Yes",
        "In_S1": "Yes",
        "In_SC": "No",
        "endpoint_protection": "SentinelOne 25.1.4.434",
    },
    # Missing SentinelOne AND ScreenConnect → partial (mismatched)
    # OLD logic: 2 platforms → "partial" but counted as matched (wrong)
    {
        "hostname": "LAPTOP-O577LVAU",
        "company": "Globex Industries",
        "In_ASIO": "Yes",
        "In_CW_PSA": "Yes",
        "In_S1": "No",
        "In_SC": "No",
        "endpoint_protection": "",
    },
]


# ── _platforms_for_device ─────────────────────────────────────────────────────

def test_platforms_for_device_all_present():
    device = {
        "In_ASIO": "Yes",
        "In_CW_PSA": "Yes",
        "In_S1": "Yes",
        "In_SC": "Yes",
    }
    assert _platforms_for_device(device) == {
        "asio",
        "connectwise_psa",
        "sentinelone",
        "screenconnect",
    }


def test_platforms_for_device_partial():
    device = {
        "In_ASIO": "Yes",
        "In_CW_PSA": "Yes",
        "In_S1": "No",
        "In_SC": "No",
    }
    assert _platforms_for_device(device) == {"asio", "connectwise_psa"}


def test_platforms_for_device_none():
    device = {
        "In_ASIO": "No",
        "In_CW_PSA": "No",
        "In_S1": "No",
        "In_SC": "No",
    }
    assert _platforms_for_device(device) == set()


# ── _classify_device ──────────────────────────────────────────────────────────

def test_classify_full_match():
    device = {
        "hostname": "PC-01",
        "In_ASIO": "Yes",
        "In_CW_PSA": "Yes",
        "In_S1": "Yes",
        "In_SC": "Yes",
        "endpoint_protection": "SentinelOne 25.1.4.434",
    }
    expected = {"asio", "connectwise_psa", "sentinelone", "screenconnect"}
    result = _classify_device(device, expected)
    assert result["match_type"] == "full"
    assert result["missing_platforms"] == []


def test_classify_partial_missing_one():
    """HEATHER-LAPTOP scenario: 3/4 platforms → should be partial, not full."""
    device = {
        "hostname": "HEATHER-LAPTOP",
        "In_ASIO": "Yes",
        "In_CW_PSA": "Yes",
        "In_S1": "Yes",
        "In_SC": "No",
        "endpoint_protection": "SentinelOne 25.1.4.434",
    }
    expected = {"asio", "connectwise_psa", "sentinelone", "screenconnect"}
    result = _classify_device(device, expected)
    assert result["match_type"] == "partial"
    assert "screenconnect" in result["missing_platforms"]


def test_classify_partial_missing_two():
    """LAPTOP-O577LVAU scenario: 2/4 platforms → should be partial."""
    device = {
        "hostname": "LAPTOP-O577LVAU",
        "In_ASIO": "Yes",
        "In_CW_PSA": "Yes",
        "In_S1": "No",
        "In_SC": "No",
        "endpoint_protection": "",
    }
    expected = {"asio", "connectwise_psa", "sentinelone", "screenconnect"}
    result = _classify_device(device, expected)
    assert result["match_type"] == "partial"
    assert "sentinelone" in result["missing_platforms"]
    assert "screenconnect" in result["missing_platforms"]


def test_classify_none():
    device = {
        "hostname": "GHOST-PC",
        "In_ASIO": "No",
        "In_CW_PSA": "No",
        "In_S1": "No",
        "In_SC": "No",
        "endpoint_protection": "",
    }
    expected = {"asio", "connectwise_psa", "sentinelone", "screenconnect"}
    result = _classify_device(device, expected)
    assert result["match_type"] == "none"
    assert set(result["missing_platforms"]) == expected


# ── reconcile_company ─────────────────────────────────────────────────────────

def test_reconcile_company_expected_platforms():
    result = reconcile_company("Globex Industries", GLOBEX_DEVICES)
    assert set(result["expected_platforms"]) == {
        "asio",
        "connectwise_psa",
        "sentinelone",
        "screenconnect",
    }


def test_reconcile_company_matched_count():
    """Only GLOBEX-WS-01 is fully matched; the other two are mismatched."""
    result = reconcile_company("Globex Industries", GLOBEX_DEVICES)
    assert result["matched_count"] == 1
    assert result["mismatched_count"] == 2


def test_reconcile_company_heather_laptop_is_mismatched():
    """HEATHER-LAPTOP (missing SC) must be in mismatched, not matched."""
    result = reconcile_company("Globex Industries", GLOBEX_DEVICES)
    mismatched_hostnames = [d["hostname"] for d in result["mismatched"]]
    assert "HEATHER-LAPTOP" in mismatched_hostnames
    matched_hostnames = [d["hostname"] for d in result["matched"]]
    assert "HEATHER-LAPTOP" not in matched_hostnames


def test_reconcile_company_laptop_o577_is_mismatched():
    """LAPTOP-O577LVAU (missing S1 and SC) must be in mismatched."""
    result = reconcile_company("Globex Industries", GLOBEX_DEVICES)
    mismatched_hostnames = [d["hostname"] for d in result["mismatched"]]
    assert "LAPTOP-O577LVAU" in mismatched_hostnames


def test_reconcile_company_version_preserved():
    """
    Version strings from endpoint_protection must survive reconciliation.
    (FIX for "version numbers not displaying on Breakdown page.")
    """
    result = reconcile_company("Globex Industries", GLOBEX_DEVICES)
    all_devices = result["matched"] + result["mismatched"]
    by_hostname = {d["hostname"]: d for d in all_devices}

    # Devices with S1 installed should carry the version string
    assert by_hostname["GLOBEX-WS-01"]["endpoint_protection"] == "SentinelOne 25.1.4.434"
    assert by_hostname["HEATHER-LAPTOP"]["endpoint_protection"] == "SentinelOne 25.1.4.434"

    # Device without S1 should have an empty string (not None or missing key)
    assert by_hostname["LAPTOP-O577LVAU"]["endpoint_protection"] == ""


# ── reconcile_all ─────────────────────────────────────────────────────────────

def test_reconcile_all_groups_by_company():
    devices = [
        {
            "hostname": "A-PC",
            "company": "Alpha",
            "In_ASIO": "Yes",
            "In_CW_PSA": "Yes",
            "In_S1": "Yes",
            "In_SC": "Yes",
            "endpoint_protection": "SentinelOne 25.1.4.434",
        },
        {
            "hostname": "B-PC",
            "company": "Beta",
            "In_ASIO": "Yes",
            "In_CW_PSA": "Yes",
            "In_S1": "Yes",
            "In_SC": "Yes",
            "endpoint_protection": "SentinelOne 25.1.4.434",
        },
    ]
    results = reconcile_all(devices)
    company_names = [r["company"] for r in results]
    assert "Alpha" in company_names
    assert "Beta" in company_names
    assert len(results) == 2


def test_reconcile_all_total_counts():
    """Total matched/mismatched across all companies for sample Globex data."""
    results = reconcile_all(GLOBEX_DEVICES)
    assert len(results) == 1
    assert results[0]["matched_count"] == 1
    assert results[0]["mismatched_count"] == 2
