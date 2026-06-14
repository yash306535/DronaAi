"""Anomaly and alert schemas.

Validation rules (Requirements 14.7, 14.8, design Data Models):
- ``Anomaly.score`` constrained to the inclusive range 0.0-1.0.
- ``Alert.severity`` constrained to the AlertSeverity enum
  (info / warning / danger).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import (
    AlertSeverity,
    AnomalyCategory,
    SourceAgent,
)

ANOMALY_SCORE_MIN = 0.0
ANOMALY_SCORE_MAX = 1.0


class AnomalyCreate(BaseModel):
    """Inbound anomaly. ``score`` must lie within [0.0, 1.0] (Req 14.7)."""

    session_id: str
    source_agent: SourceAgent
    category: AnomalyCategory
    score: float = Field(ge=ANOMALY_SCORE_MIN, le=ANOMALY_SCORE_MAX)
    reasons: list[str] = Field(default_factory=list)
    evidence: dict = Field(default_factory=dict)
    confirmed: bool = False


class AnomalyRead(BaseModel):
    """Persisted anomaly view."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    source_agent: SourceAgent
    category: AnomalyCategory
    score: float = Field(ge=ANOMALY_SCORE_MIN, le=ANOMALY_SCORE_MAX)
    reasons: list[str]
    evidence: dict
    detected_at: datetime
    confirmed: bool


class AlertCreate(BaseModel):
    """Inbound alert. ``severity`` must be a valid enum member (Req 14.8)."""

    anomaly_id: str
    session_id: str
    severity: AlertSeverity
    message: str = Field(min_length=1)


class AlertRead(BaseModel):
    """Persisted alert view."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    anomaly_id: str
    session_id: str
    severity: AlertSeverity
    message: str
    delivered_ws: bool
    delivered_email: bool
    created_at: datetime
