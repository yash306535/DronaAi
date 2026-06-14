"""Unit tests for the Herald agent broadcasting rules (task 13.1).

Covers Requirement 9 broadcasting behaviour:

- severity persistence: an alert is persisted with exactly one severity from
  ``{info, warning, danger}`` as indicated by the event (9.1)
- no broadcast on an unconfirmed guardian anomaly (9.2, 9.3)
- the anomaly's reasons are included in the ``alert.broadcast`` payload (9.7)
- email-failure fallback retains the alert and completes the WebSocket
  broadcast (9.5)

A real in-memory SQLite DB backs the repositories so persistence is exercised
end to end; a fake WebSocket manager records broadcasts so no real socket I/O
occurs, and an injectable email sender simulates SMTP success/failure.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.herald import HeraldAgent
from app.core.db import Base
from app.core.events import Event, EventType
from app.core.ws import DASHBOARD_ROOM, INVIGILATOR_ROOM_PREFIX
from app.models.enums import (
    AlertSeverity,
    AnomalyCategory,
    AuditStatus,
    Role,
    SessionStatus,
    SourceAgent,
)
from app.models.orm import Anomaly, Exam, ExamSession, GeneratedPaper, User
from app.repositories.alert import AlertRepository
from app.repositories.session import ExamSessionRepository


# --- fakes ------------------------------------------------------------------


class FakeWSManager:
    """Records ``broadcast`` calls instead of delivering over a socket."""

    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, object]] = []

    async def broadcast(self, room: str, message) -> int:
        self.broadcasts.append((room, message))
        return 1


# --- fixtures ---------------------------------------------------------------


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
    """Seed a student, exam, paper, an active session, and a stored anomaly."""
    session = session_factory()
    student = User(
        email="stu@example.com", full_name="Stu", role=Role.STUDENT, password_hash="x"
    )
    session.add(student)
    session.flush()

    exam = Exam(
        title="Algebra",
        subject="Math",
        blueprint={"topics": [{"name": "algebra", "count": 1}]},
        duration_minutes=60,
        starts_at=datetime.now(timezone.utc),
        created_by=student.id,
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

    exam_session = ExamSession(
        exam_id=exam.id,
        student_id=student.id,
        paper_id=paper.id,
        status=SessionStatus.ACTIVE,
        started_at=datetime.now(timezone.utc),
    )
    session.add(exam_session)
    session.flush()

    anomaly = Anomaly(
        session_id=exam_session.id,
        source_agent=SourceAgent.GUARDIAN,
        category=AnomalyCategory.FACE_ABSENT,
        score=0.93,
        reasons=["No face detected in frame", "Consistent with 4.2s local absence"],
        evidence={},
        confirmed=True,
    )
    session.add(anomaly)
    session.commit()

    ids = {
        "exam": exam.id,
        "session": exam_session.id,
        "anomaly": anomaly.id,
        "reasons": list(anomaly.reasons),
    }
    session.close()
    return ids


def _make_herald(session_factory, ws_manager, email_sender):
    return HeraldAgent(
        alert_repo_factory=lambda: AlertRepository(session_factory()),
        session_repo_factory=lambda: ExamSessionRepository(session_factory()),
        ws_manager=ws_manager,
        email_sender=email_sender,
    )


def _anomaly_event(
    ids,
    *,
    confirmed: bool,
    severity: str = "danger",
    reasons: list[str] | None = None,
    source: str = "guardian",
) -> Event:
    return Event(
        type=EventType.ANOMALY_DETECTED,
        payload={
            "anomalyId": ids["anomaly"],
            "sessionId": ids["session"],
            "sourceAgent": source,
            "category": "face_absent",
            "score": 0.93,
            "reasons": reasons if reasons is not None else ids["reasons"],
            "confirmed": confirmed,
            "severity": severity,
        },
        source=source,
        session_id=ids["session"],
    )


# --- 9.1: severity persistence ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("severity", ["info", "warning", "danger"])
async def test_alert_persisted_with_indicated_severity(
    session_factory, seeded, severity
) -> None:
    """9.1: the alert is persisted with exactly the severity the event indicates."""
    ws = FakeWSManager()
    herald = _make_herald(session_factory, ws, lambda alert, reasons: False)

    await herald.on_anomaly_detected(
        _anomaly_event(seeded, confirmed=True, severity=severity)
    )

    db = session_factory()
    try:
        alerts = AlertRepository(db).list_for_session(seeded["session"])
    finally:
        db.close()

    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity(severity)
    assert alerts[0].anomaly_id == seeded["anomaly"]


# --- 9.2 / 9.3: no broadcast on an unconfirmed guardian anomaly ------------


@pytest.mark.asyncio
async def test_unconfirmed_guardian_anomaly_persists_but_does_not_broadcast(
    session_factory, seeded
) -> None:
    """9.3: an unconfirmed guardian anomaly is persisted but never broadcast."""
    ws = FakeWSManager()
    herald = _make_herald(session_factory, ws, lambda alert, reasons: False)

    await herald.on_anomaly_detected(
        _anomaly_event(seeded, confirmed=False, severity="warning")
    )

    # The alert is still persisted (9.1) ...
    db = session_factory()
    try:
        alerts = AlertRepository(db).list_for_session(seeded["session"])
    finally:
        db.close()
    assert len(alerts) == 1
    # ... but no WebSocket broadcast happened (9.3).
    assert ws.broadcasts == []
    assert alerts[0].delivered_ws is False


@pytest.mark.asyncio
async def test_confirmed_anomaly_broadcasts_to_dashboard_and_invigilator(
    session_factory, seeded
) -> None:
    """9.2: a confirmed anomaly broadcasts to the dashboard + invigilator rooms."""
    ws = FakeWSManager()
    herald = _make_herald(session_factory, ws, lambda alert, reasons: False)

    await herald.on_anomaly_detected(_anomaly_event(seeded, confirmed=True))

    rooms = {room for room, _ in ws.broadcasts}
    assert DASHBOARD_ROOM in rooms
    assert f"{INVIGILATOR_ROOM_PREFIX}{seeded['exam']}" in rooms

    db = session_factory()
    try:
        alerts = AlertRepository(db).list_for_session(seeded["session"])
    finally:
        db.close()
    assert alerts[0].delivered_ws is True


# --- 9.7: reasons included in the broadcast payload ------------------------


@pytest.mark.asyncio
async def test_reasons_included_in_broadcast_payload(session_factory, seeded) -> None:
    """9.7: the anomaly reasons appear in the alert.broadcast payload."""
    ws = FakeWSManager()
    herald = _make_herald(session_factory, ws, lambda alert, reasons: False)
    reasons = ["Vision confirmed 2 faces", "Local multi-face for 6.1s"]

    await herald.on_anomaly_detected(
        _anomaly_event(seeded, confirmed=True, reasons=reasons)
    )

    assert ws.broadcasts, "expected at least one broadcast"
    for _room, message in ws.broadcasts:
        payload = message.to_dict()["payload"]
        assert payload["reasons"] == reasons
        assert payload["anomalyId"] == seeded["anomaly"]


# --- 9.5: email-failure fallback retains alert + completes WS broadcast ----


@pytest.mark.asyncio
async def test_email_failure_retains_alert_and_completes_broadcast(
    session_factory, seeded
) -> None:
    """9.5: a failing email send leaves the alert intact and the WS broadcast done."""
    ws = FakeWSManager()

    def _failing_email(alert, reasons):
        raise RuntimeError("smtp down")

    herald = _make_herald(session_factory, ws, _failing_email)

    # Must not raise despite the email failure.
    await herald.on_anomaly_detected(_anomaly_event(seeded, confirmed=True))

    # WebSocket broadcast still completed (dashboard + invigilator).
    rooms = {room for room, _ in ws.broadcasts}
    assert DASHBOARD_ROOM in rooms
    assert f"{INVIGILATOR_ROOM_PREFIX}{seeded['exam']}" in rooms

    db = session_factory()
    try:
        alerts = AlertRepository(db).list_for_session(seeded["session"])
    finally:
        db.close()
    assert len(alerts) == 1  # alert retained
    assert alerts[0].delivered_ws is True  # WS broadcast recorded
    assert alerts[0].delivered_email is False  # email failure recorded


@pytest.mark.asyncio
async def test_successful_email_marks_delivered(session_factory, seeded) -> None:
    """9.4: a successful email send marks the alert delivered_email=true."""
    ws = FakeWSManager()
    herald = _make_herald(session_factory, ws, lambda alert, reasons: True)

    await herald.on_anomaly_detected(_anomaly_event(seeded, confirmed=True))

    db = session_factory()
    try:
        alerts = AlertRepository(db).list_for_session(seeded["session"])
    finally:
        db.close()
    assert alerts[0].delivered_email is True
