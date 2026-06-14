"""Exam analytics response schema (Requirement 10).

Mirrors the persisted :class:`~app.models.orm.ExamAnalytics` aggregate produced
by the Analyst agent. The ``summary``, ``difficulty_heatmap``, and
``per_student`` columns are free-form JSON dicts (their internal shape is owned
by the Analyst), so they are surfaced here as ``dict`` and rendered by the
analytics dashboard.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ExamAnalyticsRead(BaseModel):
    """Full analytics record returned to an admin (``GET /analytics/exams/{id}``)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    exam_id: str
    summary: dict = Field(default_factory=dict)
    difficulty_heatmap: dict = Field(default_factory=dict)
    per_student: dict = Field(default_factory=dict)
    generated_at: datetime


__all__ = ["ExamAnalyticsRead"]
