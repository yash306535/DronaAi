"""Unit tests for PDF exam report generation (task 25, Requirement 10.1, P3).

Covers two layers:

- :func:`app.services.pdf_report.render_exam_report_pdf` produces a non-empty
  PDF (bytes starting with the ``%PDF`` header) from a sample analytics record.
- ``GET /analytics/exams/{id}/report.pdf`` returns 200 ``application/pdf`` for an
  existing analytics record and 404 when missing, under admin auth.

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
from app.services.pdf_report import render_exam_report_pdf

SECRET_VALUE = "super-secret-jwt-value-for-pdf-report-tests-1234567"
API_KEY_VALUE = "sk-openai-secret-key-for-pdf-report-tests"


# -- a lightweight analytics-shaped sample for the pure helper test ----------


class _SampleAnalytics:
    """Minimal analytics-like object satisfying the renderer's needs."""

    exam_id = "exam-123"
    generated_at = datetime(2026, 6, 13, 10, 30, tzinfo=timezone.utc)
    summary = {
        "distribution": {"0-10": 0, "70-80": 2, "90-100": 1},
        "mean": 78.33,
        "anomalyCount": 2,
        "completedStudents": 3,
        "status": "ready",
    }
    difficulty_heatmap = {
        "topics": {
            "algebra": {"accuracy": 80.0, "difficulty": 40.0},
            "geometry": {"accuracy": 55.0, "difficulty": 65.0},
        },
        "status": "ready",
    }
    per_student = {
        "students": {
            "student-1": {
                "score": 90.0,
                "topicAccuracy": {"algebra": 100.0, "geometry": 80.0},
                "suggestions": ["Review geometry proofs", "Practice timing"],
                "suggestionsStatus": "ready",
            },
            "student-2": {
                "score": 66.67,
                "topicAccuracy": {"algebra": 60.0},
                "suggestions": [],
                "suggestionsStatus": "pending",
            },
        },
        "status": "ready",
    }


def test_helper_produces_pdf_bytes() -> None:
    """The helper returns non-empty bytes starting with the %PDF header."""
    pdf = render_exam_report_pdf(_SampleAnalytics())
    assert isinstance(pdf, bytes)
    assert len(pdf) > 0
    assert pdf.startswith(b"%PDF")


def test_helper_handles_empty_sections() -> None:
    """A sparse/partial analytics record still renders a valid PDF."""

    class _Empty:
        exam_id = "exam-empty"
        generated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        summary: dict = {}
        difficulty_heatmap: dict = {}
        per_student: dict = {}

    pdf = render_exam_report_pdf(_Empty())
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 0


# -- endpoint tests ----------------------------------------------------------


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
            summary={
                "distribution": {"70-80": 1},
                "mean": 75.0,
                "anomalyCount": 1,
                "completedStudents": 1,
            },
            difficulty_heatmap={
                "topics": {"algebra": {"accuracy": 75.0, "difficulty": 40.0}}
            },
            per_student={
                "students": {
                    "student-1": {
                        "score": 75.0,
                        "topicAccuracy": {"algebra": 75.0},
                        "suggestions": ["Practice more algebra"],
                        "suggestionsStatus": "ready",
                    }
                }
            },
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


def test_report_pdf_returns_pdf_for_existing_analytics(
    client, seeded, session_factory
) -> None:
    """The endpoint returns 200 application/pdf for an existing record."""
    _add_analytics(session_factory, seeded["exam"])
    resp = client.get(
        f"/analytics/exams/{seeded['exam']}/report.pdf",
        headers=_headers(seeded["admin"], Role.ADMIN),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment" in resp.headers["content-disposition"]
    assert f"exam-{seeded['exam']}-report.pdf" in resp.headers["content-disposition"]
    assert resp.content.startswith(b"%PDF")


def test_report_pdf_missing_analytics_returns_404(client, seeded) -> None:
    """A 404 is returned when no analytics exist for the exam yet."""
    resp = client.get(
        f"/analytics/exams/{seeded['exam']}/report.pdf",
        headers=_headers(seeded["admin"], Role.ADMIN),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == ANALYTICS_NOT_FOUND_CODE


def test_report_pdf_non_admin_is_rejected(client, seeded) -> None:
    """A non-admin role is rejected with 403 before any data is returned."""
    resp = client.get(
        f"/analytics/exams/{seeded['exam']}/report.pdf",
        headers=_headers("someone", Role.STUDENT),
    )
    assert resp.status_code == 403
