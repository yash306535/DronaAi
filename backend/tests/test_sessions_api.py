"""Unit tests for the session lifecycle endpoints (task 10.5).

Covers Requirement 5 lifecycle rules and the answer-key-safe serialization
boundary:

- duplicate-session rejection (5.2)
- answer rejection on a non-active session (5.5)
- terminate blocks later answers (5.10)
- >100-event batch rejection, persisting none (5.9)
- server-timestamp authority on ingested events (5.8, 14.3)
- exactly one ``exam.completed`` event on submit (5.7)
- the returned paper carries no answer-key field (5.3, 14.1)

A test app mounts the real session router against an in-memory SQLite DB seeded
with a student, an exam, a generated paper (with answer keys stored
server-side), and a session. A capturing event bus is attached to
``app.state.event_bus`` so emitted events can be asserted.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.sessions import router as sessions_router
from app.core.db import Base, get_db
from app.core.errors import register_error_handlers
from app.core.events import Event, EventType
from app.core.logging import RequestIdMiddleware
from app.core.security import issue_token_pair
from app.models.enums import (
    AuditStatus,
    QuestionType,
    Role,
    SessionStatus,
)
from app.models.orm import (
    Exam,
    ExamSession,
    GeneratedPaper,
    Question,
    User,
)
from app.schemas.session import MAX_EVENT_BATCH
from app.services.session_service import (
    SESSION_EXISTS_CODE,
    SESSION_NOT_ACTIVE_CODE,
)

from datetime import datetime, timezone

SECRET_VALUE = "super-secret-jwt-value-for-session-api-tests-123456"
API_KEY_VALUE = "sk-openai-secret-key-for-session-api-tests"

ANSWER_KEY_VALUE = "the-correct-answer-42"


class CapturingBus:
    """Minimal async event bus stand-in that records published events."""

    def __init__(self) -> None:
        self.published: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.published.append(event)


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch):
    from app.core.config import Settings, get_settings

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
    """Seed a student, exam, generated paper (+ one question with answer key)."""
    session = session_factory()
    student = User(
        email="stu@example.com",
        full_name="Stu Dent",
        role=Role.STUDENT,
        password_hash="x",
    )
    other = User(
        email="other@example.com",
        full_name="Other Student",
        role=Role.STUDENT,
        password_hash="x",
    )
    invig = User(
        email="inv@example.com",
        full_name="In Vig",
        role=Role.INVIGILATOR,
        password_hash="x",
    )
    session.add_all([student, other, invig])
    session.flush()

    exam = Exam(
        title="Algebra",
        subject="Math",
        blueprint={"topics": [{"name": "algebra", "count": 1}]},
        duration_minutes=60,
        starts_at=datetime.now(timezone.utc),
        created_by=invig.id,
    )
    session.add(exam)
    session.flush()

    paper = GeneratedPaper(
        exam_id=exam.id,
        student_id=student.id,
        seed="seed-1",
        audit_status=AuditStatus.APPROVED,
    )
    session.add(paper)
    session.flush()

    question = Question(
        paper_id=paper.id,
        index=0,
        type=QuestionType.MCQ,
        prompt="2 + 2 = ?",
        options=["3", ANSWER_KEY_VALUE, "5", "6"],
        answer_key=ANSWER_KEY_VALUE,
        topic="algebra",
        difficulty=0.5,
        max_marks=1.0,
    )
    session.add(question)
    session.commit()

    ids = {
        "student": student.id,
        "other": other.id,
        "invig": invig.id,
        "exam": exam.id,
        "paper": paper.id,
        "question": question.id,
    }
    session.close()
    return ids


@pytest.fixture()
def bus():
    return CapturingBus()


@pytest.fixture()
def client(session_factory, bus):
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)
    app.include_router(sessions_router)
    app.state.event_bus = bus

    def _override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app, raise_server_exceptions=False)


def _auth(user_id: str, role: Role) -> dict[str, str]:
    token = issue_token_pair(user_id, role.value).access_token
    return {"Authorization": f"Bearer {token}"}


def _make_active_session(session_factory, ids) -> str:
    """Create an active session row directly and return its id."""
    session = session_factory()
    row = ExamSession(
        exam_id=ids["exam"],
        student_id=ids["student"],
        paper_id=ids["paper"],
        status=SessionStatus.ACTIVE,
        started_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    sid = row.id
    session.close()
    return sid


# --- start / duplicate rejection (5.1, 5.2, 5.3) ---------------------------


def test_start_returns_paper_without_answer_key(client, seeded) -> None:
    """5.1/5.3: starting returns the student's paper with no answer-key field."""
    resp = client.post(
        f"/sessions/{seeded['exam']}/start", headers=_auth(seeded["student"], Role.STUDENT)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == SessionStatus.ACTIVE.value
    # The answer-key FIELD is never serialized (the correct option's text is
    # legitimately shown among the MCQ options; only the key field is secret).
    assert "answer_key" not in resp.text
    question = body["paper"]["questions"][0]
    assert "answer_key" not in question
    assert question["prompt"] == "2 + 2 = ?"


def test_start_rejects_when_active_session_exists(client, seeded, session_factory) -> None:
    """5.2: a second start with an existing active session is rejected."""
    _make_active_session(session_factory, seeded)
    resp = client.post(
        f"/sessions/{seeded['exam']}/start", headers=_auth(seeded["student"], Role.STUDENT)
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == SESSION_EXISTS_CODE


# --- answer rejection on non-active session (5.5, 5.10) --------------------


def test_answer_rejected_when_session_not_active(client, seeded, session_factory) -> None:
    """5.5: submitting an answer to a non-active session is rejected."""
    sid = _make_active_session(session_factory, seeded)
    # Terminate it first so it is no longer active.
    term = client.post(
        f"/sessions/{sid}/terminate", headers=_auth(seeded["invig"], Role.INVIGILATOR)
    )
    assert term.status_code == 200
    assert term.json()["status"] == SessionStatus.TERMINATED.value

    resp = client.post(
        f"/sessions/{sid}/answers",
        headers=_auth(seeded["student"], Role.STUDENT),
        json={"question_id": seeded["question"], "response": "late"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == SESSION_NOT_ACTIVE_CODE


def test_terminate_blocks_later_answers(client, seeded, session_factory) -> None:
    """5.10: after terminate, subsequent answer submissions are rejected."""
    sid = _make_active_session(session_factory, seeded)
    # Answer works while active.
    ok = client.post(
        f"/sessions/{sid}/answers",
        headers=_auth(seeded["student"], Role.STUDENT),
        json={"question_id": seeded["question"], "response": "early"},
    )
    assert ok.status_code == 200
    # Terminate, then a later answer is blocked.
    client.post(
        f"/sessions/{sid}/terminate", headers=_auth(seeded["invig"], Role.INVIGILATOR)
    )
    blocked = client.post(
        f"/sessions/{sid}/answers",
        headers=_auth(seeded["student"], Role.STUDENT),
        json={"question_id": seeded["question"], "response": "late"},
    )
    assert blocked.status_code == 422


def test_answer_records_whole_second_time_spent(client, seeded, session_factory) -> None:
    """5.4: persisted time spent is server-recorded and a whole-second multiple."""
    sid = _make_active_session(session_factory, seeded)
    resp = client.post(
        f"/sessions/{sid}/answers",
        headers=_auth(seeded["student"], Role.STUDENT),
        json={"question_id": seeded["question"], "response": "answer"},
    )
    assert resp.status_code == 200
    time_spent_ms = resp.json()["time_spent_ms"]
    assert time_spent_ms % 1000 == 0  # whole seconds only


# --- submit emits exactly one exam.completed (5.6, 5.7) --------------------


def test_submit_emits_exactly_one_exam_completed(client, seeded, session_factory, bus) -> None:
    """5.7: submitting an active session emits exactly one exam.completed event."""
    sid = _make_active_session(session_factory, seeded)
    resp = client.post(
        f"/sessions/{sid}/submit", headers=_auth(seeded["student"], Role.STUDENT)
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == SessionStatus.SUBMITTED.value

    completed = [e for e in bus.published if e.type == EventType.EXAM_COMPLETED]
    assert len(completed) == 1
    assert completed[0].session_id == sid

    # A second submit is rejected (no further events).
    again = client.post(
        f"/sessions/{sid}/submit", headers=_auth(seeded["student"], Role.STUDENT)
    )
    assert again.status_code == 422
    completed_after = [e for e in bus.published if e.type == EventType.EXAM_COMPLETED]
    assert len(completed_after) == 1


# --- event ingestion (5.8, 5.9, 14.3) --------------------------------------


def test_event_batch_over_limit_is_rejected_and_persists_none(
    client, seeded, session_factory
) -> None:
    """5.9: a >100-event batch is rejected (422) and persists none of its events."""
    sid = _make_active_session(session_factory, seeded)
    events = [{"kind": "heartbeat", "payload": {}} for _ in range(MAX_EVENT_BATCH + 1)]
    resp = client.post(
        f"/sessions/{sid}/events",
        headers=_auth(seeded["student"], Role.STUDENT),
        json={"events": events},
    )
    assert resp.status_code == 422

    # Nothing persisted: a subsequent valid single event is the first stored row.
    from app.repositories.session_event import SessionEventRepository

    db = session_factory()
    try:
        stored = SessionEventRepository(db).list_for_session(sid)
    finally:
        db.close()
    assert stored == []


def test_events_use_server_timestamp_authority(
    client, seeded, session_factory, bus
) -> None:
    """5.8/14.3: each event gets an authoritative server_ts; client_ts is metadata."""
    sid = _make_active_session(session_factory, seeded)
    bogus_client_ts = "2000-01-01T00:00:00+00:00"
    resp = client.post(
        f"/sessions/{sid}/events",
        headers=_auth(seeded["student"], Role.STUDENT),
        json={
            "events": [
                {"kind": "paste", "payload": {"len": 10}, "client_ts": bogus_client_ts},
                {"kind": "tab_blur", "payload": {}},
            ]
        },
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    for row in rows:
        # server_ts is present and authoritative (not the bogus client value).
        assert row["server_ts"][:4] != "2000"
    # One session.event published per persisted event (for Sentinel).
    session_events = [e for e in bus.published if e.type == EventType.SESSION_EVENT]
    assert len(session_events) == 2
