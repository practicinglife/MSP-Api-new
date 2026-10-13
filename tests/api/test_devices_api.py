"""Tests for devices API endpoints."""




class TestDevicesAPI:
    """Tests for /api/devices endpoints."""

    def test_list_devices_empty(self, test_client) -> None:  # type: ignore[no-untyped-def]
        """GET /api/devices should return an empty list initially."""
        response = test_client.get("/api/devices")
        assert response.status_code == 200
        assert response.json() == []

    def test_get_device_not_found(self, test_client) -> None:  # type: ignore[no-untyped-def]
        """GET /api/devices/{id} should return 404 for unknown ID."""
        response = test_client.get("/api/devices/nonexistent-id")
        assert response.status_code == 404
