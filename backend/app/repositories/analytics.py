"""ExamAnalytics aggregate repository.

Parameterized ORM/Core access only (15.7); transient-fault retry-once (15.10).
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.orm import ExamAnalytics
from app.repositories.base import BaseRepository


class ExamAnalyticsRepository(BaseRepository):
    """Upsert/read access for the per-exam :class:`ExamAnalytics` record."""

    def add(self, analytics: ExamAnalytics) -> ExamAnalytics:
        """Persist a new analytics record and return it."""

        def _work(session):
            session.add(analytics)
            return analytics

        result = self._run(_work, commit=True)
        self.session.refresh(result)
        return result

    def get(self, analytics_id: str) -> ExamAnalytics | None:
        """Return the analytics row with ``analytics_id`` or ``None``."""

        def _work(session):
            return session.get(ExamAnalytics, analytics_id)

        return self._run(_work, commit=False)

    def get_for_exam(self, exam_id: str) -> ExamAnalytics | None:
        """Return the analytics row for ``exam_id`` (bound param) or ``None``."""

        def _work(session):
            stmt = select(ExamAnalytics).where(ExamAnalytics.exam_id == exam_id)
            return session.execute(stmt).scalar_one_or_none()

        return self._run(_work, commit=False)

    def upsert(
        self,
        *,
        exam_id: str,
        summary: dict,
        difficulty_heatmap: dict,
        per_student: dict,
    ) -> ExamAnalytics:
        """Create or update the analytics record for ``exam_id``."""

        def _work(session):
            stmt = select(ExamAnalytics).where(ExamAnalytics.exam_id == exam_id)
            row = session.execute(stmt).scalar_one_or_none()
            if row is None:
                row = ExamAnalytics(
                    exam_id=exam_id,
                    summary=summary,
                    difficulty_heatmap=difficulty_heatmap,
                    per_student=per_student,
                )
                session.add(row)
            else:
                row.summary = summary
                row.difficulty_heatmap = difficulty_heatmap
                row.per_student = per_student
            return row

        result = self._run(_work, commit=True)
        self.session.refresh(result)
        return result
