"""Tests for the demo-data seed script (task 26.2).

Runs ``app.seed.seed`` against an in-memory SQLite database and asserts the
expected rows exist (admin/invigilator/students, exams, papers + questions,
sessions, anomalies/alerts, graded answers, and persisted analytics). Also
verifies that re-running the seed is idempotent-ish: it does not crash on a
duplicate key and leaves exactly one copy of the demo dataset.

These tests are offline and deterministic — the seed synthesizes demo content
directly rather than calling any LLM/Vision API.

Requirements: 3.1, 4.1, 5.1.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.db import Base
from app.core.security import verify_password
from app.models.enums import (
    AlertSeverity,
    ExamStatus,
    Role,
    SessionStatus,
)
from app.models.orm import (
    Alert,
    Anomaly,
    Answer,
    Exam,
    ExamAnalytics,
    ExamSession,
    GeneratedPaper,
    Question,
    User,
)
from app.seed import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    EXAM_COMPLETED_ID,
    EXAM_LIVE_ID,
    INVIGILATOR_EMAIL,
    STUDENT_NAMES,
    STUDENT_PASSWORD,
    TOTAL_QUESTIONS,
    seed,
)


@pytest.fixture()
def db_session() -> Session:
    import app.models  # noqa: F401 - register tables on Base.metadata

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _count(session: Session, model) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def test_seed_creates_expected_users(db_session: Session) -> None:
    summary = seed(db_session)

    admin = db_session.execute(
        select(User).where(User.email == ADMIN_EMAIL)
    ).scalar_one()
    assert admin.role == Role.ADMIN

    invigilator = db_session.execute(
        select(User).where(User.email == INVIGILATOR_EMAIL)
    ).scalar_one()
    assert invigilator.role == Role.INVIGILATOR

    students = db_session.execute(
        select(User).where(User.role == Role.STUDENT)
    ).scalars().all()
    assert len(students) == len(STUDENT_NAMES)

    assert summary.users == 2 + len(STUDENT_NAMES)
    assert summary.students == len(STUDENT_NAMES)


def test_seed_passwords_are_bcrypt_hashed_and_verifiable(db_session: Session) -> None:
    seed(db_session)

    admin = db_session.execute(
        select(User).where(User.email == ADMIN_EMAIL)
    ).scalar_one()
    # Stored as a hash, never the raw password.
    assert admin.password_hash != ADMIN_PASSWORD
    assert admin.password_hash.startswith("$2")  # bcrypt prefix
    assert verify_password(ADMIN_PASSWORD, admin.password_hash) is True

    student = db_session.execute(
        select(User).where(User.role == Role.STUDENT)
    ).scalars().first()
    assert verify_password(STUDENT_PASSWORD, student.password_hash) is True


def test_seed_creates_live_and_completed_exams(db_session: Session) -> None:
    seed(db_session)

    live = db_session.get(Exam, EXAM_LIVE_ID)
    completed = db_session.get(Exam, EXAM_COMPLETED_ID)
    assert live is not None and live.status == ExamStatus.LIVE
    assert completed is not None and completed.status == ExamStatus.COMPLETED

    # Blueprint satisfies the design's validation rules (>=1 topic, >=1 total).
    assert len(live.blueprint["topics"]) >= 1
    assert live.blueprint["total_questions"] == TOTAL_QUESTIONS


def test_seed_creates_papers_and_questions_for_every_live_student(
    db_session: Session,
) -> None:
    seed(db_session)

    papers = db_session.execute(
        select(GeneratedPaper).where(GeneratedPaper.exam_id == EXAM_LIVE_ID)
    ).scalars().all()
    assert len(papers) == len(STUDENT_NAMES)

    for paper in papers:
        questions = db_session.execute(
            select(Question).where(Question.paper_id == paper.id)
        ).scalars().all()
        assert len(questions) == TOTAL_QUESTIONS
        # MCQ questions must carry >= 2 options whose key is one of them.
        for q in questions:
            if q.options is not None:
                assert len(q.options) >= 2
                assert q.answer_key in q.options


def test_seed_papers_are_unique_per_student(db_session: Session) -> None:
    """Requirement 3.1: each student gets a uniquely-seeded paper."""
    seed(db_session)
    papers = db_session.execute(
        select(GeneratedPaper).where(GeneratedPaper.exam_id == EXAM_LIVE_ID)
    ).scalars().all()
    seeds = {p.seed for p in papers}
    assert len(seeds) == len(papers)


def test_seed_creates_sessions_in_useful_states(db_session: Session) -> None:
    seed(db_session)

    live_sessions = db_session.execute(
        select(ExamSession).where(ExamSession.exam_id == EXAM_LIVE_ID)
    ).scalars().all()
    statuses = {s.status for s in live_sessions}
    assert SessionStatus.ACTIVE in statuses
    assert SessionStatus.NOT_STARTED in statuses

    completed_sessions = db_session.execute(
        select(ExamSession).where(ExamSession.exam_id == EXAM_COMPLETED_ID)
    ).scalars().all()
    assert completed_sessions
    assert all(s.status == SessionStatus.SUBMITTED for s in completed_sessions)


def test_seed_creates_anomaly_and_alert_timeline(db_session: Session) -> None:
    seed(db_session)

    anomalies = db_session.execute(select(Anomaly)).scalars().all()
    alerts = db_session.execute(select(Alert)).scalars().all()
    assert len(anomalies) >= 3
    assert len(alerts) >= 1

    # Spans multiple severities and includes at least one confirmed anomaly.
    severities = {a.severity for a in alerts}
    assert AlertSeverity.DANGER in severities
    assert any(a.confirmed for a in anomalies)
    # Every alert references a real anomaly.
    anomaly_ids = {a.id for a in anomalies}
    assert all(al.anomaly_id in anomaly_ids for al in alerts)


def test_seed_creates_graded_answers_and_analytics(db_session: Session) -> None:
    seed(db_session)

    answers = db_session.execute(select(Answer)).scalars().all()
    assert answers
    assert all(a.is_correct is not None for a in answers)

    analytics = db_session.execute(
        select(ExamAnalytics).where(ExamAnalytics.exam_id == EXAM_COMPLETED_ID)
    ).scalar_one()
    assert analytics.summary
    assert analytics.per_student


def test_seed_is_idempotent_on_rerun(db_session: Session) -> None:
    """Re-running the seed must not crash and must not duplicate rows."""
    summary_first = seed(db_session)
    counts_first = {
        model: _count(db_session, model)
        for model in (
            User,
            Exam,
            GeneratedPaper,
            Question,
            ExamSession,
            Anomaly,
            Alert,
            Answer,
            ExamAnalytics,
        )
    }

    # Second run against the same database — should replace, not duplicate.
    summary_second = seed(db_session)
    counts_second = {
        model: _count(db_session, model)
        for model in counts_first
    }

    assert counts_first == counts_second
    assert summary_first.users == summary_second.users
    assert summary_first.papers == summary_second.papers
    assert summary_first.anomalies == summary_second.anomalies


def test_summary_render_includes_credentials(db_session: Session) -> None:
    summary = seed(db_session)
    rendered = summary.render()
    assert ADMIN_EMAIL in rendered
    assert "Demo credentials" in rendered
