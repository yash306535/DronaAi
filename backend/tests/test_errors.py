"""Tests for centralized error handling and structured logging.

Covers:
- Requirement 15.3: schema-invalid request bodies are rejected with a 422
  carrying the standard error envelope.
- Requirement 15.4: an unhandled application error returns a 500 with the
  standard envelope (non-empty code, human-readable message, request id that
  matches the request) and never leaks stack traces, secrets, or raw DB text.
- The error envelope shape and request-id propagation for the AppError
  hierarchy (AuthError / NotFoundError / ValidationError / UpstreamError).
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.errors import (
    AppError,
    AuthError,
    NotFoundError,
    UpstreamError,
    ValidationError,
    build_error_envelope,
    register_error_handlers,
)
from app.core.logging import (
    REQUEST_ID_HEADER,
    JsonLogFormatter,
    RequestIdMiddleware,
    get_request_id,
)

# A recognizable secret/DB-internal sentinel that must never appear in a body.
SECRET_SENTINEL = "sk-super-secret-value-do-not-leak-1234567890"
DB_INTERNAL_SENTINEL = 'syntax error at or near "SELECT" in table users'


class _Body(BaseModel):
    name: str
    count: int


def _build_app() -> FastAPI:
    """A minimal app exercising each error path through the global handlers."""
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)

    @app.post("/validate")
    async def _validate(body: _Body) -> dict[str, str]:  # noqa: ANN001
        return {"ok": "yes"}

    @app.get("/boom")
    async def _boom() -> dict[str, str]:
        # Simulate an unexpected crash whose text contains a secret + raw DB
        # error. Neither must reach the client.
        raise RuntimeError(f"{DB_INTERNAL_SENTINEL} token={SECRET_SENTINEL}")

    @app.get("/auth")
    async def _auth() -> dict[str, str]:
        raise AuthError("Token expired", code="TOKEN_EXPIRED")

    @app.get("/forbidden")
    async def _forbidden() -> dict[str, str]:
        raise AuthError("Role not permitted", code="FORBIDDEN", status_code=403)

    @app.get("/missing")
    async def _missing() -> dict[str, str]:
        raise NotFoundError("Exam not found")

    @app.get("/bad")
    async def _bad() -> dict[str, str]:
        raise ValidationError("Topic count out of range", code="TOPIC_RANGE")

    @app.get("/upstream")
    async def _upstream() -> dict[str, str]:
        raise UpstreamError("Vision timed out", code="UPSTREAM_VISION_TIMEOUT")

    return app


@pytest.fixture()
def client() -> TestClient:
    # raise_server_exceptions=False so the 500 handler response is returned to
    # the test client rather than re-raised.
    return TestClient(_build_app(), raise_server_exceptions=False)


def _assert_envelope_shape(body: dict) -> dict:
    assert set(body.keys()) >= {"error"}
    error = body["error"]
    assert error["code"], "code must be non-empty"
    assert error["message"], "message must be non-empty"
    assert error["requestId"], "requestId must be non-empty"
    return error


# --- Requirement 15.4: unhandled errors ------------------------------------


def test_unhandled_error_returns_500_envelope(client: TestClient) -> None:
    resp = client.get("/boom")
    assert resp.status_code == 500
    error = _assert_envelope_shape(resp.json())
    assert error["code"] == "INTERNAL_ERROR"


def test_unhandled_error_does_not_leak_secret_or_db_text(client: TestClient) -> None:
    resp = client.get("/boom")
    raw = resp.text
    assert SECRET_SENTINEL not in raw
    assert DB_INTERNAL_SENTINEL not in raw
    # No stack-trace markers in the response body.
    assert "Traceback" not in raw
    assert "RuntimeError" not in raw


def test_request_id_matches_header_and_body(client: TestClient) -> None:
    resp = client.get("/boom")
    header_id = resp.headers.get(REQUEST_ID_HEADER)
    assert header_id
    assert resp.json()["error"]["requestId"] == header_id


def test_inbound_request_id_is_propagated(client: TestClient) -> None:
    supplied = "trace-abc-123"
    resp = client.get("/boom", headers={REQUEST_ID_HEADER: supplied})
    assert resp.headers.get(REQUEST_ID_HEADER) == supplied
    assert resp.json()["error"]["requestId"] == supplied


# --- Requirement 15.3: schema/body validation ------------------------------


def test_schema_invalid_body_returns_422_envelope(client: TestClient) -> None:
    resp = client.post("/validate", json={"name": "x"})  # missing 'count'
    assert resp.status_code == 422
    error = _assert_envelope_shape(resp.json())
    assert error["code"] == "VALIDATION_ERROR"
    assert "details" in error


def test_valid_body_passes(client: TestClient) -> None:
    resp = client.post("/validate", json={"name": "x", "count": 3})
    assert resp.status_code == 200


# --- AppError hierarchy mapping --------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected_status", "expected_code"),
    [
        ("/auth", 401, "TOKEN_EXPIRED"),
        ("/forbidden", 403, "FORBIDDEN"),
        ("/missing", 404, "NOT_FOUND"),
        ("/bad", 422, "TOPIC_RANGE"),
        ("/upstream", 503, "UPSTREAM_VISION_TIMEOUT"),
    ],
)
def test_app_error_hierarchy_mapping(
    client: TestClient, path: str, expected_status: int, expected_code: str
) -> None:
    resp = client.get(path)
    assert resp.status_code == expected_status
    error = _assert_envelope_shape(resp.json())
    assert error["code"] == expected_code


def test_default_status_codes_on_subclasses() -> None:
    assert AuthError("x").status_code == 401
    assert NotFoundError("x").status_code == 404
    assert ValidationError("x").status_code == 422
    assert UpstreamError("x").status_code == 503
    assert isinstance(AuthError("x"), AppError)


def test_build_error_envelope_shape() -> None:
    env = build_error_envelope("CODE", "msg", "rid-1")
    assert env == {"error": {"code": "CODE", "message": "msg", "requestId": "rid-1"}}


# --- Structured logging ----------------------------------------------------


def test_json_formatter_emits_request_id_and_valid_json() -> None:
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    record.errorCode = "SOME_CODE"  # structured extra
    rendered = formatter.format(record)
    parsed = json.loads(rendered)
    assert parsed["message"] == "hello world"
    assert parsed["level"] == "INFO"
    assert "requestId" in parsed
    assert parsed["errorCode"] == "SOME_CODE"


def test_json_formatter_renders_exception_without_breaking_json() -> None:
    formatter = JsonLogFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    parsed = json.loads(formatter.format(record))
    assert "exc_info" in parsed
    assert "ValueError" in parsed["exc_info"]


def test_request_id_context_clears_after_request() -> None:
    # Outside of any request, no id should be bound.
    client = TestClient(_build_app(), raise_server_exceptions=False)
    client.get("/missing")
    assert get_request_id() is None
