"""Unit tests for the Stage-2 escalation endpoint + Guardian (task 12.4).

Covers Requirement 7 guards and benign/timeout handling:

- oversized frame rejection (422) with NO Vision call (7.3)
- bad-MIME frame rejection (422) with NO Vision call (7.3)
- non-owned session rejection (403) with NO Vision call (7.7)
- non-active session rejection (403) with NO Vision call (7.7)
- confirmed anomalous verdict → broadcast action, confirmed=true (7.4)
- benign verdict → false positive recorded, local threshold raised +0.05, cap 0.95 (7.5)
- Vision unavailable/timeout → unconfirmed ``warning`` anomaly, threshold unchanged (7.6)
- raw escalated frame discarded after scoring (7.8)

A test app mounts the real proctoring router against an in-memory SQLite DB
seeded with a student, an exam, a generated paper, and a session. A
:class:`StaticMockVisionClient` (or a call-counting stub) stands in for OpenAI
Vision so the "NO Vision call" guarantees can be asserted and no network I/O
occurs.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.guardian import (
    DEFAULT_LOCAL_THRESHOLD,
    THRESHOLD_CAP,
    THRESHOLD_STEP,
    GuardianAgent,
)
from app.agents.vision import (
    VERDICT_BENIGN,
    StaticMockVisionClient,
    VisionClient,
    VisionTimeoutError,
    VisionVerdict,
)
from app.api.proctoring import (
    FRAME_BAD_MIME_CODE,
    FRAME_TOO_LARGE_CODE,
    MAX_FRAME_BYTES,
    router as proctoring_router,
)
from app.core.db import Base, get_db
from app.core.errors import register_error_handlers
from app.core.events import Event, EventType
from app.core.logging import RequestIdMiddleware
from app.core.security import issue_token_pair
from app.models.enums import AuditStatus, QuestionType, Role, SessionStatus
from app.models.orm import Exam, ExamSession, GeneratedPaper, Question, User
from app.repositories.anomaly import AnomalyRepository

SECRET_VALUE = "super-secret-jwt-value-for-proctoring-api-tests-123456"
API_KEY_VALUE = "sk-openai-secret-key-for-proctoring-api-tests"


# --- counting / canned vision stubs ----------------------------------------


class CountingVisionClient(VisionClient):
    """Vision stub that records call count and returns a fixed verdict.

    Used to assert that a guarded rejection NEVER reaches the provider
    (``calls == 0``) while a valid escalation does (``calls == 1``).
    """

    def __init__(self, verdict: VisionVerdict) -> None:
        self._verdict = verdict
        self.calls = 0

    async def analyze(self, frame_b64, prompt, *, mime_type="image/jpeg", timeout=None):
        self.calls += 1
        return self._verdict


# --- fixtures ---------------------------------------------------------------


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
    """Seed a student (+ another student), an exam, a paper, and an active session."""
    session = session_factory()
    student = User(
        email="stu@example.com", full_name="Stu", role=Role.STUDENT, password_hash="x"
    )
    other = User(
        email="other@example.com", full_name="Other", role=Role.STUDENT, password_hash="x"
    )
    session.add_all([student, other])
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
    session.add(
        Question(
            paper_id=paper.id,
            index=0,
            type=QuestionType.MCQ,
            prompt="2+2?",
            options=["3", "4"],
            answer_key="4",
            topic="algebra",
            difficulty=0.5,
            max_marks=1.0,
        )
    )

    active = ExamSession(
        exam_id=exam.id,
        student_id=student.id,
        paper_id=paper.id,
        status=SessionStatus.ACTIVE,
        started_at=datetime.now(timezone.utc),
    )
    submitted = ExamSession(
        exam_id=exam.id,
        student_id=student.id,
        paper_id=paper.id,
        status=SessionStatus.SUBMITTED,
        started_at=datetime.now(timezone.utc),
    )
    session.add_all([active, submitted])
    session.commit()

    ids = {
        "student": student.id,
        "other": other.id,
        "exam": exam.id,
        "active_session": active.id,
        "submitted_session": submitted.id,
    }
    session.close()
    return ids


def _make_client(session_factory, guardian: GuardianAgent):
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)
    app.include_router(proctoring_router)
    app.state.guardian = guardian

    def _override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app, raise_server_exceptions=False)


def _auth(user_id: str, role: Role = Role.STUDENT) -> dict[str, str]:
    token = issue_token_pair(user_id, role.value).access_token
    return {"Authorization": f"Bearer {token}"}


def _frame(mime: str = "image/jpeg", payload: bytes = b"hello-frame") -> str:
    b64 = base64.b64encode(payload).decode()
    return f"data:{mime};base64,{b64}"


def _guardian(session_factory, vision: VisionClient) -> GuardianAgent:
    return GuardianAgent(
        vision=vision,
        bus=None,
        anomaly_repo_factory=lambda: AnomalyRepository(session_factory()),
    )


def _anomalous_verdict() -> VisionVerdict:
    return VisionVerdict(anomalous=True, confidence=0.93, label="face_absent")


def _benign_verdict() -> VisionVerdict:
    return VisionVerdict(anomalous=False, confidence=0.2, label=VERDICT_BENIGN)


# --- 7.3: payload guards reject with NO Vision call ------------------------


def test_oversized_frame_rejected_no_vision_call(session_factory, seeded) -> None:
    """7.3: a frame exceeding the max size is rejected (422) and Vision is not called."""
    vision = CountingVisionClient(_anomalous_verdict())
    client = _make_client(session_factory, _guardian(session_factory, vision))

    big_payload = b"x" * (MAX_FRAME_BYTES + 10)
    resp = client.post(
        f"/proctoring/{seeded['active_session']}/escalate",
        headers=_auth(seeded["student"]),
        json={
            "local_signal": {"kind": "face_absent"},
            "frame": _frame("image/jpeg", big_payload),
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == FRAME_TOO_LARGE_CODE
    assert vision.calls == 0


def test_bad_mime_frame_rejected_no_vision_call(session_factory, seeded) -> None:
    """7.3: a non-jpeg/png MIME is rejected (422) and Vision is not called."""
    vision = CountingVisionClient(_anomalous_verdict())
    client = _make_client(session_factory, _guardian(session_factory, vision))

    resp = client.post(
        f"/proctoring/{seeded['active_session']}/escalate",
        headers=_auth(seeded["student"]),
        json={
            "local_signal": {"kind": "face_absent"},
            "frame": _frame("image/gif", b"gif-bytes"),
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == FRAME_BAD_MIME_CODE
    assert vision.calls == 0


# --- 7.7: auth / ownership / active-session guards reject with NO Vision call


def test_non_owned_session_rejected_no_vision_call(session_factory, seeded) -> None:
    """7.7: a student escalating another student's session is rejected (403), no Vision."""
    vision = CountingVisionClient(_anomalous_verdict())
    client = _make_client(session_factory, _guardian(session_factory, vision))

    resp = client.post(
        f"/proctoring/{seeded['active_session']}/escalate",
        headers=_auth(seeded["other"]),  # not the owner
        json={"local_signal": {"kind": "face_absent"}, "frame": _frame()},
    )
    assert resp.status_code == 403
    assert vision.calls == 0


