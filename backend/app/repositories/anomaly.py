"""Anomaly aggregate repository.

Parameterized ORM/Core access only (15.7); transient-fault retry-once (15.10).
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.orm import Anomaly
from app.repositories.base import BaseRepository


class AnomalyRepository(BaseRepository):
    """Append/read access for :class:`Anomaly` records."""

    def add(self, anomaly: Anomaly) -> Anomaly:
        """Persist a new anomaly and return it."""

        def _work(session):
            session.add(anomaly)
            return anomaly

        result = self._run(_work, commit=True)
        self.session.refresh(result)
        return result

    def get(self, anomaly_id: str) -> Anomaly | None:
        """Return the anomaly with ``anomaly_id`` or ``None``."""

        def _work(session):
            return session.get(Anomaly, anomaly_id)

        return self._run(_work, commit=False)

    def list_for_session(self, session_id: str) -> list[Anomaly]:
        """Return anomalies for ``session_id`` ordered by detection time."""

        def _work(session):
            stmt = (
                select(Anomaly)
                .where(Anomaly.session_id == session_id)
                .order_by(Anomaly.detected_at)
            )
            return list(session.execute(stmt).scalars().all())

        return self._run(_work, commit=False)
