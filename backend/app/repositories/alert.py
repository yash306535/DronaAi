"""Alert aggregate repository.

Parameterized ORM/Core access only (15.7); transient-fault retry-once (15.10).
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.orm import Alert
from app.repositories.base import BaseRepository


class AlertRepository(BaseRepository):
    """Append/read/update access for :class:`Alert` records."""

    def add(self, alert: Alert) -> Alert:
        """Persist a new alert and return it."""

        def _work(session):
            session.add(alert)
            return alert

        result = self._run(_work, commit=True)
        self.session.refresh(result)
        return result

    def get(self, alert_id: str) -> Alert | None:
        """Return the alert with ``alert_id`` or ``None``."""

        def _work(session):
            return session.get(Alert, alert_id)

        return self._run(_work, commit=False)

    def list_for_session(self, session_id: str) -> list[Alert]:
        """Return alerts for ``session_id`` ordered by creation time."""

        def _work(session):
            stmt = (
                select(Alert)
                .where(Alert.session_id == session_id)
                .order_by(Alert.created_at)
            )
            return list(session.execute(stmt).scalars().all())

        return self._run(_work, commit=False)

    def mark_delivered(
        self,
        alert_id: str,
        *,
        ws: bool | None = None,
        email: bool | None = None,
    ) -> Alert | None:
        """Update an alert's delivery flags and return it (or ``None``)."""

        def _work(session):
            alert = session.get(Alert, alert_id)
            if alert is None:
                return None
            if ws is not None:
                alert.delivered_ws = ws
            if email is not None:
                alert.delivered_email = email
            return alert

        result = self._run(_work, commit=True)
        if result is not None:
            self.session.refresh(result)
        return result
