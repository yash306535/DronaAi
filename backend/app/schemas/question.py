"""Question and generated-paper schemas.

Validation rules (Requirement 4.4, design Data Models):
- MCQ questions must have between 2 and ``MAX_MCQ_OPTIONS`` options.
- Exactly one MCQ option must be marked correct (the ``answer_key`` must match
  exactly one option).
- ``answer_key`` is present on the server-side / internal schemas only and is
  excluded from every student-facing schema (``StudentQuestion`` /
  ``StudentPaper``) to satisfy answer-key confidentiality (Req 4.9, 5.3, 14.1).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import AuditStatus, QuestionType

MIN_MCQ_OPTIONS = 2
MAX_MCQ_OPTIONS = 4


class QuestionCreate(BaseModel):
    """A generated question including its server-side answer key.

    MCQ validation enforces Requirement 4.4: 2-4 options with exactly one
    correct option (the answer key must equal exactly one option).
    """

    index: int = Field(ge=0)
    type: QuestionType
    prompt: str = Field(min_length=1)
    options: list[str] | None = None
    answer_key: str = Field(min_length=1)
    topic: str = Field(min_length=1, max_length=200)
    difficulty: float = Field(ge=0.0, le=1.0)
    max_marks: float = Field(gt=0.0)

    @model_validator(mode="after")
    def _validate_mcq(self) -> "QuestionCreate":
        if self.type == QuestionType.MCQ:
            if self.options is None:
                raise ValueError("mcq questions require an options list")
            if not (MIN_MCQ_OPTIONS <= len(self.options) <= MAX_MCQ_OPTIONS):
                raise ValueError(
                    f"mcq questions must have between {MIN_MCQ_OPTIONS} and "
                    f"{MAX_MCQ_OPTIONS} options"
                )
            correct_count = sum(1 for opt in self.options if opt == self.answer_key)
            if correct_count != 1:
                raise ValueError(
                    "exactly one mcq option must match the answer key"
                )
        return self


class StudentQuestion(BaseModel):
    """Question as delivered to a student: no answer key, ever.

    Deliberately omits ``answer_key`` so it can never be serialized to the
    student client (Requirement 4.9 / 5.3 / 14.1).
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    index: int
    type: QuestionType
    prompt: str
    options: list[str] | None = None
    topic: str
    max_marks: float


class StudentPaper(BaseModel):
    """A student's own paper with answer keys stripped."""

    id: str
    exam_id: str
    questions: list[StudentQuestion]


class PaperRead(BaseModel):
    """Internal/admin view of a generated paper (no question payload)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    exam_id: str
    student_id: str
    seed: str
    audit_status: AuditStatus
    created_at: datetime
