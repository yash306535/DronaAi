"""Property-based test for integrity-score monotonicity (task 11.1).

**Property 8: Integrity score monotonicity** — for all sessions, the
``integrity_score`` is non-increasing as confirmed anomalies accumulate and
stays within the inclusive range ``[0, 100]``.

**Validates: Requirements 14.5, 14.6**

The test drives the real :class:`IntegrityService` against an in-memory SQLite
database (no mocking of the persistence path): it seeds a session at a random
starting integrity score, then applies a random sequence of confirmed-anomaly
scores (including out-of-range values to exercise the 14.7 clamp). After every
step it asserts the persisted score never rises and always lies within
``[0, 100]``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base
from app.models.enums import Role, SessionStatus
from app.models.orm import Exam, ExamSession, GeneratedPaper, User
from app.repositories.session import ExamSessionRepository
from app.services.integrity_service import (
    INTEGRITY_SCORE_MAX,
    INTEGRITY_SCORE_MIN,
    IntegrityService,
)


def _session_factory():
    import app.models  # noqa: F401 - register tables

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session), engine


def _seed_session(db: Session, integrity_score: float) -> str:
    admin = User(
        email="admin@example.com",
        full_name="Admin",
        role=Role.ADMIN,
        password_hash="h",
    )
    db.add(admin)
    db.flush()
    student = User(
        email="student@example.com",
        full_name="Student",
        role=Role.STUDENT,
        password_hash="h",
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


@settings(max_examples=60, deadline=None)
@given(
    start_score=st.floats(min_value=0.0, max_value=100.0),
    # Include out-of-range scores to exercise the [0.0, 1.0] clamp (14.7).
    anomaly_scores=st.lists(
        st.floats(
            min_value=-0.5,
            max_value=1.5,
            allow_nan=False,
            allow_infinity=False,
        ),
        min_size=0,
        max_size=12,
    ),
)
@pytest.mark.asyncio
async def test_integrity_score_monotonic_and_bounded(
    start_score: float, anomaly_scores: list[float]
) -> None:
    """Property 8: integrity score is non-increasing and stays within [0, 100]."""
    factory, engine = _session_factory()
    try:
        db = factory()
        session_id = _seed_session(db, start_score)
        service = IntegrityService(sessions=ExamSessionRepository(db))

        previous = start_score
        for score in anomaly_scores:
            result = await service.record_confirmed_anomaly(session_id, score)
            current = result.integrity_score

            # Bounded within [0, 100] (Requirement 14.6).
            assert INTEGRITY_SCORE_MIN <= current <= INTEGRITY_SCORE_MAX
            # Non-increasing as confirmed anomalies accumulate (Requirement 14.5).
            assert current <= previous

            # The persisted value matches what the service returned.
            persisted = ExamSessionRepository(db).get(session_id)
            assert persisted.integrity_score == current

            previous = current
    finally:
        engine.dispose()
