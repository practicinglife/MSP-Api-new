"""Tests for the overrides admin API endpoints."""




class TestCompanyOverrideAPI:
    """Tests for /admin/overrides/companies endpoints."""

    def test_create_company_override(self, test_client) -> None:  # type: ignore[no-untyped-def]
        """POST /admin/overrides/companies should create and return override."""
        payload = {
            "platform_a": "avast",
            "platform_id_a": "a1",
            "platform_b": "eset",
            "platform_id_b": "e1",
            "note": "manual link",
        }
        response = test_client.post("/admin/overrides/companies", json=payload)
        assert response.status_code == 201
        data = response.json()
        assert data["platform_a"] == "avast"
        assert data["platform_id_a"] == "a1"
        assert data["platform_b"] == "eset"
        assert data["note"] == "manual link"
        assert "id" in data

    def test_list_company_overrides(self, test_client) -> None:  # type: ignore[no-untyped-def]
        """GET /admin/overrides/companies should list created overrides."""
        payload = {
            "platform_a": "avast",
            "platform_id_a": "x1",
            "platform_b": "eset",
            "platform_id_b": "y1",
        }
        test_client.post("/admin/overrides/companies", json=payload)
        response = test_client.get("/admin/overrides/companies")
        assert response.status_code == 200
        items = response.json()
        assert isinstance(items, list)
        assert len(items) >= 1

    def test_company_override_missing_fields(self, test_client) -> None:  # type: ignore[no-untyped-def]
        """POST with missing required fields should return 422."""
        response = test_client.post("/admin/overrides/companies", json={"platform_a": "avast"})
        assert response.status_code == 422


class TestDeviceOverrideAPI:
    """Tests for /admin/overrides/devices endpoints."""

    def test_create_device_override(self, test_client) -> None:  # type: ignore[no-untyped-def]
        """POST /admin/overrides/devices should create and return override."""
        payload = {
            "platform_a": "sentinelone",
            "platform_id_a": "s1",
            "platform_b": "avast",
            "platform_id_b": "av1",
            "note": "device link",
        }
        response = test_client.post("/admin/overrides/devices", json=payload)
        assert response.status_code == 201
        data = response.json()
        assert data["platform_a"] == "sentinelone"

    def test_list_device_overrides(self, test_client) -> None:  # type: ignore[no-untyped-def]
        """GET /admin/overrides/devices should return list."""
        response = test_client.get("/admin/overrides/devices")
        assert response.status_code == 200
        assert isinstance(response.json(), list)