def test_non_active_session_rejected_no_vision_call(session_factory, seeded) -> None:
    """7.7: escalating a non-active (submitted) session is rejected (403), no Vision."""
    vision = CountingVisionClient(_anomalous_verdict())
    client = _make_client(session_factory, _guardian(session_factory, vision))

    resp = client.post(
        f"/proctoring/{seeded['submitted_session']}/escalate",
        headers=_auth(seeded["student"]),
        json={"local_signal": {"kind": "face_absent"}, "frame": _frame()},
    )
    assert resp.status_code == 403
    assert vision.calls == 0


def test_unknown_session_rejected_no_vision_call(session_factory, seeded) -> None:
    """7.7: escalating a non-existent session is rejected (403), no Vision."""
    vision = CountingVisionClient(_anomalous_verdict())
    client = _make_client(session_factory, _guardian(session_factory, vision))

    resp = client.post(
        "/proctoring/does-not-exist/escalate",
        headers=_auth(seeded["student"]),
        json={"local_signal": {"kind": "face_absent"}, "frame": _frame()},
    )
    assert resp.status_code == 403
    assert vision.calls == 0


# --- 7.4: confirmed anomalous verdict --------------------------------------


def test_confirmed_anomaly_broadcasts(session_factory, seeded) -> None:
    """7.4: an anomalous verdict ≥0.70 confirms → confirmed=true, alert_broadcast."""
    vision = CountingVisionClient(_anomalous_verdict())
    client = _make_client(session_factory, _guardian(session_factory, vision))

    resp = client.post(
        f"/proctoring/{seeded['active_session']}/escalate",
        headers=_auth(seeded["student"]),
        json={"local_signal": {"kind": "face_absent"}, "frame": _frame()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert vision.calls == 1
    assert body["confirmed"] is True
    assert body["action"] == "alert_broadcast"
    assert body["category"] == "face_absent"
    assert body["anomaly_id"]


# --- 7.5: benign verdict raises the local threshold ------------------------


def test_benign_verdict_raises_threshold_and_suppresses(session_factory, seeded) -> None:
    """7.5: a benign verdict records a false positive and raises the threshold +0.05."""
    guardian = _guardian(session_factory, StaticMockVisionClient([_benign_verdict()]))
    client = _make_client(session_factory, guardian)
    sid = seeded["active_session"]

    resp = client.post(
        f"/proctoring/{sid}/escalate",
        headers=_auth(seeded["student"]),
        json={"local_signal": {"kind": "face_absent"}, "frame": _frame()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["confirmed"] is False
    assert body["action"] == "suppressed"
    assert body["anomaly_id"] is None
    # Threshold raised by exactly 0.05 and a false positive recorded (7.5).
    assert guardian.false_positive_count(sid) == 1
    assert guardian.get_threshold(sid) == pytest.approx(
        DEFAULT_LOCAL_THRESHOLD + THRESHOLD_STEP
    )


@pytest.mark.asyncio
async def test_benign_threshold_capped_at_095(session_factory) -> None:
    """7.5: repeated benign verdicts raise the threshold but cap at 0.95."""
    guardian = GuardianAgent(
        vision=StaticMockVisionClient([_benign_verdict()]),
        bus=None,
        anomaly_repo_factory=lambda: AnomalyRepository(session_factory()),
    )
    from app.schemas.proctoring import LocalSignal

    sid = "session-cap"
    # Many benign escalations; the threshold must never exceed the 0.95 cap.
    for _ in range(50):
        await guardian.handle_escalation(sid, "/9j/abc", LocalSignal(kind="gaze_away"))
    assert guardian.get_threshold(sid) == pytest.approx(THRESHOLD_CAP)
    assert guardian.get_threshold(sid) <= THRESHOLD_CAP


# --- 7.6: Vision unavailable/timeout → unconfirmed warning -----------------


def test_vision_timeout_records_unconfirmed_warning(session_factory, seeded) -> None:
    """7.6: a Vision timeout records an unconfirmed warning, threshold unchanged."""
    guardian = _guardian(
        session_factory, StaticMockVisionClient([VisionTimeoutError("timeout")])
    )
    client = _make_client(session_factory, guardian)
    sid = seeded["active_session"]

    resp = client.post(
        f"/proctoring/{sid}/escalate",
        headers=_auth(seeded["student"]),
        json={"local_signal": {"kind": "multiple_faces", "confidence_local": 0.8}, "frame": _frame()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["confirmed"] is False
    assert body["action"] == "suppressed"
    # An unconfirmed anomaly was persisted with warning severity.
    assert body["anomaly_id"]
    # Threshold unchanged on unavailability (7.6: not a benign false positive).
    assert guardian.get_threshold(sid) == pytest.approx(DEFAULT_LOCAL_THRESHOLD)
    assert guardian.false_positive_count(sid) == 0

    db = session_factory()
    try:
        anomalies = AnomalyRepository(db).list_for_session(sid)
    finally:
        db.close()
    assert len(anomalies) == 1
    assert anomalies[0].confirmed is False


# --- 7.8: raw frame discarded after scoring --------------------------------


@pytest.mark.asyncio
async def test_raw_frame_discarded_after_scoring(session_factory) -> None:
    """7.8: the raw escalated frame is discarded after scoring (no retention)."""
    from app.schemas.proctoring import LocalSignal

    guardian = GuardianAgent(
        vision=StaticMockVisionClient([_anomalous_verdict()]),
        bus=None,
        anomaly_repo_factory=lambda: AnomalyRepository(session_factory()),
    )
    await guardian.handle_escalation(
        "session-x", "/9j/raw-frame-bytes", LocalSignal(kind="face_absent")
    )
    # No raw frame is retained once scoring completes.
    assert guardian.retained_frame_count == 0
