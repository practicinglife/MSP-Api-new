"""Unit tests for device matching service."""


from app.models.device import DeviceRecord
from app.services.device_matcher import (
    AUTO_LINK_THRESHOLD,
    DeviceMatchStatus,
    match_device,
)


def _device(
    platform: str,
    pid: str,
    hostname: str = "",
    serial: str = "",
    macs: list | None = None,
) -> DeviceRecord:
    return DeviceRecord(
        platform=platform,
        platform_id=pid,
        hostname=hostname,
        serial_number=serial,
        mac_addresses=macs or [],
    )


class TestMatchDevice:
    """Tests for match_device()."""

    def test_serial_exact_gives_auto_link(self) -> None:
        source = _device("avast", "1", serial="SN12345")
        candidate = _device("eset", "10", serial="SN12345")
        result = match_device(source, [candidate])
        assert result.status == DeviceMatchStatus.AUTO_LINKED
        assert result.score >= AUTO_LINK_THRESHOLD
        assert "serial_exact" in result.reasons

    def test_mac_and_hostname_gives_auto_link(self) -> None:
        mac = "AABBCCDDEEFF"
        source = _device("avast", "1", hostname="SERVER01", macs=[mac])
        candidate = _device("eset", "10", hostname="SERVER01", macs=[mac])
        result = match_device(source, [candidate])
        assert result.status == DeviceMatchStatus.AUTO_LINKED
        assert result.score >= AUTO_LINK_THRESHOLD

    def test_mac_only_gives_needs_review(self) -> None:
        mac = "AABBCCDDEEFF"
        source = _device("avast", "1", hostname="HOST_A", macs=[mac])
        candidate = _device("eset", "10", hostname="HOST_B", macs=[mac])
        result = match_device(source, [candidate])
        assert result.status == DeviceMatchStatus.NEEDS_REVIEW
        assert result.score == 60
        assert "mac_overlap" in result.reasons

    def test_hostname_only_gives_needs_review(self) -> None:
        source = _device("avast", "1", hostname="SERVER01")
        candidate = _device("eset", "10", hostname="SERVER01")
        result = match_device(source, [candidate])
        assert result.status == DeviceMatchStatus.NEEDS_REVIEW
        assert result.score == 30
        assert "hostname_exact" in result.reasons

    def test_no_match_gives_unmatched(self) -> None:
        source = _device("avast", "1", hostname="ALPHA", serial="AAA111", macs=["111111111111"])
        candidate = _device("eset", "10", hostname="BETA", serial="BBB222", macs=["222222222222"])
        result = match_device(source, [candidate])
        assert result.status == DeviceMatchStatus.UNMATCHED
        assert result.target is None

    def test_placeholder_serial_not_matched(self) -> None:
        source = _device("avast", "1", serial="NONE")
        candidate = _device("eset", "10", serial="NONE")
        result = match_device(source, [candidate])
        # NONE is in blocklist so serial match should not fire
        assert "serial_exact" not in result.reasons

    def test_zeros_serial_not_matched(self) -> None:
        source = _device("avast", "1", serial="00000000")
        candidate = _device("eset", "10", serial="00000000")
        result = match_device(source, [candidate])
        assert "serial_exact" not in result.reasons

    def test_does_not_match_self(self) -> None:
        source = _device("avast", "1", hostname="HOST", serial="SN999")
        result = match_device(source, [source])
        assert result.status == DeviceMatchStatus.UNMATCHED

    def test_empty_candidate_list(self) -> None:
        source = _device("avast", "1", serial="SN001")
        result = match_device(source, [])
        assert result.status == DeviceMatchStatus.UNMATCHED
