"""Pydantic request/response schemas mirroring the ORM models."""

from app.schemas.anomaly import (
    AlertCreate,
    AlertRead,
    AnomalyCreate,
    AnomalyRead,
)
from app.schemas.exam import (
    ExamBlueprint,
    ExamCreate,
    ExamRead,
    TopicSpec,
)
from app.schemas.question import (
    PaperRead,
    QuestionCreate,
    StudentPaper,
    StudentQuestion,
)
from app.schemas.session import (
    AnswerRead,
    AnswerSubmit,
    SessionEventBatch,
    SessionEventIn,
    SessionEventRead,
    SessionRead,
)
from app.schemas.user import (
    LoginRequest,
    RefreshRequest,
    TokenPair,
    UserCreate,
    UserRead,
)
from app.schemas.ws import (
    WSMessage,
    WSMessageType,
)

__all__ = [
    # user / auth
    "LoginRequest",
    "RefreshRequest",
    "TokenPair",
    "UserCreate",
    "UserRead",
    # exam
    "ExamBlueprint",
    "ExamCreate",
    "ExamRead",
    "TopicSpec",
    # question / paper
    "PaperRead",
    "QuestionCreate",
    "StudentPaper",
    "StudentQuestion",
    # session / answer / events
    "AnswerRead",
    "AnswerSubmit",
    "SessionEventBatch",
    "SessionEventIn",
    "SessionEventRead",
    "SessionRead",
    # anomaly / alert
    "AlertCreate",
    "AlertRead",
    "AnomalyCreate",
    "AnomalyRead",
    # websocket envelope
    "WSMessage",
    "WSMessageType",
]
