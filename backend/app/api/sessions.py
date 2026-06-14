"""Student exam-session endpoints (Requirement 5).

Thin HTTP layer over :class:`~app.services.session_service.SessionService`:

| Method | Path | Role | Purpose |
|--------|------|------|---------|
| POST | ``/sessions/{exam_id}/start`` | student | Start session, return own paper (5.1, 5.2, 5.3) |
| GET  | ``/sessions/{id}`` | student(own), invigilator, admin | Session state (2.4, 2.5) |
| POST | ``/sessions/{id}/answers`` | student | Submit/update an answer (5.4, 5.5) |
| POST | ``/sessions/{id}/submit`` | student | Finalize exam → ``exam.completed`` (5.6, 5.7) |
| POST | ``/sessions/{id}/events`` | student | Ingest 1-100 telemetry events (5.8, 5.9, 14.3, 14.4) |
| POST | ``/sessions/{id}/terminate`` | invigilator, admin | Force-end a session (5.10) |

Routes resolve the owning student id from the persisted session and call
:func:`enforce_student_ownership` before returning resource data so a student
can only touch their own session (2.4, 2.5). The student-facing paper is built
from the ``StudentPaper`` schema (no answer keys) and passed through
:func:`guard_student_payload` as a pre-transmission guard (14.1, 14.2) before it
is returned.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import (
    AuthUser,
    enforce_student_ownership,
    get_current_user,
    require_role,
)
from app.api.runtime import get_event_bus
from app.core.db import get_db
from app.core.events import EventBus
from app.models.enums import Role
from app.repositories.answer import AnswerRepository
from app.repositories.paper import PaperRepository
from app.repositories.session import ExamSessionRepository
from app.repositories.session_event import SessionEventRepository
from app.schemas.session import (
    AnswerRead,
    AnswerSubmit,
    SessionEventBatch,
    SessionEventRead,
    SessionRead,
)
from app.services.serialization import guard_student_payload
from app.services.session_service import SessionService

router = APIRouter(prefix="/sessions", tags=["sessions"])


def get_session_service(
    db: Session = Depends(get_db),
    bus: EventBus | None = Depends(get_event_bus),
) -> SessionService:
    """Build a :class:`SessionService` bound to the request's DB session/bus."""
    return SessionService(
        sessions=ExamSessionRepository(db),
        papers=PaperRepository(db),
        answers=AnswerRepository(db),
        events=SessionEventRepository(db),
        event_bus=bus,
    )


@router.post("/{exam_id}/start")
def start_session(
    exam_id: str,
    user: AuthUser = Depends(require_role(Role.STUDENT)),
    service: SessionService = Depends(get_session_service),
) -> dict:
    """Start a session and return the student's own answer-key-free paper (5.1)."""
    session_read, paper = service.start_session(exam_id, user.id)
    body = {
        **session_read.model_dump(mode="json"),
        "paper": paper.model_dump(mode="json"),
    }
    # Pre-transmission guard: block any answer-key field before it ships (14.2).
    return guard_student_payload(body)


@router.get("/{session_id}", response_model=SessionRead)
def get_session(
    session_id: str,
    user: AuthUser = Depends(require_role(*list(Role))),
    service: SessionService = Depends(get_session_service),
) -> SessionRead:
    """Return session state; students may only read their own session (2.4, 2.5)."""
    session_row = service.get_session(session_id)
    enforce_student_ownership(user, session_row.student_id)
    return SessionRead.model_validate(session_row)


@router.post("/{session_id}/answers", response_model=AnswerRead)
def submit_answer(
    session_id: str,
    body: AnswerSubmit,
    user: AuthUser = Depends(require_role(Role.STUDENT)),
    service: SessionService = Depends(get_session_service),
) -> AnswerRead:
    """Persist/update an answer while the session is active (5.4, 5.5)."""
    session_row = service.get_session(session_id)
    enforce_student_ownership(user, session_row.student_id)
    return service.submit_answer(session_row, body.question_id, body.response)


@router.post("/{session_id}/submit", response_model=SessionRead)
async def submit_exam(
    session_id: str,
    user: AuthUser = Depends(require_role(Role.STUDENT)),
    service: SessionService = Depends(get_session_service),
) -> SessionRead:
    """Finalize an active session → ``submitted`` and emit ``exam.completed`` (5.6, 5.7)."""
    session_row = service.get_session(session_id)
    enforce_student_ownership(user, session_row.student_id)
    return await service.submit_exam(session_row)


@router.post("/{session_id}/events", response_model=list[SessionEventRead])
async def ingest_events(
    session_id: str,
    batch: SessionEventBatch,
    user: AuthUser = Depends(require_role(Role.STUDENT)),
    service: SessionService = Depends(get_session_service),
) -> list[SessionEventRead]:
    """Ingest a 1-100 event batch with authoritative server timestamps (5.8, 5.9, 14.3, 14.4).

    A batch larger than 100 events is rejected with a 422 by the
    ``SessionEventBatch`` schema before this handler runs, so none of its events
    are persisted (5.9).
    """
    session_row = service.get_session(session_id)
    enforce_student_ownership(user, session_row.student_id)
    return await service.ingest_events(session_row, batch)


@router.post("/{session_id}/terminate", response_model=SessionRead)
def terminate_session(
    session_id: str,
    user: AuthUser = Depends(require_role(Role.ADMIN, Role.INVIGILATOR)),
    service: SessionService = Depends(get_session_service),
) -> SessionRead:
    """Invigilator/admin force-end a session → ``terminated`` (5.10)."""
    session_row = service.get_session(session_id)
    return service.terminate_session(session_row)


__all__ = ["router", "get_session_service"]
