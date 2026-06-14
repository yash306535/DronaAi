"""Answer aggregate repository.

Parameterized ORM/Core access only (15.7); transient-fault retry-once (15.10).
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.orm import Answer
from app.repositories.base import BaseRepository


class AnswerRepository(BaseRepository):
    """Upsert/read access for :class:`Answer` records."""

    def get(self, answer_id: str) -> Answer | None:
        """Return the answer with ``answer_id`` or ``None``."""

        def _work(session):
            return session.get(Answer, answer_id)

        return self._run(_work, commit=False)

    def get_for_question(
        self, session_id: str, question_id: str
    ) -> Answer | None:
        """Return the answer for ``(session_id, question_id)`` or ``None``."""

        def _work(session):
            stmt = select(Answer).where(
                Answer.session_id == session_id,
                Answer.question_id == question_id,
            )
            return session.execute(stmt).scalar_one_or_none()

        return self._run(_work, commit=False)

    def upsert(
        self,
        *,
        session_id: str,
        question_id: str,
        response: str,
        time_spent_ms: int,
    ) -> Answer:
        """Create or update the answer for ``(session_id, question_id)``.

        A student may submit or update an answer while the session is active;
        this writes a single row per question, updating it in place on resubmit.
        """

        def _work(session):
            stmt = select(Answer).where(
                Answer.session_id == session_id,
                Answer.question_id == question_id,
            )
            answer = session.execute(stmt).scalar_one_or_none()
            if answer is None:
                answer = Answer(
                    session_id=session_id,
                    question_id=question_id,
                    response=response,
                    time_spent_ms=time_spent_ms,
                )
                session.add(answer)
            else:
                answer.response = response
                answer.time_spent_ms = time_spent_ms
            return answer

        result = self._run(_work, commit=True)
        self.session.refresh(result)
        return result

    def list_for_session(self, session_id: str) -> list[Answer]:
        """Return all answers for ``session_id`` (bound param)."""

        def _work(session):
            stmt = select(Answer).where(Answer.session_id == session_id)
            return list(session.execute(stmt).scalars().all())

        return self._run(_work, commit=False)
