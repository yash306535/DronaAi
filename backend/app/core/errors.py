"""Centralized error handling and the standard error envelope.

Implements the design's centralized error handling and Requirements 15.3/15.4:

- **Exception hierarchy**: ``AppError`` is the base for all expected,
  domain-level failures. Concrete subtypes (``AuthError``, ``NotFoundError``,
  ``ValidationError``, ``UpstreamError``) carry a machine-readable ``code``, a
  human-readable ``message``, and an HTTP ``status_code``.
- **Standard error envelope**: every error response — domain error, request
  validation failure, or unexpected crash — is rendered as
  ``{"error": {"code": ..., "message": ..., "requestId": ...}}``. The
  ``requestId`` always matches the correlation id recorded for the request
  (see ``app.core.logging``).
- **No leakage (15.4)**: handlers never place stack traces, secret values, or
  raw database/driver error text into the response body. Unexpected errors are
  logged in full server-side (with the correlation id) and returned to the
  client as a generic 500 envelope with a non-empty code and message.
- **Schema/body validation (15.3)**: FastAPI ``RequestValidationError`` is
  mapped to a 422 envelope. (The 1 MB body-size limit that also yields 422 is
  enforced by middleware in the app factory; both surface the same envelope.)
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger, get_or_create_request_id

# Attribute set on ``request.state`` by ``RequestIdMiddleware``. Reading it here
# is robust even when the context var has already been reset (e.g. the 500
# handler runs in the outermost middleware, after per-request teardown).
_REQUEST_STATE_ID_ATTR = "request_id"

logger = get_logger("app.core.errors")

# Generic, safe fallback surfaced for any unexpected/unhandled error. The real
# cause is logged server-side only; the client never sees internals.
INTERNAL_ERROR_CODE = "INTERNAL_ERROR"
INTERNAL_ERROR_MESSAGE = "An internal error occurred. Please retry later."

VALIDATION_ERROR_CODE = "VALIDATION_ERROR"


class AppError(Exception):
    """Base class for expected, domain-level application errors.

    Attributes:
        code: Stable, machine-readable error code (e.g. ``"NOT_FOUND"``).
        message: Human-readable, safe-to-expose description. MUST NOT contain
            secrets, stack traces, or raw database text.
        status_code: HTTP status used when rendering the envelope.
        details: Optional structured, safe-to-expose context. Never include
            sensitive values here.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "APP_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details
        super().__init__(message)


class AuthError(AppError):
    """Authentication/authorization failure.

    Defaults to 401 (unauthenticated). Pass ``status_code=403`` for an
    authenticated-but-forbidden caller.
    """

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "AUTH_ERROR"


class NotFoundError(AppError):
    """A requested resource does not exist or is not visible to the caller."""

    status_code = status.HTTP_404_NOT_FOUND
    code = "NOT_FOUND"


class ValidationError(AppError):
    """A request failed domain/schema validation (422)."""

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = VALIDATION_ERROR_CODE


