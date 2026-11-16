"""Smoke tests for the API scaffold."""

from fastapi.testclient import TestClient

from casapi.main import app


def test_health() -> None:
    """Verify the public health endpoint."""
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
