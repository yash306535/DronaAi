"""GeneratedPaper / Question aggregate repository.

A generated paper and its questions form a single aggregate: questions are
persisted with their paper so the answer key never leaks (Requirement 4.9 is
enforced at the serialization layer; this layer simply stores it server-side).

Parameterized ORM/Core access only (15.7); transient-fault retry-once (15.10).
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.orm import GeneratedPaper, Question
from app.repositories.base import BaseRepository


class PaperRepository(BaseRepository):
    """CRUD access for :class:`GeneratedPaper` and its :class:`Question` rows."""

    def add(self, paper: GeneratedPaper) -> GeneratedPaper:
        """Persist a paper (with any attached questions) and return it.

        Questions appended to ``paper.questions`` are cascaded on commit, so a
        paper and its full question set are written as one unit of work.
        """

        def _work(session):
            session.add(paper)
            return paper

        result = self._run(_work, commit=True)
        self.session.refresh(result)
        return result

    def add_with_questions(
        self, paper: GeneratedPaper, questions: list[Question]
    ) -> GeneratedPaper:
        """Persist ``paper`` together with ``questions`` atomically."""

        def _work(session):
            for question in questions:
                question.paper_id = paper.id
                paper.questions.append(question)
            session.add(paper)
            return paper

        result = self._run(_work, commit=True)
        self.session.refresh(result)
        return result

    def get(self, paper_id: str) -> GeneratedPaper | None:
        """Return the paper with ``paper_id`` or ``None``."""

        def _work(session):
            return session.get(GeneratedPaper, paper_id)

        return self._run(_work, commit=False)

    def list_for_exam(self, exam_id: str) -> list[GeneratedPaper]:
        """Return all papers generated for ``exam_id`` (bound param)."""

        def _work(session):
            stmt = select(GeneratedPaper).where(GeneratedPaper.exam_id == exam_id)
            return list(session.execute(stmt).scalars().all())

        return self._run(_work, commit=False)

    def get_for_student(
        self, exam_id: str, student_id: str
    ) -> GeneratedPaper | None:
        """Return the paper for ``(exam_id, student_id)`` or ``None``."""

        def _work(session):
            stmt = select(GeneratedPaper).where(
                GeneratedPaper.exam_id == exam_id,
                GeneratedPaper.student_id == student_id,
            )
            return session.execute(stmt).scalar_one_or_none()

        return self._run(_work, commit=False)

    def list_questions(self, paper_id: str) -> list[Question]:
        """Return the ordered questions for ``paper_id`` (bound param)."""

        def _work(session):
            stmt = (
                select(Question)
                .where(Question.paper_id == paper_id)
                .order_by(Question.index)
            )
            return list(session.execute(stmt).scalars().all())

        return self._run(_work, commit=False)
