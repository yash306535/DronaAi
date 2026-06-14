"""Tests for the app factory hardening middleware (task 7.1).

Covers the security-middleware acceptance criteria of Requirement 15:

- 15.5: CORS allowlist — an allowlisted Origin gets cross-origin access
  headers; a non-allowlisted Origin does not.
- 15.3: a request body over ``MAX_BODY_BYTES`` is rejected with a 422 carrying
  the standard error envelope.
- 15.6: the login endpoint is capped at 5 requests per source IP within a
  rolling 60-second window; the 6th gets a 429 with the standard envelope.

The app is built via :func:`app.main.create_app` so each test gets an isolated
instance (its own rate limiter). Required secrets are injected via env so
``get_settings`` validation passes, and ``FRONTEND_ORIGINS`` / ``MAX_BODY_BYTES``
are set per test to exercise the middlewares deterministically.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.ratelimit import SlidingWindowRateLimiter
from app.main import (
    BODY_TOO_LARGE_CODE,
    RATE_LIMITED_CODE,
    create_app,
)

SECRET_VALUE = "super-secret-jwt-value-for-main-app-tests-1234567890"
API_KEY_VALUE = "sk-openai-secret-key-for-main-app-tests"

ALLOWED_ORIGIN = "https://drona.example.com"
DENIED_ORIGIN = "https://evil.example.com"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Deterministic settings: known secrets, a single-origin allowlist, 1KB body."""
    monkeypatch.setenv("JWT_SECRET", SECRET_VALUE)
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY_VALUE)
    monkeypatch.setenv("FRONTEND_ORIGINS", ALLOWED_ORIGIN)
    monkeypatch.setenv("MAX_BODY_BYTES", "1024")
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture()
def client() -> TestClient:
    """A TestClient over a fresh app instance (no lifespan side effects needed)."""
    app = create_app()
    # ``raise_server_exceptions=False`` lets envelope responses surface as-is.
    return TestClient(app, raise_server_exceptions=False)


# --- 15.5: CORS allowlist ---------------------------------------------------


def test_cors_allows_allowlisted_origin(client: TestClient) -> None:
    """15.5: an exactly-matching Origin receives cross-origin access headers."""
    resp = client.get("/health", headers={"Origin": ALLOWED_ORIGIN})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN


def test_cors_denies_non_allowlisted_origin(client: TestClient) -> None:
    """15.5: a non-allowlisted Origin gets no cross-origin access header."""
    resp = client.get("/health", headers={"Origin": DENIED_ORIGIN})
    # The request itself still completes server-side, but the browser-enforced
    # access header is omitted for a disallowed origin.
    assert "access-control-allow-origin" not in {
        k.lower() for k in resp.headers.keys()
    }


def test_cors_preflight_denied_for_non_allowlisted_origin(client: TestClient) -> None:
    """15.5: a preflight from a denied origin is not granted access headers."""
    resp = client.options(
        "/api/v1/auth/login",
        headers={
            "Origin": DENIED_ORIGIN,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in {
        k.lower() for k in resp.headers.keys()
    }


# --- 15.3: body-size limit --------------------------------------------------


def test_body_over_limit_is_422_envelope(client: TestClient) -> None:
    """15.3: a body over MAX_BODY_BYTES is rejected with a 422 envelope."""
    oversized = "x" * 2048  # MAX_BODY_BYTES is 1024 in this test
    resp = client.post(
        "/api/v1/auth/login",
        content=oversized,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == BODY_TOO_LARGE_CODE
    assert body["error"]["requestId"]


def test_body_under_limit_passes_through(client: TestClient) -> None:
    """A small body is not blocked by the size limit (reaches validation)."""
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "user@example.com", "password": "password123"},
    )
    # The body-size middleware lets it through; the route then proceeds to
    # credential/DB handling. The key assertion is that it is NOT a 422
    # body-too-large rejection from the size middleware.
    assert resp.status_code != 422


# --- 15.6: login rate limiting ----------------------------------------------


def test_login_rate_limited_after_five_requests(client: TestClient) -> None:
    """15.6: a 6th login from the same IP in the window gets a 429 envelope."""
    payload = {"email": "user@example.com", "password": "password123"}
    # The first five requests are within the per-IP window (not rate-limited).
    for _ in range(5):
        resp = client.post("/api/v1/auth/login", json=payload)
        assert resp.status_code != 429
    # The sixth exceeds 5/IP per rolling 60s and is rejected with the envelope.
    sixth = client.post("/api/v1/auth/login", json=payload)
    assert sixth.status_code == 429
    body = sixth.json()
    assert body["error"]["code"] == RATE_LIMITED_CODE
    assert body["error"]["requestId"]


# --- security headers -------------------------------------------------------


def test_security_headers_present(client: TestClient) -> None:
    """Defensive security headers are attached to responses."""
    resp = client.get("/health")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"


# --- rate limiter unit (per-session escalate keying) ------------------------


def test_sliding_window_limiter_blocks_excess() -> None:
    """The limiter allows exactly ``max_requests`` then blocks within window."""
    fake_time = [1000.0]
    limiter = SlidingWindowRateLimiter(
        max_requests=5, window_seconds=60.0, time_func=lambda: fake_time[0]
    )
    for _ in range(5):
        assert limiter.check("k") is True
    assert limiter.check("k") is False
    # Advancing past the window resets the rolling count.
    fake_time[0] += 61.0
    assert limiter.check("k") is True
