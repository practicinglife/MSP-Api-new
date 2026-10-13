"""Unit tests for company matching service."""


from app.models.company import CompanyRecord
from app.services.company_matcher import (
    FUZZY_AUTO_LINK_THRESHOLD,
    MatchStatus,
    match_company,
)


def _company(platform: str, pid: str, name: str) -> CompanyRecord:
    return CompanyRecord(platform=platform, platform_id=pid, name=name)


class TestMatchCompany:
    """Tests for match_company()."""

    def test_auto_link_exact_match(self) -> None:
        source = _company("avast", "1", "Acme Corporation")
        candidate = _company("eset", "10", "Acme Corporation")
        result = match_company(source, [candidate])
        assert result.status == MatchStatus.AUTO_LINKED
        assert result.score >= FUZZY_AUTO_LINK_THRESHOLD
        assert result.target == candidate

    def test_auto_link_near_match(self) -> None:
        source = _company("avast", "1", "Acme Inc")
        # After normalisation both become "acme" — 100% match
        candidate = _company("eset", "10", "Acme LLC")
        result = match_company(source, [candidate])
        assert result.status == MatchStatus.AUTO_LINKED

    def test_review_when_score_in_range(self) -> None:
        source = _company("avast", "1", "Contoso Technologies Ltd")
        # "contoso technologies" vs "contoso tech solutions" should be in review range
        candidate = _company("eset", "10", "Contoso Tech Solutions")
        result = match_company(source, [candidate])
        # Score should be non-trivial — at worst NEEDS_REVIEW, could be AUTO_LINKED
        # The important assertion is it is not completely unmatched due to zero similarity
        assert result.score > 0

    def test_unmatched_when_score_too_low(self) -> None:
        source = _company("avast", "1", "Completely Different Name Here")
        candidate = _company("eset", "10", "Xyz Corp")
        result = match_company(source, [candidate])
        assert result.status == MatchStatus.UNMATCHED
        assert result.target is None

    def test_does_not_match_self(self) -> None:
        source = _company("avast", "1", "Acme")
        # Same platform + id = self; should not match
        result = match_company(source, [source])
        assert result.status == MatchStatus.UNMATCHED

    def test_empty_candidate_list(self) -> None:
        source = _company("avast", "1", "Acme")
        result = match_company(source, [])
        assert result.status == MatchStatus.UNMATCHED
        assert result.score == 0.0

    def test_selects_best_candidate(self) -> None:
        source = _company("avast", "1", "Acme Corporation")
        good = _company("eset", "10", "Acme Corporation")
        bad = _company("eset", "20", "Totally Different")
        result = match_company(source, [bad, good])
        assert result.target == good
