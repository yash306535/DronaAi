"""Exam creation and provisioning service (Requirement 3).

Owns the use-case logic behind the exam router so the API layer stays thin:

- :meth:`ExamService.create_exam` (3.1-3.4) — create an exam in ``draft`` status
  from a validated :class:`~app.schemas.exam.ExamCreate`. The title (1-200),
  topic count (1-100) and total-question count (1-1000) constraints are enforced
  by the schema, so an out-of-range request is rejected with a 422 before this
  method runs.
- :meth:`ExamService.list_exams` (3.5) — list exams whose role audience includes
  the caller's Admin/Invigilator role.
- :meth:`ExamService.get_exam` — fetch one exam (or raise ``NotFoundError``).
- :meth:`ExamService.provision_exam` (3.6-3.9) — transition a ``draft`` exam to
  ``provisioning`` and dispatch paper generation per enrolled student by
  publishing one ``exam.provision`` event per student (the Architect handles
  each off the delivery path). A non-``draft`` exam is rejected leaving its
  status unchanged (3.8); an exam with zero enrolled students is rejected
  leaving it ``draft`` (3.9).
- :meth:`ExamService.papers_status` — provisioning/audit progress for an exam.

"Enrolled students" are resolved as the users holding the Student role (the
platform has no separate enrollment table at this phase); the same set drives
the zero-enrolled guard and the provisioning fan-out.
"""

from __future__ import annotations

from app.core.errors import NotFoundError, ValidationError
from app.core.events import Event, EventBus, EventType
from app.core.logging import get_logger
from app.models.enums import ExamStatus, Role
from app.models.orm import Exam
from app.repositories.exam import ExamRepository
from app.repositories.paper import PaperRepository
from app.repositories.user import UserRepository
from app.schemas.exam import ExamCreate, ExamRead

logger = get_logger("app.services.exam")

# Stable, machine-readable domain error codes (kept stable for clients).
EXAM_NOT_FOUND_CODE = "exam_not_found"
EXAM_NOT_DRAFT_CODE = "exam_not_provisionable"
NO_STUDENTS_ENROLLED_CODE = "no_students_enrolled"

# The source name on events this service publishes (the exam/REST layer).
EVENT_SOURCE = "exam_service"

# Roles permitted to see an exam in the listing (3.5): the exam's audience is
# the staff roles (admin/invigilator).
_EXAM_AUDIENCE_ROLES = frozenset({Role.ADMIN, Role.INVIGILATOR})


