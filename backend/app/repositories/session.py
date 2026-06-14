"""ExamSession aggregate repository.

Parameterized ORM/Core access only (15.7); transient-fault retry-once (15.10).
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.enums import SessionStatus
from app.models.orm import ExamSession
from app.repositories.base import BaseRepository


class ExamSessionRepository(BaseRepository):
    """CRUD access for :class:`ExamSession` records."""

    def add(self, session_row: ExamSession) -> ExamSession:
        """Persist a new exam session and return it."""

        def _work(session):
            session.add(session_row)
            return session_row

        result = self._run(_work, commit=True)
        self.session.refresh(result)
        return result

    def get(self, session_id: str) -> ExamSession | None:
        """Return the session with ``session_id`` or ``None``."""

        def _work(session):
            return session.get(ExamSession, session_id)

        return self._run(_work, commit=False)

    def get_for_student(
        self, exam_id: str, student_id: str
    ) -> ExamSession | None:
        """Return the session for ``(exam_id, student_id)`` or ``None``."""

        def _work(session):
            stmt = select(ExamSession).where(
                ExamSession.exam_id == exam_id,
                ExamSession.student_id == student_id,
            )
            return session.execute(stmt).scalar_one_or_none()

        return self._run(_work, commit=False)

    def list_for_exam(self, exam_id: str) -> list[ExamSession]:
        """Return all sessions for ``exam_id`` (bound param)."""

        def _work(session):
            stmt = select(ExamSession).where(ExamSession.exam_id == exam_id)
            return list(session.execute(stmt).scalars().all())

        return self._run(_work, commit=False)

    def set_status(
        self, session_id: str, status: SessionStatus
    ) -> ExamSession | None:
        """Update a session's status and return the updated record (or ``None``)."""

        def _work(session):
            row = session.get(ExamSession, session_id)
            if row is None:
                return None
            row.status = status
            return row

        result = self._run(_work, commit=True)
        if result is not None:
            self.session.refresh(result)
        return result

    def set_integrity_score(
        self, session_id: str, score: float
    ) -> ExamSession | None:
        """Persist a new integrity score for the session (clamped by caller)."""

        def _work(session):
            row = session.get(ExamSession, session_id)
            if row is None:
                return None
            row.integrity_score = score
            return row

        result = self._run(_work, commit=True)
        if result is not None:
            self.session.refresh(result)
        return result
