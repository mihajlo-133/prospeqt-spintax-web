"""Integration tests for the /health endpoint.

GET /health is the canonical liveness check used by:
- Render: to decide if the container is healthy
- UptimeRobot: 5-min keepalive ping (prevents cold starts on free tier)

All three assertions must hold simultaneously:
1. HTTP 200 (not 204, not 301)
2. Body is exactly {"status": "ok"}  - no extra keys
3. Content-Type starts with "application/json"

Using the shared TestClient fixture from conftest.py.
"""

import json


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------


def test_health_returns_200(client):
    """GET /health must return HTTP 200."""
    response = client.get("/health")
    assert response.status_code == 200, (
        f"Expected 200, got {response.status_code}. Body: {response.text}"
    )


def test_health_returns_status_ok(client):
    """GET /health body must be exactly {\"status\": \"ok\"}."""
    response = client.get("/health")
    body = response.json()
    assert body == {"status": "ok"}, (
        f'Expected {{"status": "ok"}}, got {json.dumps(body)}. '
        "Do not add extra keys to the health response - UptimeRobot only checks HTTP status, "
        "but we pin the shape here so future changes don't silently break contract."
    )


def test_health_content_type_is_json(client):
    """GET /health must return Content-Type: application/json."""
    response = client.get("/health")
    content_type = response.headers.get("content-type", "")
    assert content_type.startswith("application/json"), (
        f"Expected content-type starting with 'application/json', got '{content_type}'."
    )
