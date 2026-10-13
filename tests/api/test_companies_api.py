"""Tests for companies API endpoints."""




class TestCompaniesAPI:
    """Tests for /api/companies endpoints."""

    def test_list_companies_empty(self, test_client) -> None:  # type: ignore[no-untyped-def]
        """GET /api/companies should return an empty list initially."""
        response = test_client.get("/api/companies")
        assert response.status_code == 200
        assert response.json() == []

    def test_get_company_not_found(self, test_client) -> None:  # type: ignore[no-untyped-def]
        """GET /api/companies/{id} should return 404 for unknown ID."""
        response = test_client.get("/api/companies/nonexistent-id")
        assert response.status_code == 404
