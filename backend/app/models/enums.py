"""Domain enumerations shared by ORM models and Pydantic schemas.

These mirror the value sets named in the design document's Data Models section
and the requirements (e.g. Role admin/invigilator/student, alert severity
info/warning/danger). Keeping them in one place guarantees the ORM and the
schema mirrors stay in agreement.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """The three RBAC roles recognized by the platform (Requirement 2.3)."""

    ADMIN = "admin"
    INVIGILATOR = "invigilator"
    STUDENT = "student"


class ExamStatus(StrEnum):
    """Exam lifecycle states (design Data Models: Exam.status)."""

    DRAFT = "draft"
    PROVISIONING = "provisioning"
    LIVE = "live"
    COMPLETED = "completed"


class QuestionType(StrEnum):
    """Supported question types (design Data Models: Question.type)."""

    MCQ = "mcq"
    SHORT = "short"
    NUMERICAL = "numerical"


class AuditStatus(StrEnum):
    """Auditor verdict states for a generated paper."""

    PENDING = "pending"
    APPROVED = "approved"
    FLAGGED = "flagged"


class SessionStatus(StrEnum):
    """Exam session lifecycle states (design Data Models: ExamSession.status)."""

    NOT_STARTED = "not_started"
    ACTIVE = "active"
    SUBMITTED = "submitted"
    TERMINATED = "terminated"


class SessionEventKind(StrEnum):
    """Telemetry event kinds (design Data Models: SessionEvent.kind)."""

    TAB_BLUR = "tab_blur"
    TAB_FOCUS = "tab_focus"
    PASTE = "paste"
    COPY = "copy"
    ANSWER_CHANGE = "answer_change"
    QUESTION_VIEW = "question_view"
    HEARTBEAT = "heartbeat"


class SourceAgent(StrEnum):
    """Agents that can source an anomaly (design Data Models: Anomaly.source_agent)."""

    GUARDIAN = "guardian"
    SENTINEL = "sentinel"


class AnomalyCategory(StrEnum):
    """Anomaly categories (design Data Models: Anomaly.category)."""

    FACE_ABSENT = "face_absent"
    MULTIPLE_FACES = "multiple_faces"
    GAZE_AWAY = "gaze_away"
    TAB_SWITCH = "tab_switch"
    PASTE = "paste"
    TIMING = "timing"
    ANSWER_SIMILARITY = "answer_similarity"


class AlertSeverity(StrEnum):
    """Alert severity enumeration (Requirement 9.1, 14.8)."""

    INFO = "info"
    WARNING = "warning"
    DANGER = "danger"
