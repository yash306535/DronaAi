"""User aggregate repository.

All access goes through the SQLAlchemy ORM/Core with bound parameters
(Requirement 15.7) and inherits the transient-fault retry-once policy from
:class:`~app.repositories.base.BaseRepository` (Requirement 15.10).
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.orm import User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository):
    """CRUD access for :class:`User` records."""

    def add(self, user: User) -> User:
        """Persist a new user and return it."""

        def _work(session):
            session.add(user)
            return user

        result = self._run(_work, commit=True)
        self.session.refresh(result)
        return result

    def get(self, user_id: str) -> User | None:
        """Return the user with ``user_id`` or ``None``."""

        def _work(session):
            return session.get(User, user_id)

        return self._run(_work, commit=False)

    def get_by_email(self, email: str) -> User | None:
        """Return the user whose email equals ``email`` (bound param) or ``None``."""

        def _work(session):
            stmt = select(User).where(User.email == email)
            return session.execute(stmt).scalar_one_or_none()

        return self._run(_work, commit=False)

    def list_by_role(self, role: str) -> list[User]:
        """Return all users assigned ``role`` (bound param)."""

        def _work(session):
            stmt = select(User).where(User.role == role)
            return list(session.execute(stmt).scalars().all())

        return self._run(_work, commit=False)

    def delete(self, user_id: str) -> bool:
        """Delete the user with ``user_id``; return ``True`` if one was removed."""

        def _work(session):
            user = session.get(User, user_id)
            if user is None:
                return False
            session.delete(user)
            return True

        return self._run(_work, commit=True)
