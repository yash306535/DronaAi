"""Exam aggregate repository.

Parameterized ORM/Core access only (Requirement 15.7) with transient-fault
retry-once (Requirement 15.10) inherited from :class:`BaseRepository`.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.enums import ExamStatus
from app.models.orm import Exam
from app.repositories.base import BaseRepository


class ExamRepository(BaseRepository):
    """CRUD access for :class:`Exam` records."""

    def add(self, exam: Exam) -> Exam:
        """Persist a new exam and return it."""

        def _work(session):
            session.add(exam)
            return exam

        result = self._run(_work, commit=True)
        self.session.refresh(result)
        return result

    def get(self, exam_id: str) -> Exam | None:
        """Return the exam with ``exam_id`` or ``None``."""

        def _work(session):
            return session.get(Exam, exam_id)

        return self._run(_work, commit=False)

    def list_all(self) -> list[Exam]:
        """Return all exams ordered by creation-derived id stability."""

        def _work(session):
            return list(session.execute(select(Exam)).scalars().all())

        return self._run(_work, commit=False)

    def list_by_creator(self, created_by: str) -> list[Exam]:
        """Return exams created by ``created_by`` (bound param)."""

        def _work(session):
            stmt = select(Exam).where(Exam.created_by == created_by)
            return list(session.execute(stmt).scalars().all())

        return self._run(_work, commit=False)

    def set_status(self, exam_id: str, status: ExamStatus) -> Exam | None:
        """Update an exam's status and return the updated record (or ``None``)."""

        def _work(session):
            exam = session.get(Exam, exam_id)
            if exam is None:
                return None
            exam.status = status
            return exam

        result = self._run(_work, commit=True)
        if result is not None:
            self.session.refresh(result)
        return result
