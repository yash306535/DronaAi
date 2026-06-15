"""Exam creation and provisioning endpoints (Requirement 3).

Thin HTTP layer over :class:`~app.services.exam_service.ExamService`:

| Method | Path | Role | Purpose |
|--------|------|------|---------|
| POST | ``/exams`` | admin | Create exam + blueprint → ``draft`` (3.1-3.4) |
| GET  | ``/exams`` | admin, invigilator | List exams by role audience (3.5) |
| GET  | ``/exams/{id}`` | admin, invigilator | Exam detail |
| POST | ``/exams/{id}/provision`` | admin | Draft → provisioning, dispatch (3.6-3.9) |
| GET  | ``/exams/{id}/papers/status`` | admin | Provisioning + audit progress |

Roles are enforced by :func:`require_role`; an invalid blueprint/title is
rejected with a 422 by the ``ExamCreate`` schema before any handler runs (3.2,
3.3, 3.4).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import AuthUser, require_role
from app.api.runtime import get_event_bus
from app.core.db import get_db
from app.core.events import EventBus
from app.models.enums import Role
from app.repositories.alert import AlertRepository
from app.repositories.exam import ExamRepository
from app.repositories.paper import PaperRepository
from app.repositories.session import ExamSessionRepository
from app.repositories.user import UserRepository
from app.schemas.anomaly import AlertRead
from app.schemas.exam import ExamCreate, ExamRead
from app.schemas.session import SessionRead
from app.services.exam_service import ExamService

router = APIRouter(prefix="/exams", tags=["exams"])


def get_exam_service(
    db: Session = Depends(get_db),
    bus: EventBus | None = Depends(get_event_bus),
) -> ExamService:
    """Build an :class:`ExamService` bound to the request's DB session/bus."""
    return ExamService(
        exams=ExamRepository(db),
        users=UserRepository(db),
        papers=PaperRepository(db),
        event_bus=bus,
    )


@router.post("", response_model=ExamRead, status_code=status.HTTP_201_CREATED)
def create_exam(
    body: ExamCreate,
    user: AuthUser = Depends(require_role(Role.ADMIN)),
    service: ExamService = Depends(get_exam_service),
) -> ExamRead:
    """Create an exam with a blueprint in ``draft`` status (3.1)."""
    return service.create_exam(body, created_by=user.id)


@router.get("", response_model=list[ExamRead])
def list_exams(
    user: AuthUser = Depends(require_role(Role.ADMIN, Role.INVIGILATOR)),
    service: ExamService = Depends(get_exam_service),
) -> list[ExamRead]:
    """List exams whose role audience includes the caller's role (3.5)."""
    return service.list_exams(user.role)


@router.get("/available", response_model=list[ExamRead])
def list_available_exams(
    user: AuthUser = Depends(require_role(*list(Role))),
    service: ExamService = Depends(get_exam_service),
) -> list[ExamRead]:
    """List exams open to sit (``live``) for any authenticated user.

    Declared before ``/{exam_id}`` so the literal path is not captured as an
    exam id. Students use this to discover and start an exam.
    """
    return service.list_available_exams()


@router.get("/{exam_id}", response_model=ExamRead)
def get_exam(
    exam_id: str,
    user: AuthUser = Depends(require_role(Role.ADMIN, Role.INVIGILATOR)),
    service: ExamService = Depends(get_exam_service),
) -> ExamRead:
    """Return a single exam's detail."""
    return ExamRead.model_validate(service.get_exam(exam_id))


@router.post("/{exam_id}/provision", response_model=ExamRead)
async def provision_exam(
    exam_id: str,
    user: AuthUser = Depends(require_role(Role.ADMIN)),
    service: ExamService = Depends(get_exam_service),
) -> ExamRead:
    """Provision a draft exam → ``provisioning`` and dispatch generation (3.6-3.9)."""
    return await service.provision_exam(exam_id)


@router.get("/{exam_id}/papers/status")
def papers_status(
    exam_id: str,
    user: AuthUser = Depends(require_role(Role.ADMIN)),
    service: ExamService = Depends(get_exam_service),
) -> dict:
    """Return provisioning + audit progress for an exam."""
    return service.papers_status(exam_id)


@router.get("/{exam_id}/sessions", response_model=list[SessionRead])
def list_exam_sessions(
    exam_id: str,
    user: AuthUser = Depends(require_role(Role.ADMIN, Role.INVIGILATOR)),
    db: Session = Depends(get_db),
) -> list[SessionRead]:
    """List all sessions for an exam (admin/invigilator monitoring view)."""
    rows = ExamSessionRepository(db).list_for_exam(exam_id)
    return [SessionRead.model_validate(row) for row in rows]


@router.get("/{exam_id}/alerts", response_model=list[AlertRead])
def list_exam_alerts(
    exam_id: str,
    user: AuthUser = Depends(require_role(Role.ADMIN, Role.INVIGILATOR)),
    db: Session = Depends(get_db),
) -> list[AlertRead]:
    """List alerts across all sessions of an exam, newest first."""
    sessions = ExamSessionRepository(db).list_for_exam(exam_id)
    alert_repo = AlertRepository(db)
    alerts: list[AlertRead] = []
    for sess in sessions:
        for alert in alert_repo.list_for_session(sess.id):
            alerts.append(AlertRead.model_validate(alert))
    alerts.sort(key=lambda a: a.created_at, reverse=True)
    return alerts


__all__ = ["router", "get_exam_service"]
