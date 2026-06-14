"""SQLAlchemy ORM models.

Importing this package registers every model on ``Base.metadata`` so that
``create_all`` provisions the full schema.
"""

from app.models.enums import (
    AlertSeverity,
    AnomalyCategory,
    AuditStatus,
    ExamStatus,
    QuestionType,
    Role,
    SessionEventKind,
    SessionStatus,
    SourceAgent,
)
from app.models.orm import (
    Alert,
    Answer,
    Anomaly,
    Exam,
    ExamAnalytics,
    ExamSession,
    GeneratedPaper,
    Question,
    SessionEvent,
    User,
)

__all__ = [
    # Enums
    "AlertSeverity",
    "AnomalyCategory",
    "AuditStatus",
    "ExamStatus",
    "QuestionType",
    "Role",
    "SessionEventKind",
    "SessionStatus",
    "SourceAgent",
    # ORM models
    "Alert",
    "Answer",
    "Anomaly",
    "Exam",
    "ExamAnalytics",
    "ExamSession",
    "GeneratedPaper",
    "Question",
    "SessionEvent",
    "User",
]