class ExamService:
    """Use-case orchestration for exam creation and provisioning."""

    def __init__(
        self,
        *,
        exams: ExamRepository,
        users: UserRepository,
        papers: PaperRepository,
        event_bus: EventBus | None = None,
    ) -> None:
        self._exams = exams
        self._users = users
        self._papers = papers
        self._bus = event_bus

    # -- create --------------------------------------------------------------

    def create_exam(self, body: ExamCreate, created_by: str) -> ExamRead:
        """Create an exam in ``draft`` status (Requirement 3.1).

        Title/topic-count/total-question validation is enforced by the
        ``ExamCreate``/``ExamBlueprint`` schemas (3.2, 3.3, 3.4); an invalid
        request never reaches here (it is a 422 at the API boundary).
        """
        exam = Exam(
            title=body.title,
            subject=body.subject,
            blueprint=body.blueprint.model_dump(mode="json"),
            duration_minutes=body.duration_minutes,
            starts_at=body.starts_at,
            status=ExamStatus.DRAFT,
            created_by=created_by,
        )
        created = self._exams.add(exam)
        logger.info("exam.created", extra={"examId": created.id})
        return ExamRead.model_validate(created)

    # -- read ----------------------------------------------------------------

    def list_exams(self, role: Role) -> list[ExamRead]:
        """List exams visible to ``role`` (Requirement 3.5).

        Admin and Invigilator are in every exam's role audience, so both see all
        exams. Any other role sees none (the router restricts this endpoint to
        admin/invigilator anyway, so this is defence in depth).
        """
        if role not in _EXAM_AUDIENCE_ROLES:
            return []
        return [ExamRead.model_validate(exam) for exam in self._exams.list_all()]

    def list_available_exams(self) -> list[ExamRead]:
        """List exams currently open to sit (status ``live``).

        Available to any authenticated user — students use this to discover the
        exam(s) they can start. ``ExamRead`` carries no answer keys (only the
        blueprint, timing, and metadata), so it is safe to expose to students.
        """
        return [
            ExamRead.model_validate(exam)
            for exam in self._exams.list_all()
            if exam.status == ExamStatus.LIVE
        ]

    def get_exam(self, exam_id: str) -> Exam:
        """Return the exam row or raise :class:`NotFoundError`."""
        exam = self._exams.get(exam_id)
        if exam is None:
            raise NotFoundError("Exam not found.", code=EXAM_NOT_FOUND_CODE)
        return exam

    # -- provision -----------------------------------------------------------

    async def provision_exam(self, exam_id: str) -> ExamRead:
        """Transition ``draft`` → ``provisioning`` and dispatch generation.

        Requirement 3.6: provisioning a ``draft`` exam sets it to
        ``provisioning``. Requirement 3.7: the transition dispatches paper
        generation for each enrolled student (one ``exam.provision`` event per
        student). Requirement 3.8: a non-``draft`` exam is rejected, status
        unchanged. Requirement 3.9: an exam with zero enrolled students is
        rejected, left ``draft``.
        """
        exam = self.get_exam(exam_id)

        # 3.8: only a draft exam is provisionable; leave status unchanged.
        if exam.status != ExamStatus.DRAFT:
            raise ValidationError(
                "Exam is not in a provisionable (draft) state.",
                code=EXAM_NOT_DRAFT_CODE,
            )

        # 3.9: reject when no students are enrolled; leave the exam draft.
        students = self._enrolled_students()
        if not students:
            raise ValidationError(
                "No students are enrolled for this exam.",
                code=NO_STUDENTS_ENROLLED_CODE,
            )

        # 3.6: set status to provisioning.
        updated = self._exams.set_status(exam_id, ExamStatus.PROVISIONING)
        if updated is None:  # pragma: no cover - row existed moments ago
            raise NotFoundError("Exam not found.", code=EXAM_NOT_FOUND_CODE)

        # 3.7: dispatch paper generation per enrolled student.
        for student_id in students:
            await self._publish(
                Event(
                    type=EventType.EXAM_PROVISION,
                    payload={
                        "examId": exam_id,
                        "studentId": student_id,
                        "subject": updated.subject,
                        "blueprint": updated.blueprint,
                    },
                    source=EVENT_SOURCE,
                )
            )
        logger.info(
            "exam.provisioning_dispatched",
            extra={"examId": exam_id, "studentCount": len(students)},
        )
        return ExamRead.model_validate(updated)

    def _enrolled_students(self) -> list[str]:
        """Return the ids of students enrolled for provisioning.

        The platform has no separate enrollment table at this phase, so the
        enrolled set is the users holding the Student role. Returned as ids so
        the provisioning fan-out carries only the student identifier.
        """
        return [user.id for user in self._users.list_by_role(Role.STUDENT.value)]

    # -- status --------------------------------------------------------------

    def papers_status(self, exam_id: str) -> dict:
        """Return provisioning + audit progress for an exam.

        Reports the number of enrolled students, how many papers have been
        generated so far, and a breakdown of paper audit statuses, so an admin
        can watch provisioning progress.
        """
        exam = self.get_exam(exam_id)
        papers = self._papers.list_for_exam(exam_id)
        enrolled = len(self._enrolled_students())
        audit_breakdown: dict[str, int] = {}
        for paper in papers:
            key = str(paper.audit_status)
            audit_breakdown[key] = audit_breakdown.get(key, 0) + 1
        generated = len(papers)
        return {
            "examId": exam_id,
            "status": str(exam.status),
            "enrolledStudents": enrolled,
            "papersGenerated": generated,
            "pending": max(0, enrolled - generated),
            "auditBreakdown": audit_breakdown,
        }

    # -- helpers -------------------------------------------------------------

    async def _publish(self, event: Event) -> None:
        """Publish ``event`` if an event bus is wired; otherwise a no-op."""
        if self._bus is None:
            return
        await self._bus.publish(event)


__all__ = [
    "ExamService",
    "EXAM_NOT_FOUND_CODE",
    "EXAM_NOT_DRAFT_CODE",
    "NO_STUDENTS_ENROLLED_CODE",
    "EVENT_SOURCE",
]