class UpstreamError(AppError):
    """A dependency (LLM, Vision, DB, SMTP) failed or timed out.

    Defaults to 503; use a specific ``code`` (e.g. ``UPSTREAM_VISION_TIMEOUT``)
    to identify the failing dependency without exposing its raw error text.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "UPSTREAM_ERROR"


def build_error_envelope(
    code: str, message: str, request_id: str
) -> dict[str, dict[str, str]]:
    """Construct the standard error envelope payload.

    Shape: ``{"error": {"code": ..., "message": ..., "requestId": ...}}``.
    """
    return {"error": {"code": code, "message": message, "requestId": request_id}}


def _resolve_request_id(request: Request | None) -> str:
    """Return the correlation id for this request.

    Prefers the id stashed on ``request.state`` by ``RequestIdMiddleware`` (it
    survives context-var teardown), then the logging context var, then a fresh
    id as a last resort.
    """
    if request is not None:
        state_id = getattr(request.state, _REQUEST_STATE_ID_ATTR, None)
        if state_id:
            return str(state_id)
    return get_or_create_request_id()


def _error_response(
    *,
    request: Request | None,
    code: str,
    message: str,
    status_code: int,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """Render an error envelope as a JSON response tagged with the request id."""
    request_id = _resolve_request_id(request)
    envelope = build_error_envelope(code, message, request_id)
    if details:
        # Details are optional, structured, and must already be safe to expose.
        envelope["error"]["details"] = jsonable_encoder(details)  # type: ignore[assignment]
    return JSONResponse(
        status_code=status_code,
        content=envelope,
        headers={"X-Request-ID": request_id},
    )


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """Render an :class:`AppError` (and subclasses) as the standard envelope."""
    # Domain errors are expected; log at warning without a stack trace.
    logger.warning(
        "Application error handled",
        extra={"errorCode": exc.code, "statusCode": exc.status_code},
    )
    return _error_response(
        request=request,
        code=exc.code,
        message=exc.message,
        status_code=exc.status_code,
        details=exc.details,
    )


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Map FastAPI request-validation failures to a 422 envelope (15.3).

    The validation ``errors()`` describe which fields failed and are safe to
    expose (they contain no secrets or DB internals); they are attached under
    ``details`` to help clients correct the request.
    """
    safe_details = {"errors": jsonable_encoder(exc.errors())}
    return _error_response(
        request=request,
        code=VALIDATION_ERROR_CODE,
        message="Request validation failed.",
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        details=safe_details,
    )


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Map raw ``HTTPException`` (e.g. 404 routing, manual raises) to envelope.

    Keeps every error path on the same envelope shape. The HTTP phrase is used
    as a generic code so no internal detail leaks.
    """
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
    code = _status_to_code(exc.status_code)
    return _error_response(
        request=request, code=code, message=detail, status_code=exc.status_code
    )


async def unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Map any unhandled exception to a generic 500 envelope (15.4).

    The full exception (including its stack trace and any raw DB/driver text)
    is logged server-side with the correlation id. The client receives only a
    generic code/message plus the matching ``requestId`` — never stack traces,
    secrets, or raw database error text.
    """
    logger.error(
        "Unhandled exception",
        exc_info=exc,
        extra={"path": request.url.path, "method": request.method},
    )
    return _error_response(
        request=request,
        code=INTERNAL_ERROR_CODE,
        message=INTERNAL_ERROR_MESSAGE,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def _status_to_code(status_code: int) -> str:
    """Derive a stable machine-readable code from an HTTP status."""
    mapping = {
        status.HTTP_400_BAD_REQUEST: "BAD_REQUEST",
        status.HTTP_401_UNAUTHORIZED: "UNAUTHORIZED",
        status.HTTP_403_FORBIDDEN: "FORBIDDEN",
        status.HTTP_404_NOT_FOUND: "NOT_FOUND",
        status.HTTP_405_METHOD_NOT_ALLOWED: "METHOD_NOT_ALLOWED",
        status.HTTP_409_CONFLICT: "CONFLICT",
        status.HTTP_422_UNPROCESSABLE_ENTITY: VALIDATION_ERROR_CODE,
        status.HTTP_429_TOO_MANY_REQUESTS: "RATE_LIMITED",
        status.HTTP_503_SERVICE_UNAVAILABLE: "SERVICE_UNAVAILABLE",
    }
    return mapping.get(status_code, "HTTP_ERROR")


def register_error_handlers(app: FastAPI) -> None:
    """Register all global exception handlers on a FastAPI app.

    Order is not significant for FastAPI (handlers are keyed by exception
    type), but the most specific types are registered first for clarity.
    """
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(
        RequestValidationError, validation_error_handler  # type: ignore[arg-type]
    )
    app.add_exception_handler(
        StarletteHTTPException, http_exception_handler  # type: ignore[arg-type]
    )
    app.add_exception_handler(Exception, unhandled_exception_handler)
