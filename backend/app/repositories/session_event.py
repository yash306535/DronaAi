"""SessionEvent aggregate repository.

Parameterized ORM/Core access only (15.7); transient-fault retry-once (15.10).
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.orm import SessionEvent
from app.repositories.base import BaseRepository


class SessionEventRepository(BaseRepository):
    """Append/read access for :class:`SessionEvent` telemetry rows."""

    def add(self, event: SessionEvent) -> SessionEvent:
        """Persist a single session event and return it."""

        def _work(session):
            session.add(event)
            return event

        result = self._run(_work, commit=True)
        self.session.refresh(result)
        return result

    def add_batch(self, events: list[SessionEvent]) -> list[SessionEvent]:
        """Persist a batch of events atomically (all-or-nothing).

        Either every event in the batch is committed or, on failure, none are
        (the unit of work is rolled back), supporting the "persist none" reject
        semantics required when a batch is invalid.
        """

        def _work(session):
            session.add_all(events)
            return events

        result = self._run(_work, commit=True)
        for event in result:
            self.session.refresh(event)
        return result

    def list_for_session(self, session_id: str) -> list[SessionEvent]:
        """Return events for ``session_id`` ordered by server timestamp."""

        def _work(session):
            stmt = (
                select(SessionEvent)
                .where(SessionEvent.session_id == session_id)
                .order_by(SessionEvent.server_ts)
            )
            return list(session.execute(stmt).scalars().all())

        return self._run(_work, commit=False)
