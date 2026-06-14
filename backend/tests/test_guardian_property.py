"""Property-based test for Guardian confirmation gating (task 12.3).

**Property 2: No false alerts without confirmation** — for all guardian-sourced
anomalies, a broadcast occurs only when ``confirmed == true``.

**Validates: Requirements 7.4, 9.2, 9.3**

The Herald is not built yet, so this asserts the property at the Guardian level:
the ``confirmed`` flag on the emitted ``anomaly.detected`` event (which the
Herald uses to gate broadcasting per 9.2/9.3) is ``true`` *iff* the Vision
verdict was an authoritative anomalous verdict at or above the confirm threshold
(7.4). For every other outcome — benign, anomalous-but-below-threshold, or
Vision unavailable/timeout — the Guardian must NOT mark the anomaly
``confirmed=true`` (so the Herald would never broadcast it).

The vision backend is a deterministic, no-network stub fed an arbitrary verdict
(or a simulated provider failure); no real Vision call is ever made.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.guardian import CONFIRM_THRESHOLD, GuardianAgent
from app.agents.vision import (
    StaticMockVisionClient,
    VisionError,
    VisionTimeoutError,
    VisionVerdict,
)
from app.core.events import Event, EventBus, EventType
from app.repositories.anomaly import AnomalyRepository
from app.schemas.proctoring import LocalSignal

# A small, valid base64 JPEG-ish payload (content is irrelevant to the stub).
FRAME_B64 = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBD"

_LABELS = ["face_absent", "multiple_faces", "gaze_away", "benign"]


def _session_factory():
    import app.models  # noqa: F401

    from app.core.db import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session), engine


@st.composite
def verdicts_or_failures(draw):
    """Generate an arbitrary VisionVerdict, or a simulated provider failure."""
    if draw(st.booleans()):
        # Simulate Vision unavailable / timeout (7.6).
        return draw(
            st.sampled_from(
                [VisionTimeoutError("timeout"), VisionError("unavailable")]
            )
        )
    anomalous = draw(st.booleans())
    confidence = draw(
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
    )
    label = draw(st.sampled_from(_LABELS))
    if not anomalous:
        label = "benign"
    return VisionVerdict(anomalous=anomalous, confidence=confidence, label=label)


@settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(response=verdicts_or_failures(), kind=st.sampled_from(_LABELS[:3]))
@pytest.mark.asyncio
async def test_confirmed_iff_anomalous_and_above_threshold(response, kind) -> None:
    """Property 2: emitted ``confirmed`` is true only for an authoritative anomaly."""
    factory, engine = _session_factory()
    try:
        bus = EventBus()
        emitted: list[Event] = []

        async def _collect(event: Event) -> None:
            emitted.append(event)

        bus.subscribe(EventType.ANOMALY_DETECTED, _collect)

        agent = GuardianAgent(
            vision=StaticMockVisionClient([response]),
            bus=bus,
            anomaly_repo_factory=lambda: AnomalyRepository(factory()),
        )

        outcome = await agent.handle_escalation(
            "session-1", FRAME_B64, LocalSignal(kind=kind)
        )

        # Determine the expected confirmation from the response.
        if isinstance(response, Exception):
            expected_confirmed = False
        else:
            expected_confirmed = (
                response.anomalous and response.confidence >= CONFIRM_THRESHOLD
            )

        assert outcome.confirmed is expected_confirmed

        # Every emitted guardian anomaly.detected event carries a confirmed flag
        # that matches the outcome — the Herald broadcasts iff this is true.
        guardian_events = [
            e for e in emitted if e.payload.get("sourceAgent") == "guardian"
        ]
        for event in guardian_events:
            assert event.payload["confirmed"] is expected_confirmed

        # A confirmed outcome must have emitted exactly one confirmed event;
        # a non-confirmed outcome must never emit a confirmed event.
        confirmed_events = [
            e for e in guardian_events if e.payload["confirmed"] is True
        ]
        if expected_confirmed:
            assert len(confirmed_events) == 1
            assert outcome.action == "alert_broadcast"
        else:
            assert confirmed_events == []
            assert outcome.action == "suppressed"

        # The raw frame is always discarded after scoring (7.8).
        assert agent.retained_frame_count == 0
    finally:
        engine.dispose()
