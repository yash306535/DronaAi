"""Unit tests for the integrity-score management service (task 11).

Covers the deterministic penalty rule and the four data-integrity invariants
from Requirement 14:

- 14.5 — a confirmed anomaly never raises the integrity score.
- 14.6 — the integrity score is clamped to [0, 100].
- 14.7 — an anomaly score is clamped to [0.0, 1.0] before it drives a penalty.
- 14.8 — an invalid alert severity is rejected, prior value unchanged.

Plus the ``session.update`` change-emission contract (Requirement 12.6) and the
no-op behavior when the score does not change.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.errors import NotFoundError, ValidationError
from app.core.events import Event, EventBus, EventType
from app.models.enums import AlertSeverity, Role, SessionStatus
from app.models.orm import Exam, ExamSession, GeneratedPaper, User
from app.repositories.session import ExamSessionRepository
from app.services.integrity_service import (
    MAX_ANOMALY_PENALTY,
    IntegrityService,
    anomaly_penalty,
    clamp_anomaly_score,
    clamp_integrity_score,
    validate_alert_severity,
)


@pytest.fixture()
def db_session():
    import app.models  # noqa: F401 - register tables

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base_metadata_create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    session = factory()
    yield session
    session.close()
    engine.dispose()


def Base_metadata_create(engine) -> None:
    from app.core.db import Base

    Base.metadata.create_all(bind=engine)


def _seed_session(db: Session, integrity_score: float = 100.0) -> str:
    admin = User(
        email="admin@example.com", full_name="A", role=Role.ADMIN, password_hash="h"
    )
    db.add(admin)
    db.flush()
    student = User(
        email="s@example.com", full_name="S", role=Role.STUDENT, password_hash="h"
    )
    db.add(student)
    db.flush()
    exam = Exam(
        title="Math",
        subject="Mathematics",
        blueprint={"topics": [{"name": "algebra", "count": 1}], "total_questions": 1},
        duration_minutes=60,
        starts_at=datetime.now(timezone.utc),
        created_by=admin.id,
    )
    db.add(exam)
    db.flush()
    paper = GeneratedPaper(exam_id=exam.id, student_id=student.id, seed="s")
    db.add(paper)
    db.flush()
    sess = ExamSession(
        exam_id=exam.id,
        student_id=student.id,
        paper_id=paper.id,
        status=SessionStatus.ACTIVE,
        integrity_score=integrity_score,
    )
    db.add(sess)
    db.commit()
    return sess.id


# --- pure helpers ---------------------------------------------------------


def test_clamp_anomaly_score_bounds() -> None:
    assert clamp_anomaly_score(-1.0) == 0.0
    assert clamp_anomaly_score(2.0) == 1.0
    assert clamp_anomaly_score(0.42) == 0.42


def test_clamp_integrity_score_bounds() -> None:
    assert clamp_integrity_score(-5.0) == 0.0
    assert clamp_integrity_score(150.0) == 100.0
    assert clamp_integrity_score(73.0) == 73.0


def test_anomaly_penalty_is_non_negative_and_scaled() -> None:
    assert anomaly_penalty(0.0) == 0.0
    assert anomaly_penalty(1.0) == MAX_ANOMALY_PENALTY
    assert anomaly_penalty(0.5) == MAX_ANOMALY_PENALTY * 0.5
    # Out-of-range scores are clamped before scaling (14.7).
    assert anomaly_penalty(-3.0) == 0.0
    assert anomaly_penalty(5.0) == MAX_ANOMALY_PENALTY


# --- alert severity validation (14.8) ------------------------------------


def test_validate_alert_severity_accepts_enum_and_string() -> None:
    assert validate_alert_severity(AlertSeverity.DANGER) is AlertSeverity.DANGER
    assert validate_alert_severity("warning") is AlertSeverity.WARNING


def test_validate_alert_severity_rejects_invalid() -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate_alert_severity("catastrophic")
    assert excinfo.value.code == "invalid_alert_severity"


# --- record_confirmed_anomaly --------------------------------------------


@pytest.mark.asyncio
async def test_confirmed_anomaly_lowers_score(db_session) -> None:
    session_id = _seed_session(db_session, integrity_score=100.0)
    service = IntegrityService(sessions=ExamSessionRepository(db_session))

    result = await service.record_confirmed_anomaly(session_id, 1.0)
    # Max-severity anomaly subtracts the full penalty.
    assert result.integrity_score == 100.0 - MAX_ANOMALY_PENALTY


@pytest.mark.asyncio
async def test_confirmed_anomaly_clamps_at_zero(db_session) -> None:
    session_id = _seed_session(db_session, integrity_score=10.0)
    service = IntegrityService(sessions=ExamSessionRepository(db_session))

    # Penalty (25) exceeds remaining score (10); result clamps to 0 (14.6).
    result = await service.record_confirmed_anomaly(session_id, 1.0)
    assert result.integrity_score == 0.0


@pytest.mark.asyncio
async def test_benign_confirmed_anomaly_is_noop(db_session) -> None:
    session_id = _seed_session(db_session, integrity_score=80.0)
    bus = EventBus()
    seen: list[Event] = []

    async def handler(event: Event) -> None:
        seen.append(event)

    bus.subscribe(EventType.SESSION_UPDATE, handler)
    service = IntegrityService(
        sessions=ExamSessionRepository(db_session), event_bus=bus
    )

    result = await service.record_confirmed_anomaly(session_id, 0.0)
    assert result.integrity_score == 80.0
    # No change → no session.update emitted.
    assert seen == []


@pytest.mark.asyncio
async def test_session_update_emitted_on_change(db_session) -> None:
    session_id = _seed_session(db_session, integrity_score=100.0)
    bus = EventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe(EventType.SESSION_UPDATE, handler)
    service = IntegrityService(
        sessions=ExamSessionRepository(db_session), event_bus=bus
    )

    await service.record_confirmed_anomaly(session_id, 0.5)

    assert len(received) == 1
    event = received[0]
    assert event.type == EventType.SESSION_UPDATE
    assert event.session_id == session_id
    assert event.payload["integrityScore"] == 100.0 - MAX_ANOMALY_PENALTY * 0.5
    assert event.payload["status"] == str(SessionStatus.ACTIVE)


@pytest.mark.asyncio
async def test_record_confirmed_anomaly_missing_session(db_session) -> None:
    service = IntegrityService(sessions=ExamSessionRepository(db_session))
    with pytest.raises(NotFoundError):
        await service.record_confirmed_anomaly("nonexistent", 0.5)


@pytest.mark.asyncio
async def test_set_status_emits_update_on_change(db_session) -> None:
    session_id = _seed_session(db_session, integrity_score=100.0)
    bus = EventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe(EventType.SESSION_UPDATE, handler)
    service = IntegrityService(
        sessions=ExamSessionRepository(db_session), event_bus=bus
    )

    result = await service.set_status(session_id, SessionStatus.TERMINATED)
    assert result.status == SessionStatus.TERMINATED
    assert len(received) == 1
    assert received[0].payload["status"] == str(SessionStatus.TERMINATED)


@pytest.mark.asyncio
async def test_set_status_noop_when_unchanged(db_session) -> None:
    session_id = _seed_session(db_session, integrity_score=100.0)
    bus = EventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe(EventType.SESSION_UPDATE, handler)
    service = IntegrityService(
        sessions=ExamSessionRepository(db_session), event_bus=bus
    )

    await service.set_status(session_id, SessionStatus.ACTIVE)
    assert received == []
