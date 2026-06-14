"""Exam and blueprint schemas.

Validation rules (Requirements 3.1, 3.2, 3.3, 3.4):
- ``title`` 1-200 characters.
- blueprint must specify between 1 and 100 topics.
- blueprint total question count between 1 and 1000.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import ExamStatus, QuestionType

TITLE_MIN_LENGTH = 1
TITLE_MAX_LENGTH = 200
MIN_TOPICS = 1
MAX_TOPICS = 100
MIN_TOTAL_QUESTIONS = 1
MAX_TOTAL_QUESTIONS = 1000


class TopicSpec(BaseModel):
    """A single topic entry within a blueprint."""

    name: str = Field(min_length=1, max_length=200)
    count: int = Field(ge=0)


class ExamBlueprint(BaseModel):
    """Exam specification: topics, counts, difficulty mix, and question types.

    Enforces Requirement 3.2 (1-100 topics) and 3.3 (total question count
    1-1000). ``total_questions`` must equal the sum of per-topic counts when
    topic counts are provided, so the blueprint is internally consistent.
    """

    topics: list[TopicSpec] = Field(min_length=MIN_TOPICS, max_length=MAX_TOPICS)
    total_questions: int = Field(ge=MIN_TOTAL_QUESTIONS, le=MAX_TOTAL_QUESTIONS)
    difficulty_mix: dict[str, float] = Field(default_factory=dict)
    question_types: list[QuestionType] = Field(
        default_factory=lambda: [QuestionType.MCQ]
    )

    @model_validator(mode="after")
    def _check_topic_count_sum(self) -> "ExamBlueprint":
        """If per-topic counts are specified, they must sum to total_questions."""
        topic_sum = sum(topic.count for topic in self.topics)
        if topic_sum > 0 and topic_sum != self.total_questions:
            raise ValueError(
                "blueprint total_questions must equal the sum of per-topic counts"
            )
        return self


class ExamCreate(BaseModel):
    """Request body for creating an exam (Requirement 3.1)."""

    title: str = Field(min_length=TITLE_MIN_LENGTH, max_length=TITLE_MAX_LENGTH)
    subject: str = Field(min_length=1, max_length=200)
    blueprint: ExamBlueprint
    duration_minutes: int = Field(gt=0)
    starts_at: datetime


class ExamRead(BaseModel):
    """Exam detail returned to admin/invigilator."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    subject: str
    blueprint: dict
    duration_minutes: int
    starts_at: datetime
    status: ExamStatus
    created_by: str
