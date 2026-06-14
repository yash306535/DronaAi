"""FastAPI application factory, lifespan, and security middleware.

This module assembles the DRONA AI HTTP/WebSocket application from the
cross-cutting infrastructure built in earlier tasks and hardens it per
Requirement 15:

- **Lifespan** (startup/shutdown): configure structured JSON logging
  (:func:`app.core.logging.configure_logging`), initialize the database schema
  (:func:`app.core.db.init_db`), build the in-process :class:`~app.core.events.EventBus`
  and wire the agent :class:`Orchestrator` onto it (subscribing every agent's
  handlers *before* any event is published, Requirement 11.1), and start the
  WebSocket manager heartbeat (:meth:`WebSocketManager.start_heartbeat`). On
  shutdown the heartbeat is stopped and live sockets are released. The
  orchestrator import is guarded so the app still starts before task 7.2 lands.
- **Request correlation** (:class:`~app.core.logging.RequestIdMiddleware`) and
  the **standard error envelope** handlers
  (:func:`app.core.errors.register_error_handlers`) — Requirements 15.3, 15.4.
- **CORS allowlist** (Requirement 15.5): cross-origin access is permitted only
  for origins that exactly match ``settings.FRONTEND_ORIGINS`` (a comma-separated
  allowlist); any other origin receives no cross-origin access headers.
- **Body-size limit** (Requirement 15.3): a request body larger than
  ``settings.MAX_BODY_BYTES`` (default 1 MB) is rejected with a 422 carrying the
  standard error envelope.
- **Security headers**: defensive response headers (``X-Content-Type-Options``,
  ``X-Frame-Options``, ``Referrer-Policy``, ...) on every response.
- **Rate limiting** (Requirement 15.6): the login and proctoring-escalation
  endpoints are capped at 5 requests per source IP and 5 per session within any
  rolling 60-second window; excess requests get a 429 with the standard
  envelope. Matching is by path so it applies as soon as the (later-mounted)
  escalate route exists.

The module exposes both :func:`create_app` (the factory, used by tests so each
gets an isolated app) and a module-level ``app`` instance for ``uvicorn
app.main:app``.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import Settings, get_settings
from app.core.db import init_db
from app.core.errors import build_error_envelope, register_error_handlers
from app.core.events import EventBus
from app.core.logging import (
    RequestIdMiddleware,
    configure_logging,
    get_logger,
    get_or_create_request_id,
)
from app.core.ratelimit import (
    DEFAULT_MAX_REQUESTS,
    DEFAULT_WINDOW_SECONDS,
    SlidingWindowRateLimiter,
)
from app.core.ws import get_ws_manager

logger = get_logger("app.main")

# REST routers are versioned under this prefix (design "API Surface (REST)").
# WebSocket routes mount at the root since they declare their own ``/ws/...``.
API_V1_PREFIX = "/api/v1"

# Error codes for the hardening middlewares (kept stable for clients).
BODY_TOO_LARGE_CODE = "payload_too_large"
RATE_LIMITED_CODE = "rate_limited"

# Path fragments used to apply per-endpoint rate limiting (Requirement 15.6).
# Matching is by path so the limit applies regardless of the mount prefix and
# works for the escalate route as soon as it is mounted (it may not exist yet).
_LOGIN_PATH_SUFFIX = "/auth/login"
_ESCALATE_RE = re.compile(r"/proctoring/(?P<session_id>[^/]+)/escalate/?$")


def _safe_request_id(request: Request) -> str:
    """Return the correlation id for ``request`` (set by RequestIdMiddleware)."""
    state_id = getattr(request.state, "request_id", None)
    if state_id:
        return str(state_id)
    return get_or_create_request_id()


def _envelope_response(
    request: Request, *, code: str, message: str, status_code: int
) -> JSONResponse:
    """Render the standard ``{"error": {...}}`` envelope for a middleware reject."""
    request_id = _safe_request_id(request)
    return JSONResponse(
        status_code=status_code,
        content=build_error_envelope(code, message, request_id),
        headers={"X-Request-ID": request_id},
    )


# --- Security headers -------------------------------------------------------

# Conservative, broadly-compatible defaults. These are safe for a JSON API and
# the SPA dashboard; they do not assume HTTPS-only beyond HSTS (which browsers
# ignore over plain HTTP).
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Permitted-Cross-Domain-Policies": "none",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach defensive security headers to every response."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


# --- Body-size limit (Requirement 15.3) -------------------------------------


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject request bodies larger than ``max_body_bytes`` with a 422 (15.3).

    The check uses the declared ``Content-Length``; a request whose declared
    body exceeds the limit is rejected before the route runs, carrying the
    standard error envelope.
    """

    def __init__(self, app, *, max_body_bytes: int) -> None:
        super().__init__(app)
        self._max = max_body_bytes

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = None
            if declared is not None and declared > self._max:
                return _envelope_response(
                    request,
                    code=BODY_TOO_LARGE_CODE,
                    message=(
                        "Request body exceeds the maximum accepted size of "
                        f"{self._max} bytes."
                    ),
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
        return await call_next(request)


# --- Rate limiting (Requirement 15.6) ---------------------------------------


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Cap login + escalation requests per IP and per session (15.6).

    The login endpoint and the proctoring escalation endpoint are limited to 5
    requests per source IP and 5 requests per session within any rolling
    60-second window; excess requests receive a 429 with the standard envelope.
    Only the targeted paths are gated; all other traffic passes through. The
    per-IP and per-session windows are tracked independently, so hitting either
    cap rejects the request.
    """

    def __init__(self, app, *, limiter: SlidingWindowRateLimiter) -> None:
        super().__init__(app)
        self._limiter = limiter

    def _keys_for(self, request: Request) -> list[str] | None:
        """Return the rate-limit keys to enforce, or ``None`` if not gated.

        Login is keyed by source IP only (it has no session yet). Escalation is
        keyed by both source IP and the session id taken from the path.
        """
        if request.method != "POST":
            return None
        path = request.url.path
        client_ip = request.client.host if request.client else "unknown"

        if path.endswith(_LOGIN_PATH_SUFFIX):
            return [f"login:ip:{client_ip}"]

        match = _ESCALATE_RE.search(path)
        if match:
            session_id = match.group("session_id")
            return [
                f"escalate:ip:{client_ip}",
                f"escalate:session:{session_id}",
            ]
        return None

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        keys = self._keys_for(request)
        if keys:
            # Evaluate every key so either the per-IP or per-session window can
            # trip the limit (Requirement 15.6).
            allowed = True
            for key in keys:
                if not self._limiter.check(key):
                    allowed = False
            if not allowed:
                logger.warning(
                    "rate_limit.rejected",
                    extra={"path": request.url.path, "method": request.method},
                )
                return _envelope_response(
                    request,
                    code=RATE_LIMITED_CODE,
                    message=(
                        "Too many requests. Please retry after the rate-limit "
                        "window resets."
                    ),
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                )
        return await call_next(request)


# --- CORS allowlist ---------------------------------------------------------


def _parse_origins(raw: str) -> list[str]:
    """Split the comma-separated ``FRONTEND_ORIGINS`` allowlist (15.5)."""
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


# --- Router mounting --------------------------------------------------------


def _mount_routers(app: FastAPI) -> None:
    """Mount the available routers, guarding ones not built yet.

    The auth router and WebSocket routes always exist. The exam, session,
    proctoring, and analytics routers are mounted when present and skipped
    (with a TODO log) otherwise so the app still starts during the build-out.
    """
    # Always-present routers.
    from app.api.auth import router as auth_router
    from app.api.ws_routes import router as ws_router

    app.include_router(auth_router, prefix=API_V1_PREFIX)
    # WS routes declare their own ``/ws/...`` paths; mount at the root.
    app.include_router(ws_router)

    # Optional routers that arrive in later tasks. Guard each import/mount so a
    # missing module never blocks startup.
    # TODO(tasks 9, 10, 12, 21): drop the guards once these routers exist.
    for module_name, attr in (
        ("app.api.exams", "router"),
        ("app.api.sessions", "router"),
        ("app.api.proctoring", "router"),
        ("app.api.analytics", "router"),
    ):
        try:
            module = __import__(module_name, fromlist=[attr])
            router = getattr(module, attr)
        except Exception:  # noqa: BLE001 - the module may not exist yet
            logger.info("router.skip", extra={"module": module_name})
            continue
        app.include_router(router, prefix=API_V1_PREFIX)
        logger.info("router.mounted", extra={"module": module_name})


def _wire_orchestrator(app: FastAPI, bus: EventBus) -> None:
    """Build the orchestrator and subscribe agent handlers before any publish.

    Guarded so the app still starts if ``app.agents.orchestrator`` is not yet
    present or is incomplete (task 7.2). Requirement 11.1: handlers are wired at
    startup before any event is published.
    """
    try:
        from app.agents.orchestrator import Orchestrator  # type: ignore
    except Exception:  # noqa: BLE001 - orchestrator may not exist yet (task 7.2)
        logger.info("orchestrator.unavailable")
        app.state.orchestrator = None
        return

    try:
        orchestrator = Orchestrator()
        # Assemble the crew if the API is available, then wire onto the bus.
        build_crew = getattr(orchestrator, "build_crew", None)
        if callable(build_crew):
            build_crew()
        orchestrator.wire_event_bus(bus)
        app.state.orchestrator = orchestrator
        logger.info("orchestrator.wired")
    except Exception:  # noqa: BLE001 - never let a wiring error abort startup
        logger.exception("orchestrator.wire_failed")
        app.state.orchestrator = None


def _wire_ws_bridge(app: FastAPI, bus: EventBus, ws_manager) -> None:
    """Subscribe the event-bus → WebSocket bridge onto ``bus``.

    Forwards the dashboard-facing bus events (``agent.message``,
    ``session.update``, ``report.ready``) onto the live WebSocket rooms so the
    admin dashboard receives the inter-agent feed and tile updates (Requirements
    11.5, 12.3, 12.6). Guarded so a wiring error never aborts startup; the rest
    of the system still runs (the dashboard simply would not receive the feed).
    """
    try:
        from app.core.ws_bridge import register_ws_bridge

        app.state.ws_bridge = register_ws_bridge(bus, ws_manager=ws_manager)
        logger.info("ws_bridge.wired")
    except Exception:  # noqa: BLE001 - never let a bridge error abort startup
        logger.exception("ws_bridge.wire_failed")
        app.state.ws_bridge = None


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: start the backbone, tear it down cleanly."""
    settings = get_settings()
    configure_logging()
    logger.info("startup.begin", extra={"environment": settings.ENVIRONMENT})

    # Initialize the database schema (creates tables for the configured engine).
    init_db()

    # Build the event bus and wire the orchestrator (handlers before publish).
    bus = EventBus()
    app.state.event_bus = bus
    _wire_orchestrator(app, bus)

    # Bridge dashboard-facing bus events onto the live WebSocket rooms so the
    # admin dashboard actually receives the inter-agent feed, session-tile
    # updates, and report-ready signal (Requirements 11.5, 12.3, 12.6). The
    # Herald owns anomaly.detected → alert.broadcast itself, so the bridge
    # deliberately does not mirror those. Subscribed after the orchestrator
    # wires its agents but before any event is published (Requirement 11.1).
    ws_manager = get_ws_manager()
    _wire_ws_bridge(app, bus, ws_manager)

    # Start the WebSocket heartbeat loop (prunes stale sockets, Req 12A.3/4).
    ws_manager.start_heartbeat()
    app.state.ws_manager = ws_manager
    logger.info("startup.complete")

    try:
        yield
    finally:
        # Stop the heartbeat and release any live sockets on shutdown.
        await ws_manager.shutdown()
        logger.info("shutdown.complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return a fully configured FastAPI application.

    Middleware is added inner-to-outer; the request-id middleware is added last
    so it is the *outermost* layer and a correlation id is bound before any
    other middleware (including the rejecting body-size / rate-limit layers)
    runs, so every error envelope carries a matching ``requestId``.
    """
    settings = settings or get_settings()

    app = FastAPI(
        title="DRONA AI",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Standard error envelope handlers (Requirements 15.3, 15.4).
    register_error_handlers(app)

    # Per-process rate limiter for the gated endpoints (Requirement 15.6).
    limiter = SlidingWindowRateLimiter(
        max_requests=DEFAULT_MAX_REQUESTS,
        window_seconds=DEFAULT_WINDOW_SECONDS,
    )
    app.state.rate_limiter = limiter

    # --- Middleware stack (added inner-to-outer) ---
    # Innermost: security headers on every response.
    app.add_middleware(SecurityHeadersMiddleware)
    # Reject oversized bodies (15.3).
    app.add_middleware(BodySizeLimitMiddleware, max_body_bytes=settings.MAX_BODY_BYTES)
    # Rate-limit login + escalation (15.6).
    app.add_middleware(RateLimitMiddleware, limiter=limiter)
    # CORS locked to the configured allowlist (15.5).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_parse_origins(settings.FRONTEND_ORIGINS),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Outermost: bind a correlation id before anything else (so rejects carry it).
    app.add_middleware(RequestIdMiddleware)

    _mount_routers(app)

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        """Liveness probe (unauthenticated, no sensitive data)."""
        return {"status": "ok"}

    return app


def __getattr__(name: str) -> object:
    """Lazily build the module-level ``app`` on first access.

    ``uvicorn app.main:app`` resolves this attribute, which builds the app and
    validates required secrets are present (Requirement 15.2 aborts startup
    otherwise). Deferring construction keeps merely importing helpers/symbols
    from this module (e.g. in tests) from forcing secret validation at import.
    """
    if name == "app":
        application = create_app()
        globals()["app"] = application
        return application
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["app", "create_app", "lifespan", "API_V1_PREFIX"]
