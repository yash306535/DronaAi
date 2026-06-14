"""Repository layer: all database access.

Each aggregate has a dedicated repository that performs every operation through
the SQLAlchemy ORM/Core API with bound parameters — no query is built from
string concatenation/interpolation of request-derived values (Requirement
15.7). All repositories inherit a transient-fault retry-once policy from
:class:`BaseRepository`: a connection failure, connection timeout, or deadlock
is retried exactly once after a delay of at most 500 ms, and a persisting fault
is surfaced as :class:`~app.core.errors.UpstreamError` (HTTP 503) with the
request id while leaving persisted data unchanged (Requirement 15.10).
"""

from app.repositories.alert import AlertRepository
from app.repositories.analytics import ExamAnalyticsRepository
from app.repositories.anomaly import AnomalyRepository
from app.repositories.answer import AnswerRepository
from app.repositories.base import (
    BaseRepository,
    is_transient_fault,
)
from app.repositories.exam import ExamRepository
from app.repositories.paper import PaperRepository
from app.repositories.session import ExamSessionRepository
from app.repositories.session_event import SessionEventRepository
from app.repositories.user import UserRepository

__all__ = [
    "BaseRepository",
    "is_transient_fault",
    "UserRepository",
    "ExamRepository",
    "PaperRepository",
    "ExamSessionRepository",
    "SessionEventRepository",
    "AnomalyRepository",
    "AlertRepository",
    "AnswerRepository",
    "ExamAnalyticsRepository",
]
