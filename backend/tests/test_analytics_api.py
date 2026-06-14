"""Unit tests for the analytics endpoint (task 21.1).

Covers ``GET /analytics/exams/{id}`` (Requirement 10):

- admin receives the persisted :class:`ExamAnalytics` record
- 404 when no analytics exist for the exam yet
- non-admin roles are rejected (403)

A test app mounts the real analytics router against an in-memory SQLite DB.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.analytics import ANALYTICS_NOT_FOUND_CODE, router as analytics_router
from app.core.config import Settings, get_settings
from app.core.db import Base, get_db
from app.core.errors import register_error_handlers
from app.core.logging import RequestIdMiddleware
from app.core.security import issue_token_pair
from app.models.enums import Role
from app.models.orm import Exam, ExamAnalytics, User

SECRET_VALUE = "super-secret-jwt-value-for-analytics-api-tests-1234"
API_KEY_VALUE = "sk-openai-secret-key-for-analytics-api-tests"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("JWT_SECRET", SECRET_VALUE)
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY_VALUE)
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture()
def session_factory():
    import app.models  # noqa: F401 - register tables

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    yield factory
    engine.dispose()


@pytest.fixture()
def seeded(session_factory):
    """Seed an admin and an exam; analytics are added per-test."""
    session = session_factory()
    admin = User(
        email="admin@example.com", full_name="Ada", role=Role.ADMIN, password_hash="x"
    )
    session.add(admin)
    session.flush()
    exam = Exam(
        title="Algebra",
        subject="Math",
        blueprint={"topics": [{"name": "algebra", "count": 1}]},
        duration_minutes=60,
        starts_at=datetime.now(timezone.utc),
        created_by=admin.id,
    )
    session.add(exam)
    session.commit()
    ids = {"admin": admin.id, "exam": exam.id}
    session.close()
    return ids


def _add_analytics(session_factory, exam_id: str) -> None:
    session = session_factory()
    session.add(
        ExamAnalytics(
            exam_id=exam_id,
            summary={"mean": 75.0, "anomalyCount": 1},
            difficulty_heatmap={"topics": {}},
            per_student={"students": {}},
        )
    )
    session.commit()
    session.close()


@pytest.fixture()
def client(session_factory):
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)
    app.include_router(analytics_router)

    def _override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app, raise_server_exceptions=False)


def _headers(user_id: str, role: Role) -> dict:
    token = issue_token_pair(user_id, role.value).access_token
    return {"Authorization": f"Bearer {token}"}


def test_admin_gets_persisted_analytics(client, seeded, session_factory) -> None:
    """An admin receives the persisted analytics record for the exam."""
    _add_analytics(session_factory, seeded["exam"])
    resp = client.get(
        f"/analytics/exams/{seeded['exam']}", headers=_headers(seeded["admin"], Role.ADMIN)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["exam_id"] == seeded["exam"]
    assert body["summary"]["mean"] == 75.0


def test_missing_analytics_returns_404(client, seeded) -> None:
    """A 404 is returned when no analytics exist for the exam yet."""
    resp = client.get(
        f"/analytics/exams/{seeded['exam']}", headers=_headers(seeded["admin"], Role.ADMIN)
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == ANALYTICS_NOT_FOUND_CODE


def test_non_admin_is_rejected(client, seeded) -> None:
    """A non-admin role is rejected with 403 before any data is returned."""
    resp = client.get(
        f"/analytics/exams/{seeded['exam']}",
        headers=_headers("someone", Role.STUDENT),
    )
    assert resp.status_code == 403
