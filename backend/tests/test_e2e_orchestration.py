"""End-to-end orchestration integration test (task 27.2).

Exercises the *whole* agent flow over a single shared event bus, proving the
wiring reconciliation done in task 27.1 holds: the REST layer, the agents, and
the WebSocket bridge all publish/subscribe on the **same** bus, and Herald +
the bridge fan out onto the **same** WebSocket manager the dashboard would
connect to.

The flow under test (design "End-to-End Orchestration Flow"):

    start session → emit session events (Sentinel) → escalate a frame
    (Guardian Stage-2, mocked Vision) → ``anomaly.detected`` (confirmed) →
    Herald ``alert.broadcast`` over the WS manager → submit exam →
    ``exam.completed`` → Analyst ``report.ready``.

Covered acceptance criteria:

- **7.4**: a confirmed Vision verdict (≥ 0.70) yields ``anomaly.detected`` with
  ``confirmed == true``.
- **9.2**: Herald broadcasts ``alert.broadcast`` to the ``dashboard`` and
  ``invigilator:{exam_id}`` rooms for a confirmed anomaly.
- **5.7**: submitting an active session emits exactly one ``exam.completed``.
- **10.5**: the Analyst emits ``report.ready`` once the report sections are
  produced.
- **2.8**: a WebSocket connection presenting a role not authorized for the
  requested room is rejected (closed) before binding.

All external models are mocked — a deterministic ``CallableVisionClient`` and
``CallableLLMClient`` — so no real OpenAI/Vision call is ever made.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.analyst import AnalystAgent, AnalystConfig
from app.agents.herald import HeraldAgent
from app.agents.guardian import GuardianAgent
from app.agents.llm import CallableLLMClient
from app.agents.sentinel import SentinelAgent, SentinelConfig
from app.agents.vision import CallableVisionClient, VisionVerdict
from app.api.proctoring import router as proctoring_router
from app.api.sessions import router as sessions_router
from app.core.db import Base, get_db
from app.core.errors import register_error_handlers
from app.core.events import Event, EventBus, EventType
from app.core.logging import RequestIdMiddleware
from app.core.security import issue_token_pair
from app.core.ws import (
    DASHBOARD_ROOM,
    INVIGILATOR_ROOM_PREFIX,
    WSMessage,
)
from app.core.ws_bridge import register_ws_bridge
from app.models.enums import (
    AuditStatus,
    QuestionType,
    Role,
    SessionStatus,
)
from app.models.orm import Exam, ExamSession, GeneratedPaper, Question, User
from app.repositories.alert import AlertRepository
from app.repositories.anomaly import AnomalyRepository
from app.repositories.session import ExamSessionRepository

SECRET_VALUE = "super-secret-jwt-value-for-e2e-orchestration-1234567890"
API_KEY_VALUE = "sk-openai-secret-key-for-e2e-orchestration-tests"


# --- test doubles -----------------------------------------------------------


class FakeWSManager:
    """Captures room broadcasts instead of delivering over a real socket.

    Shared by the Herald and the event-bus → WebSocket bridge so the test can
    assert what the live dashboard / invigilator rooms would have received.
    """

    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, WSMessage]] = []

    async def broadcast(self, room: str, message) -> int:
        self.broadcasts.append((room, message))
        return 1

    def messages_of(self, type_value: str) -> list[tuple[str, WSMessage]]:
        return [
            (room, msg)
            for room, msg in self.broadcasts
            if msg.to_dict()["type"] == type_value
        ]


class RecordingSpy:
    """A bus subscriber that records the events it receives (for assertions)."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)


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
def seeded(session_factory) -> dict:
    """Seed a student, an invigilator, an exam, and the student's paper."""
    session = session_factory()
    student = User(
        email="stu@example.com", full_name="Stu", role=Role.STUDENT, password_hash="x"
    )
    invig = User(
        email="inv@example.com", full_name="Inv", role=Role.INVIGILATOR, password_hash="x"
    )
    session.add_all([student, invig])
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
        options=["3", "4", "5", "6"],
        answer_key="4",
        topic="algebra",
        difficulty=0.5,
        max_marks=1.0,
    )
    session.add(question)
    session.commit()

    ids = {
        "student": student.id,
        "invig": invig.id,
        "exam": exam.id,
        "paper": paper.id,
        "question": question.id,
    }
    session.close()
    return ids


