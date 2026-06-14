"""Unit tests for ORM models, DB setup, and Pydantic schema validation.

Covers the validation rules required by task 4.1:
- email RFC validity, password 8-128 chars (Req 1.8)
- blueprint >=1 topic & total count >=1 (Req 3.1, 3.2, 3.3)
- MCQ options 2-4 with exactly one correct (Req 4.4)
- anomaly score in [0,1] / severity enum (Req 14.7, 14.8)
- answer-key never present on student-facing schema (Req 4.9, 5.3, 14.1)
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.models import (
    AlertSeverity,
    Exam,
    ExamStatus,
    Role,
    User,
)
from app.schemas.anomaly import AlertCreate, AnomalyCreate
from app.schemas.exam import ExamBlueprint, ExamCreate
from app.schemas.question import QuestionCreate, StudentQuestion
from app.schemas.session import SessionEventBatch
from app.schemas.user import UserCreate


@pytest.fixture()
def in_memory_engine():
    # Import models for table registration side effects.
    import app.models  # noqa: F401

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


def test_all_tables_create_cleanly(in_memory_engine) -> None:
    """metadata.create_all provisions every model table."""
    table_names = set(inspect(in_memory_engine).get_table_names())
    expected = {
        "users",
        "exams",
        "generated_papers",
        "questions",
        "exam_sessions",
        "session_events",
        "anomalies",
        "alerts",
        "answers",
        "exam_analytics",
    }
    assert expected.issubset(table_names)


def test_required_indices_exist(in_memory_engine) -> None:
    """session_id, exam_id, and (exam_id, student_id) indices are present."""
    insp = inspect(in_memory_engine)

    def index_columns(table: str) -> list[list[str]]:
        return [ix["column_names"] for ix in insp.get_indexes(table)]

    # session_id indices
    for table in ("session_events", "anomalies", "alerts", "answers"):
        assert any("session_id" in cols for cols in index_columns(table)), table

    # exam_id indices
    for table in ("generated_papers", "exam_sessions"):
        assert any("exam_id" in cols for cols in index_columns(table)), table

    # composite (exam_id, student_id)
    for table in ("generated_papers", "exam_sessions"):
        assert ["exam_id", "student_id"] in index_columns(table), table


def test_can_persist_and_query_user(in_memory_engine) -> None:
    session = sessionmaker(bind=in_memory_engine)()
    user = User(
        email="admin@example.com",
        full_name="Admin",
        role=Role.ADMIN,
        password_hash="hash",
    )
    session.add(user)
    session.commit()
    fetched = session.query(User).filter_by(email="admin@example.com").one()
    assert fetched.role == Role.ADMIN
    assert fetched.id  # uuid default assigned
    session.close()


def test_exam_default_status_is_draft(in_memory_engine) -> None:
    session = sessionmaker(bind=in_memory_engine)()
    admin = User(email="a@b.com", full_name="A", role=Role.ADMIN, password_hash="h")
    session.add(admin)
    session.commit()
    exam = Exam(
        title="Math",
        subject="Mathematics",
        blueprint={"topics": [{"name": "algebra", "count": 1}], "total_questions": 1},
        duration_minutes=60,
        starts_at=datetime.now(timezone.utc),
        created_by=admin.id,
    )
    session.add(exam)
    session.commit()
    assert exam.status == ExamStatus.DRAFT
    session.close()


# --- Schema validation ---------------------------------------------------


def test_user_password_length_bounds() -> None:
    base = dict(email="u@example.com", full_name="U", role=Role.STUDENT)
    with pytest.raises(ValidationError):
        UserCreate(**base, password="short")  # < 8
    with pytest.raises(ValidationError):
        UserCreate(**base, password="x" * 129)  # > 128
    ok = UserCreate(**base, password="goodpass1")
    assert ok.password == "goodpass1"


def test_user_email_must_be_rfc_valid() -> None:
    with pytest.raises(ValidationError):
        UserCreate(email="not-an-email", full_name="U", role=Role.STUDENT, password="goodpass1")


def test_blueprint_requires_at_least_one_topic() -> None:
    with pytest.raises(ValidationError):
        ExamBlueprint(topics=[], total_questions=1)


def test_blueprint_total_questions_min_one() -> None:
    with pytest.raises(ValidationError):
        ExamBlueprint(topics=[{"name": "t", "count": 0}], total_questions=0)


def test_blueprint_topic_count_upper_bound() -> None:
    topics = [{"name": f"t{i}", "count": 0} for i in range(101)]
    with pytest.raises(ValidationError):
        ExamBlueprint(topics=topics, total_questions=1)


def test_exam_title_bounds() -> None:
    bp = ExamBlueprint(topics=[{"name": "t", "count": 1}], total_questions=1)
    common = dict(subject="S", blueprint=bp, duration_minutes=30, starts_at=datetime.now(timezone.utc))
    with pytest.raises(ValidationError):
        ExamCreate(title="", **common)
    with pytest.raises(ValidationError):
        ExamCreate(title="x" * 201, **common)
    assert ExamCreate(title="Valid", **common).title == "Valid"


def test_mcq_requires_two_to_four_options_exactly_one_correct() -> None:
    common = dict(index=0, type="mcq", prompt="Q", topic="t", difficulty=0.5, max_marks=1.0)
    # too few options
    with pytest.raises(ValidationError):
        QuestionCreate(options=["A"], answer_key="A", **common)
    # too many options
    with pytest.raises(ValidationError):
        QuestionCreate(options=["A", "B", "C", "D", "E"], answer_key="A", **common)
    # answer key not among options -> zero correct
    with pytest.raises(ValidationError):
        QuestionCreate(options=["A", "B"], answer_key="C", **common)
    # duplicate correct -> two correct
    with pytest.raises(ValidationError):
        QuestionCreate(options=["A", "A"], answer_key="A", **common)
    # valid
    q = QuestionCreate(options=["A", "B"], answer_key="A", **common)
    assert q.answer_key == "A"


def test_student_question_has_no_answer_key_field() -> None:
    assert "answer_key" not in StudentQuestion.model_fields


def test_anomaly_score_range() -> None:
    common = dict(session_id="s", source_agent="guardian", category="face_absent")
    with pytest.raises(ValidationError):
        AnomalyCreate(score=-0.1, **common)
    with pytest.raises(ValidationError):
        AnomalyCreate(score=1.1, **common)
    assert AnomalyCreate(score=0.5, **common).score == 0.5


def test_alert_severity_enum_enforced() -> None:
    with pytest.raises(ValidationError):
        AlertCreate(anomaly_id="a", session_id="s", severity="critical", message="m")
    ok = AlertCreate(anomaly_id="a", session_id="s", severity="danger", message="m")
    assert ok.severity == AlertSeverity.DANGER


def test_session_event_batch_size_limits() -> None:
    with pytest.raises(ValidationError):
        SessionEventBatch(events=[])
    too_many = [{"kind": "heartbeat"} for _ in range(101)]
    with pytest.raises(ValidationError):
        SessionEventBatch(events=too_many)
    ok = SessionEventBatch(events=[{"kind": "heartbeat"}])
    assert len(ok.events) == 1
