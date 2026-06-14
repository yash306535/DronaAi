"""Stage-2 proctoring escalation request/response schemas.

Mirrors the design's *Representative request/response (escalation)* for
``POST /proctoring/{session_id}/escalate``. The browser captures a single
downscaled frame on a debounced Stage-1 trigger and POSTs it together with the
local signal that caused the escalation; the Guardian returns a structured
verdict-derived response.

Validation here enforces the local-signal shape; the heavier payload guards
(frame size ≤ 5 MB and MIME ``image/jpeg``/``image/png`` only, Requirement 7.3)
are applied in the route before any Vision call so a rejected payload never
reaches the provider.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Stage-1 local signal kinds that may trigger an escalation (Requirement 6/7).
LOCAL_SIGNAL_KINDS = ("face_absent", "multiple_faces", "gaze_away")


class LocalSignal(BaseModel):
    """The debounced Stage-1 local signal that triggered the escalation.

    ``kind`` is the suspected condition; ``duration_ms`` is how long it
    persisted locally; ``confidence_local`` is the browser screener's confidence
    (``[0.0, 1.0]``). Only these three fields are trusted as context for the
    Stage-2 verdict — the authoritative determination comes from Vision.
    """

    kind: str = Field(min_length=1)
    duration_ms: float = Field(default=0.0, ge=0.0)
    confidence_local: float = Field(default=0.0, ge=0.0, le=1.0)


class EscalationRequest(BaseModel):
    """Inbound escalation payload: the local signal plus a single frame.

    ``frame`` is the captured image. It may be either a bare base64 string or a
    ``data:image/...;base64,...`` data URL; the route extracts the MIME type and
    raw payload, validating both before any Vision call (7.3).
    """

    local_signal: LocalSignal
    frame: str = Field(min_length=1)


class EscalationResponse(BaseModel):
    """Outbound escalation result derived from the Vision verdict.

    Shape mirrors the design: ``anomaly_id`` (``None`` when no anomaly was
    persisted, e.g. a benign verdict), ``confirmed`` (true only on an
    authoritative anomalous verdict ≥ confirm threshold, 7.4), ``category``,
    ``score``, ``reasons``, and the ``action`` taken (``alert_broadcast`` on
    confirmation, otherwise ``suppressed``).
    """

    anomaly_id: str | None = None
    confirmed: bool = False
    category: str
    score: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    action: str


__all__ = [
    "LOCAL_SIGNAL_KINDS",
    "LocalSignal",
    "EscalationRequest",
    "EscalationResponse",
]
