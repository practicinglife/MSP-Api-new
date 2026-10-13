"""Unit tests for field normalisation utilities."""


from app.services.normalizers import (
    normalize_company_name,
    normalize_hostname,
    normalize_mac,
    normalize_serial,
)


class TestNormalizeCompanyName:
    """Tests for normalize_company_name()."""

    def test_strips_inc_suffix(self) -> None:
        assert normalize_company_name("Acme Inc") == "acme"

    def test_strips_ltd_suffix(self) -> None:
        assert normalize_company_name("Acme Ltd") == "acme"

    def test_strips_llc_suffix(self) -> None:
        assert normalize_company_name("Acme LLC") == "acme"

    def test_strips_corp_suffix(self) -> None:
        assert normalize_company_name("Acme Corp") == "acme"

    def test_strips_limited_suffix(self) -> None:
        assert normalize_company_name("Acme Limited") == "acme"

    def test_lowercases(self) -> None:
        assert normalize_company_name("Hello World") == "hello world"

    def test_strips_punctuation(self) -> None:
        result = normalize_company_name("Acme, Inc.")
        assert "," not in result
        assert "." not in result

    def test_collapses_whitespace(self) -> None:
        assert normalize_company_name("  Foo   Bar  ") == "foo bar"

    def test_empty_string(self) -> None:
        assert normalize_company_name("") == ""

    def test_whitespace_only(self) -> None:
        assert normalize_company_name("   ") == ""

    def test_mixed_case_suffix(self) -> None:
        result = normalize_company_name("Widgets INC")
        assert "inc" not in result


class TestNormalizeHostname:
    """Tests for normalize_hostname()."""

    def test_strips_fqdn(self) -> None:
        assert normalize_hostname("host.example.com") == "HOST"

    def test_uppercases(self) -> None:
        assert normalize_hostname("myserver") == "MYSERVER"

    def test_empty_string(self) -> None:
        assert normalize_hostname("") == ""

    def test_single_label(self) -> None:
        assert normalize_hostname("PC001") == "PC001"

    def test_multi_part_fqdn(self) -> None:
        assert normalize_hostname("laptop.corp.internal.example.org") == "LAPTOP"


class TestNormalizeMac:
    """Tests for normalize_mac()."""

    def test_colons(self) -> None:
        assert normalize_mac("AA:BB:CC:DD:EE:FF") == "AABBCCDDEEFF"

    def test_dashes(self) -> None:
        assert normalize_mac("AA-BB-CC-DD-EE-FF") == "AABBCCDDEEFF"

    def test_no_separators(self) -> None:
        assert normalize_mac("aabbccddeeff") == "AABBCCDDEEFF"

    def test_uppercase(self) -> None:
        assert normalize_mac("aa:bb:cc:dd:ee:ff") == "AABBCCDDEEFF"

    def test_empty_string(self) -> None:
        assert normalize_mac("") == ""


class TestNormalizeSerial:
    """Tests for normalize_serial()."""

    def test_strips_hyphens(self) -> None:
        assert normalize_serial("ABC-123") == "ABC123"

    def test_strips_spaces(self) -> None:
        assert normalize_serial("ABC 123") == "ABC123"

    def test_uppercase(self) -> None:
        assert normalize_serial("abc123") == "ABC123"

    def test_empty_string_is_blocked(self) -> None:
        assert normalize_serial("") == ""

    def test_none_is_blocked(self) -> None:
        assert normalize_serial("NONE") == ""

    def test_zeros_are_blocked(self) -> None:
        assert normalize_serial("00000000") == ""

    def test_to_be_filled_is_blocked(self) -> None:
        assert normalize_serial("TO BE FILLED") == ""

    def test_default_is_blocked(self) -> None:
        assert normalize_serial("DEFAULT") == ""

    def test_na_is_blocked(self) -> None:
        assert normalize_serial("N/A") == ""

    def test_valid_serial(self) -> None:
        result = normalize_serial("SN12345XYZ")
        assert result == "SN12345XYZ"

    def test_whitespace_only_returns_empty(self) -> None:
        assert normalize_serial("   ") == ""
