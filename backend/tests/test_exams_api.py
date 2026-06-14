"""Unit tests for the exam creation and provisioning endpoints (task 9.1).

Covers Requirement 3:

- create exam in ``draft`` with a valid title/blueprint (3.1)
- 422 on out-of-range topic count / total-question count / title (3.2, 3.3, 3.4)
- list exams visible to admin/invigilator (3.5)
- provision draft → ``provisioning`` and dispatch one ``exam.provision`` per
  enrolled student (3.6, 3.7)
- reject provisioning a non-draft exam, status unchanged (3.8)
- reject provisioning with zero enrolled students, status stays ``draft`` (3.9)

A test app mounts the real exam router against an in-memory SQLite DB. A
capturing event bus is attached to ``app.state.event_bus`` so dispatched
``exam.provision`` events can be asserted.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.exams import router as exams_router
from app.api.runtime import get_event_bus
from app.core.config import Settings, get_settings
from app.core.db import Base, get_db
from app.core.errors import register_error_handlers
from app.core.events import Event, EventType
from app.core.logging import RequestIdMiddleware
from app.core.security import hash_password, issue_token_pair
from app.models.enums import ExamStatus, Role
from app.models.orm import Exam, User
from app.services.exam_service import (
    EXAM_NOT_DRAFT_CODE,
    NO_STUDENTS_ENROLLED_CODE,
)

SECRET_VALUE = "super-secret-jwt-value-for-exam-api-tests-1234567"
API_KEY_VALUE = "sk-openai-secret-key-for-exam-api-tests"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("JWT_SECRET", SECRET_VALUE)
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY_VALUE)
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    return get_settings()


class CapturingBus:
    """A minimal event bus stand-in that records published events."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.events.append(event)


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
    """Seed an admin (creator) and return ids; students added per-test."""
    session = session_factory()
    admin = User(
        email="admin@example.com",
        full_name="Ada Min",
        role=Role.ADMIN,
        password_hash=hash_password("adminpass123"),
    )
    session.add(admin)
    session.commit()
    session.refresh(admin)
    ids = {"admin": admin.id}
    session.close()
    return ids


def _add_students(session_factory, n: int) -> list[str]:
    session = session_factory()
    ids = []
    for i in range(n):
        student = User(
            email=f"student{i}@example.com",
            full_name=f"Student {i}",
            role=Role.STUDENT,
            password_hash=hash_password("studentpass1"),
        )
        session.add(student)
        session.commit()
        session.refresh(student)
        ids.append(student.id)
    session.close()
    return ids


def _add_exam(session_factory, admin_id: str, status: ExamStatus) -> str:
    session = session_factory()
    exam = Exam(
        title="Existing Exam",
        subject="Math",
        blueprint={
            "topics": [{"name": "algebra", "count": 2}],
            "total_questions": 2,
            "difficulty_mix": {},
            "question_types": ["mcq"],
        },
        duration_minutes=60,
        starts_at=datetime(2026, 6, 13, 10, 0, tzinfo=timezone.utc),
        status=status,
        created_by=admin_id,
    )
    session.add(exam)
    session.commit()
    session.refresh(exam)
    exam_id = exam.id
    session.close()
    return exam_id


@pytest.fixture()
def app_and_bus(session_factory):
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)
    bus = CapturingBus()
    app.state.event_bus = bus
    app.include_router(exams_router)

    def _override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_event_bus] = lambda: bus
    return app, bus


@pytest.fixture()
def client(app_and_bus):
    app, _ = app_and_bus
    return TestClient(app, raise_server_exceptions=False)


def _admin_headers(admin_id: str) -> dict:
    token = issue_token_pair(admin_id, Role.ADMIN.value).access_token
    return {"Authorization": f"Bearer {token}"}


def _valid_exam_body() -> dict:
    return {
        "title": "Algebra Midterm",
        "subject": "Mathematics",
        "blueprint": {
            "topics": [{"name": "algebra", "count": 3}],
            "total_questions": 3,
            "difficulty_mix": {},
            "question_types": ["mcq"],
        },
        "duration_minutes": 90,
        "starts_at": "2026-06-13T10:00:00+00:00",
    }


# --- create (3.1-3.4) -------------------------------------------------------