@pytest.fixture()
def wired(session_factory, seeded):
    """Build a shared bus + agents + WS bridge over one in-memory DB.

    Mirrors the production lifespan wiring (``app/main.py`` +
    ``orchestrator.wire_event_bus`` + ``register_ws_bridge``) but with mocked
    Vision/LLM and a capturing WS manager so the full flow can be asserted with
    no network. Returns the handles the test needs.
    """
    bus = EventBus()
    ws = FakeWSManager()

    def _anomaly_repo() -> AnomalyRepository:
        return AnomalyRepository(session_factory())

    def _alert_repo() -> AlertRepository:
        return AlertRepository(session_factory())

    def _session_repo() -> ExamSessionRepository:
        return ExamSessionRepository(session_factory())

    # Guardian: mocked Vision returns a confirmed anomalous verdict (≥ 0.70).
    def _vision_fn(_frame_b64: str) -> VisionVerdict:
        return VisionVerdict(
            anomalous=True,
            confidence=0.93,
            label="face_absent",
            reasons=["No face detected in frame"],
        )

    guardian = GuardianAgent(
        vision=CallableVisionClient(_vision_fn),
        bus=bus,
        anomaly_repo_factory=_anomaly_repo,
        orchestrator=None,
    )

    # Sentinel: scores published session.event telemetry.
    sentinel = SentinelAgent(
        bus=bus,
        anomaly_repo_factory=_anomaly_repo,
        config=SentinelConfig(),
    )

    # Herald: persists + broadcasts confirmed anomalies onto the fake WS manager.
    herald = HeraldAgent(
        alert_repo_factory=_alert_repo,
        session_repo_factory=_session_repo,
        ws_manager=ws,
        email_sender=lambda alert, reasons: False,
    )

    # Analyst: mocked LLM returns one suggestion for the seeded student.
    student_id = seeded["student"]

    def _llm_fn(_prompt: str) -> str:
        return json.dumps(
            {"students": {student_id: ["Review your weakest topic."]}}
        )

    analyst = AnalystAgent(
        llm=CallableLLMClient(_llm_fn),
        bus=bus,
        session_factory=session_factory,
        config=AnalystConfig(),
    )

    # Subscribe the real agent handlers (the same subscriptions the orchestrator
    # performs at startup), then the WS bridge for the dashboard feed.
    bus.subscribe(EventType.SESSION_EVENT, sentinel.on_session_event)
    bus.subscribe(EventType.ANOMALY_DETECTED, herald.on_anomaly_detected)
    bus.subscribe(EventType.EXAM_COMPLETED, analyst.on_exam_completed)

    def _resolve_exam(session_id: str) -> str | None:
        row = ExamSessionRepository(session_factory()).get(session_id)
        return row.exam_id if row is not None else None

    register_ws_bridge(bus, ws_manager=ws, exam_id_resolver=_resolve_exam)

    # Recording spies (subscribed after the real handlers so ordering is intact).
    anomaly_spy = RecordingSpy()
    report_spy = RecordingSpy()
    completed_spy = RecordingSpy()
    bus.subscribe(EventType.ANOMALY_DETECTED, anomaly_spy)
    bus.subscribe(EventType.REPORT_READY, report_spy)
    bus.subscribe(EventType.EXAM_COMPLETED, completed_spy)

    return {
        "bus": bus,
        "ws": ws,
        "guardian": guardian,
        "analyst": analyst,
        "anomaly_spy": anomaly_spy,
        "report_spy": report_spy,
        "completed_spy": completed_spy,
    }


@pytest.fixture()
def app(session_factory, wired):
    """A FastAPI app mounting the session + proctoring routers on the shared bus."""
    application = FastAPI()
    application.add_middleware(RequestIdMiddleware)
    register_error_handlers(application)
    application.include_router(sessions_router)
    application.include_router(proctoring_router)

    # The REST layer publishes onto the SAME bus the agents subscribed to.
    application.state.event_bus = wired["bus"]
    # The escalation route reuses this Guardian (mocked Vision, shared bus).
    application.state.guardian = wired["guardian"]

    def _override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    application.dependency_overrides[get_db] = _override_get_db
    return application


def _auth(user_id: str, role: Role) -> dict[str, str]:
    token = issue_token_pair(user_id, role.value).access_token
    return {"Authorization": f"Bearer {token}"}


