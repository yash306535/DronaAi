"""Full create_app + lifespan integration test (task 7.3).

Where ``test_main_app.py`` exercises the hardening middleware against a bare
app (no lifespan side effects), this module boots the application end to end
through its lifespan context — ``with TestClient(app) as client:`` runs the
startup/shutdown handlers — and asserts the startup *wiring* guarantees plus a
smoke pass over the security hardening:

- **Req 11.1**: startup subscribes the orchestrator's agent handlers onto the
  event bus *before* any event is published. After lifespan startup,
  ``app.state.orchestrator`` is wired (``is_wired``) and ``app.state.event_bus``
  carries a subscription for every event type the agents declare, so the
  "subscribe before publish" ordering holds. The WebSocket heartbeat loop is
  also started (``app.state.ws_manager``).
- **Req 15.5**: an allowlisted Origin receives cross-origin access headers; a
  denied Origin does not.
- **Req 15.3**: a request body over ``MAX_BODY_BYTES`` is rejected with a 422
  carrying the standard error envelope.
- **Req 15.6**: the login endpoint is capped at 5 requests per source IP in a
  rolling 60-second window; the 6th gets a 429 with the standard envelope.

Required secrets (``JWT_SECRET`` / ``OPENAI_API_KEY``) are injected via env so
``get_settings`` validation passes during ``create_app``; ``FRONTEND_ORIGINS``
and ``MAX_BODY_BYTES`` are pinned so the middleware behaviour is deterministic.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.agents.orchestrator import AGENT_SPECS
from app.core.config import Settings, get_settings
from app.main import (
    BODY_TOO_LARGE_CODE,
    RATE_LIMITED_CODE,
    create_app,
)

SECRET_VALUE = "super-secret-jwt-value-for-startup-integration-1234567890"
API_KEY_VALUE = "sk-openai-secret-key-for-startup-integration-tests"

ALLOWED_ORIGIN = "https://drona.example.com"
DENIED_ORIGIN = "https://evil.example.com"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Deterministic settings: known secrets, single-origin allowlist, 1KB body."""
    monkeypatch.setenv("JWT_SECRET", SECRET_VALUE)
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY_VALUE)
    monkeypatch.setenv("FRONTEND_ORIGINS", ALLOWED_ORIGIN)
    monkeypatch.setenv("MAX_BODY_BYTES", "1024")
    # Don't read a developer's local .env during the test run.
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture(autouse=True)
def _isolate_root_logging() -> Iterator[None]:
    """Snapshot and restore root logger state around each test.

    Lifespan startup calls ``configure_logging()`` which mutates the *global*
    root logger (raises its level to INFO and installs a JSON handler). Without
    isolation that level change leaks into later tests, where ``create_app``'s
    ``router.skip`` INFO log (whose ``extra`` carries the reserved ``module``
    key) would then actually emit and raise. Saving and restoring the root
    logger's level and handlers keeps each test independent without touching
    application source.
    """
    root = logging.getLogger()
    saved_level = root.level
    saved_handlers = list(root.handlers)
    try:
        yield
    finally:
        root.setLevel(saved_level)
        root.handlers[:] = saved_handlers


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """A TestClient that runs the app's lifespan (startup wiring + heartbeat).

    Entering the ``with`` block triggers the lifespan startup; leaving it runs
    the shutdown handler (stops the heartbeat, releases sockets).
    """
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


# --- Req 11.1: startup wiring (subscribe before publish) --------------------


def test_startup_wires_orchestrator_onto_event_bus(client: TestClient) -> None:
    """11.1: after startup the orchestrator is wired onto the event bus."""
    app = client.app
    orchestrator = app.state.orchestrator
    # The orchestrator was built and wired during lifespan startup.
    assert orchestrator is not None
    assert orchestrator.is_wired is True


def test_startup_subscribes_every_agent_event_type_before_publish(
    client: TestClient,
) -> None:
    """11.1: every agent's declared event type has a subscription at startup.

    Wiring happens in lifespan startup, before any route can publish an event,
    so by the time the client can issue a request the subscriptions already
    exist for each event type the agents declare.
    """
    bus = client.app.state.event_bus
    assert bus is not None

    declared_event_types = {
        event_type for spec in AGENT_SPECS for event_type in spec.subscribes
    }
    # The bus tracks handlers per event type; each declared type must be wired.
    subscribed_types = set(bus._subs.keys())  # noqa: SLF001 - test introspection
    missing = declared_event_types - subscribed_types
    assert not missing, f"event types wired with no subscriber: {missing}"
    for event_type in declared_event_types:
        assert bus._subs[event_type], f"no handler subscribed for {event_type}"  # noqa: SLF001


def test_startup_wires_ws_bridge_for_dashboard_feed(client: TestClient) -> None:
    """11.5/12.3/12.6: the event-bus → WebSocket bridge is subscribed at startup.

    The bridge forwards the dashboard-facing bus events (``agent.message``,
    ``session.update``, ``report.ready``) onto the live WebSocket rooms so the
    admin dashboard actually receives the inter-agent feed and tile updates.
    After startup it is held on ``app.state.ws_bridge`` and the bus carries a
    subscription for each bridged event type.
    """
    from app.core.events import EventType

    app = client.app
    assert app.state.ws_bridge is not None

    bus = app.state.event_bus
    for event_type in (
        EventType.AGENT_MESSAGE,
        EventType.SESSION_UPDATE,
        EventType.REPORT_READY,
    ):
        assert bus._subs.get(event_type), (  # noqa: SLF001 - test introspection
            f"ws bridge not subscribed for {event_type}"
        )


def test_startup_starts_ws_heartbeat(client: TestClient) -> None:
    """11.1 (startup side effects): the WebSocket heartbeat loop is running."""
    ws_manager = client.app.state.ws_manager
    assert ws_manager is not None
    # The heartbeat task is created during startup and still live mid-lifespan.
    assert ws_manager._heartbeat_task is not None  # noqa: SLF001
    assert not ws_manager._heartbeat_task.done()  # noqa: SLF001


def test_health_route_served_under_lifespan(client: TestClient) -> None:
    """A booted app serves its liveness probe (end-to-end smoke)."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --- Req 15.5: CORS allowlist (smoke over the booted app) -------------------


def test_cors_allows_allowlisted_origin(client: TestClient) -> None:
    """15.5: an exactly-matching Origin receives cross-origin access headers."""
    resp = client.get("/health", headers={"Origin": ALLOWED_ORIGIN})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN


def test_cors_denies_non_allowlisted_origin(client: TestClient) -> None:
    """15.5: a non-allowlisted Origin gets no cross-origin access header."""
    resp = client.get("/health", headers={"Origin": DENIED_ORIGIN})
    assert "access-control-allow-origin" not in {
        k.lower() for k in resp.headers.keys()
    }


# --- Req 15.3: body-size limit ----------------------------------------------


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


# --- Req 15.6: login rate limiting ------------------------------------------


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