def test_create_exam_returns_draft(client, seeded) -> None:
    """3.1: a valid exam definition creates a record with status draft."""
    resp = client.post(
        "/exams", json=_valid_exam_body(), headers=_admin_headers(seeded["admin"])
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == ExamStatus.DRAFT.value
    assert body["title"] == "Algebra Midterm"


def test_create_exam_rejects_empty_title(client, seeded) -> None:
    """3.4: an empty title is rejected with a 422 (schema validation)."""
    body = _valid_exam_body()
    body["title"] = ""
    resp = client.post("/exams", json=body, headers=_admin_headers(seeded["admin"]))
    assert resp.status_code == 422


def test_create_exam_rejects_too_many_topics(client, seeded) -> None:
    """3.2: more than 100 topics is rejected with a 422."""
    body = _valid_exam_body()
    body["blueprint"]["topics"] = [
        {"name": f"t{i}", "count": 0} for i in range(101)
    ]
    body["blueprint"]["total_questions"] = 5
    resp = client.post("/exams", json=body, headers=_admin_headers(seeded["admin"]))
    assert resp.status_code == 422


def test_create_exam_rejects_total_count_out_of_range(client, seeded) -> None:
    """3.3: a total question count above 1000 is rejected with a 422."""
    body = _valid_exam_body()
    body["blueprint"]["topics"] = [{"name": "algebra", "count": 0}]
    body["blueprint"]["total_questions"] = 1001
    resp = client.post("/exams", json=body, headers=_admin_headers(seeded["admin"]))
    assert resp.status_code == 422


def test_create_exam_requires_admin(client, seeded, session_factory) -> None:
    """Only an admin may create an exam (role guard)."""
    student_id = _add_students(session_factory, 1)[0]
    token = issue_token_pair(student_id, Role.STUDENT.value).access_token
    resp = client.post(
        "/exams",
        json=_valid_exam_body(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


# --- list (3.5) -------------------------------------------------------------


def test_list_exams_visible_to_admin(client, seeded, session_factory) -> None:
    """3.5: an admin sees exams in their role audience."""
    _add_exam(session_factory, seeded["admin"], ExamStatus.DRAFT)
    resp = client.get("/exams", headers=_admin_headers(seeded["admin"]))
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# --- provision (3.6-3.9) ----------------------------------------------------


def test_provision_draft_dispatches_per_student(
    client, app_and_bus, seeded, session_factory
) -> None:
    """3.6/3.7: provisioning a draft sets provisioning and dispatches per student."""
    _, bus = app_and_bus
    student_ids = _add_students(session_factory, 3)
    exam_id = _add_exam(session_factory, seeded["admin"], ExamStatus.DRAFT)

    resp = client.post(
        f"/exams/{exam_id}/provision", headers=_admin_headers(seeded["admin"])
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == ExamStatus.PROVISIONING.value

    provision_events = [
        e for e in bus.events if e.type == EventType.EXAM_PROVISION
    ]
    assert len(provision_events) == 3
    dispatched_students = {e.payload["studentId"] for e in provision_events}
    assert dispatched_students == set(student_ids)
    # Every event carries the blueprint + exam id for the Architect.
    for e in provision_events:
        assert e.payload["examId"] == exam_id
        assert "blueprint" in e.payload


def test_provision_non_draft_is_rejected_unchanged(
    client, app_and_bus, seeded, session_factory
) -> None:
    """3.8: a non-draft exam cannot be provisioned and stays unchanged."""
    _, bus = app_and_bus
    _add_students(session_factory, 2)
    exam_id = _add_exam(session_factory, seeded["admin"], ExamStatus.PROVISIONING)

    resp = client.post(
        f"/exams/{exam_id}/provision", headers=_admin_headers(seeded["admin"])
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == EXAM_NOT_DRAFT_CODE
    # No events dispatched.
    assert not [e for e in bus.events if e.type == EventType.EXAM_PROVISION]

    # Status unchanged.
    detail = client.get(
        f"/exams/{exam_id}", headers=_admin_headers(seeded["admin"])
    ).json()
    assert detail["status"] == ExamStatus.PROVISIONING.value


def test_provision_zero_enrolled_is_rejected_stays_draft(
    client, app_and_bus, seeded, session_factory
) -> None:
    """3.9: provisioning with zero enrolled students leaves the exam draft."""
    _, bus = app_and_bus
    exam_id = _add_exam(session_factory, seeded["admin"], ExamStatus.DRAFT)

    resp = client.post(
        f"/exams/{exam_id}/provision", headers=_admin_headers(seeded["admin"])
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == NO_STUDENTS_ENROLLED_CODE
    assert not [e for e in bus.events if e.type == EventType.EXAM_PROVISION]

    detail = client.get(
        f"/exams/{exam_id}", headers=_admin_headers(seeded["admin"])
    ).json()
    assert detail["status"] == ExamStatus.DRAFT.value


def test_papers_status_reports_progress(
    client, seeded, session_factory
) -> None:
    """papers/status reports enrolled count and generated/pending papers."""
    _add_students(session_factory, 2)
    exam_id = _add_exam(session_factory, seeded["admin"], ExamStatus.DRAFT)
    resp = client.get(
        f"/exams/{exam_id}/papers/status", headers=_admin_headers(seeded["admin"])
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enrolledStudents"] == 2
    assert body["papersGenerated"] == 0
    assert body["pending"] == 2