# --- the end-to-end flow ----------------------------------------------------


@pytest.mark.asyncio
async def test_full_flow_escalate_broadcast_submit_report(
    app, seeded, wired
) -> None:
    """7.4 / 9.2 / 5.7 / 10.5: the whole orchestration flow over one shared bus."""
    student = _auth(seeded["student"], Role.STUDENT)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1) Start the session (5.1) — returns the answer-key-free paper.
        start = await client.post(f"/sessions/{seeded['exam']}/start", headers=student)
        assert start.status_code == 200, start.text
        session_id = start.json()["id"]
        assert "answer_key" not in start.text

        # 2) Emit session telemetry events (5.8) — Sentinel scores them.
        events = await client.post(
            f"/sessions/{session_id}/events",
            headers=student,
            json={
                "events": [
                    {"kind": "tab_blur", "payload": {}},
                    {"kind": "paste", "payload": {"len": 120}},
                    {"kind": "tab_focus", "payload": {}},
                ]
            },
        )
        assert events.status_code == 200, events.text

        # 3) Record an answer so the Analyst has data to aggregate later.
        ans = await client.post(
            f"/sessions/{session_id}/answers",
            headers=student,
            json={"question_id": seeded["question"], "response": "4"},
        )
        assert ans.status_code == 200, ans.text

        # 4) Escalate a captured frame (Stage 2). Mocked Vision confirms it (7.4).
        frame = base64.b64encode(b"fake-jpeg-frame-bytes").decode()
        esc = await client.post(
            f"/proctoring/{session_id}/escalate",
            headers=student,
            json={
                "local_signal": {
                    "kind": "face_absent",
                    "duration_ms": 4200,
                    "confidence_local": 0.81,
                },
                "frame": frame,
            },
        )
        assert esc.status_code == 200, esc.text
        body = esc.json()
        assert body["confirmed"] is True
        assert body["action"] == "alert_broadcast"

        # 5) Submit the exam (5.6/5.7) — emits exactly one exam.completed.
        submit = await client.post(
            f"/sessions/{session_id}/submit", headers=student
        )
        assert submit.status_code == 200, submit.text
        assert submit.json()["status"] == SessionStatus.SUBMITTED.value

        # The Analyst schedules report generation off the delivery path; await it.
        await wired["analyst"].wait_for_pending()

    # --- assertions on the orchestration outcome ---

    # 7.4: a confirmed anomaly.detected was published.
    anomalies = wired["anomaly_spy"].events
    assert any(
        e.payload.get("confirmed") is True for e in anomalies
    ), "expected a confirmed anomaly.detected event"

    # 9.2: Herald broadcast alert.broadcast to the dashboard + invigilator rooms.
    ws = wired["ws"]
    alert_rooms = {room for room, _ in ws.messages_of("alert.broadcast")}
    assert DASHBOARD_ROOM in alert_rooms
    assert f"{INVIGILATOR_ROOM_PREFIX}{seeded['exam']}" in alert_rooms

    # 11.5/12.3: the inter-agent feed (agent.message) reached the dashboard via
    # the WS bridge (Guardian announces the confirmation).
    agent_msg_rooms = {room for room, _ in ws.messages_of("agent.message")}
    assert DASHBOARD_ROOM in agent_msg_rooms

    # 5.7: exactly one exam.completed for the single submit transition.
    assert len(wired["completed_spy"].events) == 1

    # 10.5: the Analyst emitted report.ready for the exam.
    reports = wired["report_spy"].events
    assert len(reports) == 1
    assert reports[0].payload.get("examId") == seeded["exam"]

    # 12.6/12: the report.ready signal was bridged onto the dashboard feed too.
    report_rooms = {room for room, _ in ws.messages_of("report.ready")}
    assert DASHBOARD_ROOM in report_rooms


# --- 2.8: WebSocket role rejection ------------------------------------------


def test_ws_connection_with_wrong_role_is_rejected() -> None:
    """2.8: a non-admin role is rejected from the dashboard room before binding."""
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from app.api import ws_routes

    application = FastAPI()
    application.include_router(ws_routes.router)
    client = TestClient(application)

    # A Student token is not authorized for the admin-only dashboard room (2.8).
    token = issue_token_pair("stu-1", Role.STUDENT.value).access_token
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/dashboard?token={token}"):
            pass
