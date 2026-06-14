"""Structured JSON logging with a request/correlation id.

This module owns the per-request correlation id used across the backend. The
same id is:

- generated (or accepted from an inbound ``X-Request-ID`` header) by
  :class:`RequestIdMiddleware`,
- stored in a :class:`contextvars.ContextVar` so any log record emitted while
  handling the request is automatically tagged with it,
- echoed back to the client via the ``X-Request-ID`` response header, and
- embedded in every error envelope as ``requestId`` (see ``app.core.errors``).

Supports Requirement 15.4: every error envelope carries a ``requestId`` that
matches the id recorded for that request. Log output is line-delimited JSON so
it is machine-parseable by aggregators, and it never serializes ``SecretStr``
values (they mask themselves) nor exception stack traces into the response —
stack traces are only ever written to the server-side log stream.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

# Header used to accept an inbound correlation id and to echo it back out.
REQUEST_ID_HEADER = "X-Request-ID"

# Context variable holding the correlation id for the in-flight request. It is
# read by the logging formatter and by the error handlers so the id reported to
# the client matches the id recorded in the logs for that same request.
_request_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "drona_request_id", default=None
)

# Standard ``LogRecord`` attributes; anything else attached to a record is
# treated as structured "extra" context and merged into the JSON output.
_RESERVED_LOG_RECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
    }
)


def new_request_id() -> str:
    """Return a fresh correlation id."""
    return str(uuid.uuid4())


def set_request_id(request_id: str | None) -> contextvars.Token[str | None]:
    """Bind ``request_id`` to the current context; returns a reset token."""
    return _request_id_ctx.set(request_id)


def reset_request_id(token: contextvars.Token[str | None]) -> None:
    """Restore the correlation id to its previous value."""
    _request_id_ctx.reset(token)


def get_request_id() -> str | None:
    """Return the correlation id bound to the current context, if any."""
    return _request_id_ctx.get()


def get_or_create_request_id() -> str:
    """Return the current correlation id, creating and binding one if absent."""
    request_id = _request_id_ctx.get()
    if request_id is None:
        request_id = new_request_id()
        _request_id_ctx.set(request_id)
    return request_id


class JsonLogFormatter(logging.Formatter):
    """Render log records as single-line JSON objects.

    The current correlation id is always included (``null`` when none is bound).
    Exception information, when present, is rendered into the server-side log
    only; it is never surfaced in HTTP responses.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "requestId": get_request_id(),
        }

        # Merge any structured extras passed via ``logger.info(..., extra={...})``.
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOG_RECORD_ATTRS or key.startswith("_"):
                continue
            if key in payload:
                continue
            payload[key] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=_json_default, ensure_ascii=False)


def _json_default(value: Any) -> str:
    """Fallback serializer for non-JSON-native values in log extras."""
    return str(value)


def configure_logging(level: int | str = logging.INFO) -> None:
    """Install the JSON formatter on the root logger.

    Idempotent: repeated calls replace the existing DRONA handler rather than
    stacking duplicates.
    """
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    handler.set_name("drona-json")

    root = logging.getLogger()
    root.setLevel(level)
    # Remove a previously installed DRONA handler so reconfiguration is clean.
    for existing in list(root.handlers):
        if existing.get_name() == "drona-json":
            root.removeHandler(existing)
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger (thin wrapper for consistent import sites)."""
    return logging.getLogger(name)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Assign a correlation id to every request and expose it everywhere.

    The id is taken from the inbound ``X-Request-ID`` header when present (so a
    caller or upstream proxy can propagate a trace id), otherwise a new UUID is
    generated. It is stored on ``request.state.request_id`` and in the logging
    context var, then echoed on the response via ``X-Request-ID``.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        inbound = request.headers.get(REQUEST_ID_HEADER)
        request_id = inbound.strip() if inbound and inbound.strip() else new_request_id()

        request.state.request_id = request_id
        token = set_request_id(request_id)
        try:
            response = await call_next(request)
        finally:
            reset_request_id(token)

        response.headers[REQUEST_ID_HEADER] = request_id
        return response


def install_request_logging(app: ASGIApp) -> None:
    """Register :class:`RequestIdMiddleware` on a FastAPI/Starlette app."""
    app.add_middleware(RequestIdMiddleware)  # type: ignore[attr-defined]
