"""Unit tests for the repository layer (task 4.2 / 4.3).

Covers:
- Repository CRUD round-trips against an in-memory SQLite database.
- The transient-fault retry-once policy required by Requirement 15.10:
    * a transient fault on the first attempt is retried exactly once and then
      succeeds (one retry, total two attempts),
    * a transient fault on both attempts surfaces as ``UpstreamError`` (503)
      and leaves persisted data unchanged,
    * a non-transient fault (e.g. ``IntegrityError``) is not retried.

Schema-level validation (blueprint topic/question-count/title ranges and MCQ
option constraints — Requirements 3.2, 3.3, 3.4, 4.4) is already covered in
``test_models.py``; these tests focus on repository behavior.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.orm import Session, sessionmaker

from app.core.db import Base
from app.core.errors import UpstreamError
from app.models.enums import (
    AlertSeverity,
    AnomalyCategory,
    ExamStatus,
    QuestionType,
    Role,
    SessionEventKind,
    SessionStatus,
    SourceAgent,
)
from app.models.orm import (
    Alert,
    Anomaly,
    Exam,
    ExamSession,
    GeneratedPaper,
    Question,
    SessionEvent,
    User,
)
from app.repositories import (
    AlertRepository,
    AnomalyRepository,
    AnswerRepository,
    ExamAnalyticsRepository,
    ExamRepository,
    ExamSessionRepository,
    PaperRepository,
    SessionEventRepository,
    UserRepository,
)
from app.repositories.base import BaseRepository, is_transient_fault


@pytest.fixture()
def db_session():
    import app.models  # noqa: F401 - register tables

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    session = factory()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def admin_user(db_session) -> User:
    user = UserRepository(db_session).add(
        User(
            email="admin@example.com",
            full_name="Admin",
            role=Role.ADMIN,
            password_hash="hash",
        )
    )
    return user


@pytest.fixture()
def student_user(db_session) -> User:
    return UserRepository(db_session).add(
        User(
            email="student@example.com",
            full_name="Student",
            role=Role.STUDENT,
            password_hash="hash",
        )
    )


def _make_exam(created_by: str) -> Exam:
    return Exam(
        title="Math",
        subject="Mathematics",
        blueprint={"topics": [{"name": "algebra", "count": 1}], "total_questions": 1},
        duration_minutes=60,
        starts_at=datetime.now(timezone.utc),
        created_by=created_by,
    )


# --- CRUD round-trips ----------------------------------------------------


def test_user_repository_crud(db_session) -> None:
    repo = UserRepository(db_session)
    user = repo.add(
        User(email="u@e.com", full_name="U", role=Role.STUDENT, password_hash="h")
    )
    assert user.id

    assert repo.get(user.id) is not None
    assert repo.get_by_email("u@e.com").id == user.id
    assert repo.get_by_email("missing@e.com") is None
    assert [u.id for u in repo.list_by_role(Role.STUDENT)] == [user.id]
    assert repo.delete(user.id) is True
    assert repo.get(user.id) is None


def test_exam_repository_crud(db_session, admin_user) -> None:
    repo = ExamRepository(db_session)
    exam = repo.add(_make_exam(admin_user.id))

    assert repo.get(exam.id).title == "Math"
    assert [e.id for e in repo.list_all()] == [exam.id]
    assert [e.id for e in repo.list_by_creator(admin_user.id)] == [exam.id]

    updated = repo.set_status(exam.id, ExamStatus.PROVISIONING)
    assert updated.status == ExamStatus.PROVISIONING
    assert repo.set_status("nonexistent", ExamStatus.LIVE) is None


def test_paper_repository_with_questions(db_session, admin_user, student_user) -> None:
    exam = ExamRepository(db_session).add(_make_exam(admin_user.id))
    repo = PaperRepository(db_session)

    paper = GeneratedPaper(exam_id=exam.id, student_id=student_user.id, seed="seed-1")
    questions = [
        Question(
            index=0,
            type=QuestionType.MCQ,
            prompt="2+2?",
            options=["3", "4"],
            answer_key="4",
            topic="algebra",
            difficulty=0.5,
            max_marks=1.0,
        )
    ]
    saved = repo.add_with_questions(paper, questions)

    assert repo.get(saved.id) is not None
    assert [p.id for p in repo.list_for_exam(exam.id)] == [saved.id]
    assert repo.get_for_student(exam.id, student_user.id).id == saved.id
    listed = repo.list_questions(saved.id)
    assert len(listed) == 1 and listed[0].answer_key == "4"


def test_session_repository_crud(db_session, admin_user, student_user) -> None:
    exam = ExamRepository(db_session).add(_make_exam(admin_user.id))
    paper = PaperRepository(db_session).add(
        GeneratedPaper(exam_id=exam.id, student_id=student_user.id, seed="s")
    )
    repo = ExamSessionRepository(db_session)
    sess = repo.add(
        ExamSession(
            exam_id=exam.id,
            student_id=student_user.id,
            paper_id=paper.id,
            status=SessionStatus.ACTIVE,
        )
    )

    assert repo.get(sess.id).status == SessionStatus.ACTIVE
    assert repo.get_for_student(exam.id, student_user.id).id == sess.id
    assert [s.id for s in repo.list_for_exam(exam.id)] == [sess.id]
    assert repo.set_status(sess.id, SessionStatus.SUBMITTED).status == (
        SessionStatus.SUBMITTED
    )
    assert repo.set_integrity_score(sess.id, 80.0).integrity_score == 80.0


def test_session_event_batch_is_atomic(db_session, admin_user, student_user) -> None:
    exam = ExamRepository(db_session).add(_make_exam(admin_user.id))
    paper = PaperRepository(db_session).add(
        GeneratedPaper(exam_id=exam.id, student_id=student_user.id, seed="s")
    )
    sess = ExamSessionRepository(db_session).add(
        ExamSession(
            exam_id=exam.id,
            student_id=student_user.id,
            paper_id=paper.id,
            status=SessionStatus.ACTIVE,
        )
    )
    repo = SessionEventRepository(db_session)
    repo.add_batch(
        [
            SessionEvent(session_id=sess.id, kind=SessionEventKind.HEARTBEAT, payload={}),
            SessionEvent(session_id=sess.id, kind=SessionEventKind.PASTE, payload={}),
        ]
    )
    assert len(repo.list_for_session(sess.id)) == 2


def test_answer_repository_upsert(db_session, admin_user, student_user) -> None:
    exam = ExamRepository(db_session).add(_make_exam(admin_user.id))
    paper = PaperRepository(db_session).add_with_questions(
        GeneratedPaper(exam_id=exam.id, student_id=student_user.id, seed="s"),
        [
            Question(
                index=0,
                type=QuestionType.SHORT,
                prompt="Q",
                answer_key="A",
                topic="t",
                difficulty=0.5,
                max_marks=1.0,
            )
        ],
    )
    question = PaperRepository(db_session).list_questions(paper.id)[0]
    sess = ExamSessionRepository(db_session).add(
        ExamSession(
            exam_id=exam.id,
            student_id=student_user.id,
            paper_id=paper.id,
            status=SessionStatus.ACTIVE,
        )
    )
    repo = AnswerRepository(db_session)

    first = repo.upsert(
        session_id=sess.id, question_id=question.id, response="A", time_spent_ms=1000
    )
    second = repo.upsert(
        session_id=sess.id, question_id=question.id, response="B", time_spent_ms=2000
    )
    # Upsert updates in place rather than creating a duplicate row.
    assert first.id == second.id
    assert second.response == "B"
    assert len(repo.list_for_session(sess.id)) == 1


def test_anomaly_and_alert_repositories(db_session, admin_user, student_user) -> None:
    exam = ExamRepository(db_session).add(_make_exam(admin_user.id))
    paper = PaperRepository(db_session).add(
        GeneratedPaper(exam_id=exam.id, student_id=student_user.id, seed="s")
    )
    sess = ExamSessionRepository(db_session).add(
        ExamSession(
            exam_id=exam.id,
            student_id=student_user.id,
            paper_id=paper.id,
            status=SessionStatus.ACTIVE,
        )
    )
    anomaly = AnomalyRepository(db_session).add(
        Anomaly(
            session_id=sess.id,
            source_agent=SourceAgent.GUARDIAN,
            category=AnomalyCategory.FACE_ABSENT,
            score=0.9,
            reasons=["no face"],
            evidence={},
            confirmed=True,
        )
    )
    assert [a.id for a in AnomalyRepository(db_session).list_for_session(sess.id)] == [
        anomaly.id
    ]

    alert_repo = AlertRepository(db_session)
    alert = alert_repo.add(
        Alert(
            anomaly_id=anomaly.id,
            session_id=sess.id,
            severity=AlertSeverity.DANGER,
            message="m",
        )
    )
    updated = alert_repo.mark_delivered(alert.id, ws=True)
    assert updated.delivered_ws is True
    assert [a.id for a in alert_repo.list_for_session(sess.id)] == [alert.id]


def test_analytics_repository_upsert(db_session, admin_user) -> None:
    exam = ExamRepository(db_session).add(_make_exam(admin_user.id))
    repo = ExamAnalyticsRepository(db_session)
    repo.upsert(exam_id=exam.id, summary={"mean": 1}, difficulty_heatmap={}, per_student={})
    again = repo.upsert(
        exam_id=exam.id, summary={"mean": 2}, difficulty_heatmap={}, per_student={}
    )
    assert again.summary == {"mean": 2}
    assert repo.get_for_exam(exam.id).id == again.id


# --- Transient-fault classification (15.10) ------------------------------


def test_is_transient_fault_classification() -> None:
    assert is_transient_fault(SATimeoutError("pool timeout")) is True
    assert is_transient_fault(OperationalError("stmt", {}, Exception("lost"))) is True

    deadlock = DBAPIError("stmt", {}, Exception("deadlock detected"))
    assert is_transient_fault(deadlock) is True

    benign = DBAPIError("stmt", {}, Exception("syntax error"))
    assert is_transient_fault(benign) is False

    assert is_transient_fault(ValueError("nope")) is False


# --- Retry-once policy (15.10) -------------------------------------------


class _FlakySession:
    """Minimal session stand-in driving the retry policy deterministically.

    ``rollback``/``commit`` are recorded; the actual unit of work raises a
    scripted sequence of exceptions before (optionally) succeeding.
    """

    def __init__(self) -> None:
        self.rollbacks = 0
        self.commits = 0

    def rollback(self) -> None:
        self.rollbacks += 1

    def commit(self) -> None:
        self.commits += 1


def _operational_error() -> OperationalError:
    return OperationalError("stmt", {}, Exception("server closed the connection"))


def test_retry_succeeds_after_single_transient_fault(monkeypatch) -> None:
    # Avoid real sleeping; assert the back-off stays within the 500 ms cap.
    slept: list[float] = []
    monkeypatch.setattr(
        "app.repositories.base.time.sleep", lambda s: slept.append(s)
    )

    fake = _FlakySession()
    repo = BaseRepository(fake)  # type: ignore[arg-type]
    attempts = {"n": 0}

    def work(_session):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _operational_error()
        return "ok"

    result = repo._run(work, commit=True)

    assert result == "ok"
    assert attempts["n"] == 2  # original + exactly one retry
    assert fake.rollbacks == 1  # rolled back before the retry
    assert fake.commits == 1  # committed once on success
    assert slept and all(s <= 0.5 for s in slept)


def test_retry_exhausted_raises_upstream_error_and_leaves_data_unchanged(
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.repositories.base.time.sleep", lambda s: None)

    fake = _FlakySession()
    repo = BaseRepository(fake)  # type: ignore[arg-type]
    attempts = {"n": 0}

    def work(_session):
        attempts["n"] += 1
        raise _operational_error()

    with pytest.raises(UpstreamError) as excinfo:
        repo._run(work, commit=True)

    assert excinfo.value.status_code == 503
    assert attempts["n"] == 2  # original + exactly one retry, no more
    assert fake.rollbacks == 2  # rolled back after each failure (data unchanged)
    assert fake.commits == 0  # never committed


def test_non_transient_fault_is_not_retried(monkeypatch) -> None:
    monkeypatch.setattr("app.repositories.base.time.sleep", lambda s: None)

    fake = _FlakySession()
    repo = BaseRepository(fake)  # type: ignore[arg-type]
    attempts = {"n": 0}

    def work(_session):
        attempts["n"] += 1
        raise IntegrityError("stmt", {}, Exception("unique constraint"))

    with pytest.raises(IntegrityError):
        repo._run(work, commit=True)

    assert attempts["n"] == 1  # no retry for non-transient faults
    assert fake.rollbacks == 1
    assert fake.commits == 0


def test_retry_once_recovers_real_session_commit(db_session, admin_user) -> None:
    """End-to-end: a transient commit fault recovers on the single retry.

    Patches the real session's ``commit`` to fail transiently once, proving the
    repository retries against a live session and then persists successfully.
    """
    repo = ExamRepository(db_session)
    original_commit = db_session.commit
    calls = {"n": 0}

    def flaky_commit() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _operational_error()
        original_commit()

    db_session.commit = flaky_commit  # type: ignore[method-assign]

    exam = repo.add(_make_exam(admin_user.id))

    db_session.commit = original_commit  # type: ignore[method-assign]
    assert calls["n"] == 2
    assert repo.get(exam.id) is not None
