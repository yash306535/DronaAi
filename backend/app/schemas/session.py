"""Exam session, answer, and session-event schemas.

Validation rules:
- Session-event batches contain between 1 and 100 events (Req 5.8, 5.9).
- ``client_ts`` is untrusted; the server records the authoritative timestamp
  (Req 14.3, 14.4) so it is optional on input.
- ``integrity_score`` is constrained to [0, 100] (Req 14.6).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import SessionEventKind, SessionStatus

MIN_EVENT_BATCH = 1
MAX_EVENT_BATCH = 100
INTEGRITY_SCORE_MIN = 0.0
INTEGRITY_SCORE_MAX = 100.0


class SessionRead(BaseModel):
    """Session state returned to the owning student / invigilator / admin."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    exam_id: str
    student_id: str
    paper_id: str
    status: SessionStatus
    started_at: datetime | None = None
    submitted_at: datetime | None = None
    integrity_score: float = Field(ge=INTEGRITY_SCORE_MIN, le=INTEGRITY_SCORE_MAX)


class AnswerSubmit(BaseModel):
    """Submit or update an answer for an active session."""

    question_id: str
    response: str


class AnswerRead(BaseModel):
    """Persisted answer (grading fields populated post-exam)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    question_id: str
    response: str
    time_spent_ms: int
    is_correct: bool | None = None
    awarded_marks: float | None = None


class SessionEventIn(BaseModel):
    """A single inbound telemetry event. ``client_ts`` is untrusted/optional."""

    kind: SessionEventKind
    payload: dict = Field(default_factory=dict)
    client_ts: datetime | None = None


class SessionEventBatch(BaseModel):
    """Batch of 1-100 session events (Req 5.8 / 5.9)."""

    events: list[SessionEventIn] = Field(
        min_length=MIN_EVENT_BATCH, max_length=MAX_EVENT_BATCH
    )


class SessionEventRead(BaseModel):
    """Persisted event with authoritative server timestamp."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    kind: SessionEventKind
    payload: dict
    client_ts: datetime | None = None
    server_ts: datetime
