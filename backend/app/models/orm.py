"""SQLAlchemy ORM models for DRONA AI.

These mirror the design document's Data Models section. Types are chosen for
cross-backend portability (SQLite local -> PostgreSQL deploy): ``JSON`` for
structured columns, timezone-aware ``DateTime`` for timestamps, and string
UUID primary keys.

Indices required by the task:
- ``session_id`` on SessionEvent, Anomaly, Alert, Answer.
- ``exam_id`` on GeneratedPaper, ExamSession.
- ``(exam_id, student_id)`` composite on GeneratedPaper and ExamSession.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.enums import (
    AlertSeverity,
    AnomalyCategory,
    AuditStatus,
    ExamStatus,
    QuestionType,
    Role,
    SessionEventKind,
    SessionStatus,
    SourceAgent,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[Role] = mapped_column(String(20), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    papers: Mapped[list["GeneratedPaper"]] = relationship(
        back_populates="student", cascade="all, delete-orphan"
    )
    sessions: Mapped[list["ExamSession"]] = relationship(
        back_populates="student", cascade="all, delete-orphan"
    )


class Exam(Base):
    __tablename__ = "exams"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    blueprint: Mapped[dict] = mapped_column(JSON, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[ExamStatus] = mapped_column(
        String(20), default=ExamStatus.DRAFT, nullable=False
    )
    created_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )

    papers: Mapped[list["GeneratedPaper"]] = relationship(
        back_populates="exam", cascade="all, delete-orphan"
    )
    sessions: Mapped[list["ExamSession"]] = relationship(
        back_populates="exam", cascade="all, delete-orphan"
    )
    analytics: Mapped["ExamAnalytics | None"] = relationship(
        back_populates="exam", uselist=False, cascade="all, delete-orphan"
    )


class GeneratedPaper(Base):
    __tablename__ = "generated_papers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    exam_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("exams.id"), index=True, nullable=False
    )
    student_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    seed: Mapped[str] = mapped_column(String(128), nullable=False)
    audit_status: Mapped[AuditStatus] = mapped_column(
        String(20), default=AuditStatus.PENDING, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    exam: Mapped["Exam"] = relationship(back_populates="papers")
    student: Mapped["User"] = relationship(back_populates="papers")
    questions: Mapped[list["Question"]] = relationship(
        back_populates="paper",
        cascade="all, delete-orphan",
        order_by="Question.index",
    )

    __table_args__ = (
        Index("ix_generated_papers_exam_student", "exam_id", "student_id"),
    )


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    paper_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("generated_papers.id"), index=True, nullable=False
    )
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[QuestionType] = mapped_column(String(20), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Stored server-side only; never serialized to a student (Requirement 4.9/14.1).
    answer_key: Mapped[str] = mapped_column(Text, nullable=False)
    topic: Mapped[str] = mapped_column(String(200), nullable=False)
    difficulty: Mapped[float] = mapped_column(Float, nullable=False)
    max_marks: Mapped[float] = mapped_column(Float, nullable=False)

    paper: Mapped["GeneratedPaper"] = relationship(back_populates="questions")
    answers: Mapped[list["Answer"]] = relationship(
        back_populates="question", cascade="all, delete-orphan"
    )


class ExamSession(Base):
    __tablename__ = "exam_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    exam_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("exams.id"), index=True, nullable=False
    )
    student_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    paper_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("generated_papers.id"), nullable=False
    )
    status: Mapped[SessionStatus] = mapped_column(
        String(20), default=SessionStatus.NOT_STARTED, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    integrity_score: Mapped[float] = mapped_column(
        Float, default=100.0, nullable=False
    )

    exam: Mapped["Exam"] = relationship(back_populates="sessions")
    student: Mapped["User"] = relationship(back_populates="sessions")
    events: Mapped[list["SessionEvent"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    anomalies: Mapped[list["Anomaly"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    answers: Mapped[list["Answer"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_exam_sessions_exam_student", "exam_id", "student_id"),
    )


class SessionEvent(Base):
    __tablename__ = "session_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("exam_sessions.id"), index=True, nullable=False
    )
    kind: Mapped[SessionEventKind] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # client_ts is untrusted; server_ts is authoritative (Requirement 14.3/14.4).
    client_ts: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    server_ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    session: Mapped["ExamSession"] = relationship(back_populates="events")


class Anomaly(Base):
    __tablename__ = "anomalies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("exam_sessions.id"), index=True, nullable=False
    )
    source_agent: Mapped[SourceAgent] = mapped_column(String(20), nullable=False)
    category: Mapped[AnomalyCategory] = mapped_column(String(30), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    reasons: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    session: Mapped["ExamSession"] = relationship(back_populates="anomalies")
    alert: Mapped["Alert | None"] = relationship(
        back_populates="anomaly", uselist=False, cascade="all, delete-orphan"
    )


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    anomaly_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("anomalies.id"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("exam_sessions.id"), index=True, nullable=False
    )
    severity: Mapped[AlertSeverity] = mapped_column(String(20), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    delivered_ws: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    delivered_email: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    anomaly: Mapped["Anomaly"] = relationship(back_populates="alert")


class Answer(Base):
    __tablename__ = "answers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("exam_sessions.id"), index=True, nullable=False
    )
    question_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("questions.id"), nullable=False
    )
    response: Mapped[str] = mapped_column(Text, nullable=False)
    time_spent_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    awarded_marks: Mapped[float | None] = mapped_column(Float, nullable=True)

    session: Mapped["ExamSession"] = relationship(back_populates="answers")
    question: Mapped["Question"] = relationship(back_populates="answers")


class ExamAnalytics(Base):
    __tablename__ = "exam_analytics"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    exam_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("exams.id"), index=True, unique=True, nullable=False
    )
    summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    difficulty_heatmap: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    per_student: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    exam: Mapped["Exam"] = relationship(back_populates="analytics")
