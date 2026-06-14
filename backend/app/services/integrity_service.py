"""Session integrity-score management service (Requirement 14.5-14.8).

Owns the deterministic rule by which a session's *integrity score* evolves as
confirmed anomalies accumulate, plus the small set of clamping/validation
invariants the design requires around anomaly scores and alert severities.

Requirements covered:

- **14.5** — Recording a *confirmed* anomaly updates the session's integrity
  score to a value **less than or equal to** its prior value. The penalty rule
  below is non-negative, so the score is monotonically non-increasing as
  confirmed anomalies accumulate.
- **14.6** — The integrity score is constrained to the inclusive range
  ``[0, 100]``; any computed value below 0 clamps to 0 and any value above 100
  clamps to 100.
- **14.7** — An anomaly score is constrained to the inclusive range
  ``[0.0, 1.0]``; values outside clamp to the nearest bound. The clamped score
  is what drives the integrity penalty.
- **14.8** — An alert severity outside the defined :class:`AlertSeverity`
  enumeration is rejected with a :class:`ValidationError`; the caller's prior
  severity is left unchanged (this method never persists an invalid value).

**Deterministic penalty rule.** A confirmed anomaly with clamped score ``s`` in
``[0.0, 1.0]`` reduces the integrity score by ``s * MAX_ANOMALY_PENALTY`` points
(``MAX_ANOMALY_PENALTY = 25.0``). So a maximal-severity anomaly (``s == 1.0``)
costs 25 points and a benign-but-confirmed one (``s == 0.0``) costs nothing. The
result is clamped to ``[0, 100]``. The rule is pure and order-independent in
sign (every step is a non-increasing transform), which is exactly the
monotonicity property the spec asks for.

Change emission: whenever the persisted integrity score (or status) actually
changes, a :data:`EventType.SESSION_UPDATE` event is published carrying the new
integrity score and status. ``SESSION_UPDATE`` is the canonical registry value
added to :mod:`app.core.events`; its string value (``"session.update"``) matches
the dashboard's ``session.update`` WS message type so a WS-facing subscriber can
forward it to the ``dashboard`` room without translation (Requirement 12.6).
"""

from __future__ import annotations

from app.core.errors import NotFoundError, ValidationError
from app.core.events import Event, EventBus, EventType
from app.core.logging import get_logger
from app.models.enums import AlertSeverity, SessionStatus
from app.repositories.session import ExamSessionRepository
from app.schemas.session import (
    INTEGRITY_SCORE_MAX,
    INTEGRITY_SCORE_MIN,
    SessionRead,
)

logger = get_logger("app.services.integrity")

# Inclusive bounds for a single anomaly score (Requirement 14.7).
ANOMALY_SCORE_MIN = 0.0
ANOMALY_SCORE_MAX = 1.0

# Maximum points a single confirmed anomaly can subtract from the integrity
# score (when its clamped score is 1.0). The penalty scales linearly with the
# clamped anomaly score, so it is always within [0, MAX_ANOMALY_PENALTY].
MAX_ANOMALY_PENALTY = 25.0

# The source name on events this service publishes.
EVENT_SOURCE = "integrity_service"

# Stable, machine-readable domain error codes.
SESSION_NOT_FOUND_CODE = "session_not_found"
INVALID_SEVERITY_CODE = "invalid_alert_severity"


def clamp_anomaly_score(score: float) -> float:
    """Clamp an anomaly score to the inclusive range ``[0.0, 1.0]`` (14.7)."""
    if score < ANOMALY_SCORE_MIN:
        return ANOMALY_SCORE_MIN
    if score > ANOMALY_SCORE_MAX:
        return ANOMALY_SCORE_MAX
    return score


def clamp_integrity_score(score: float) -> float:
    """Clamp an integrity score to the inclusive range ``[0, 100]`` (14.6)."""
    if score < INTEGRITY_SCORE_MIN:
        return INTEGRITY_SCORE_MIN
    if score > INTEGRITY_SCORE_MAX:
        return INTEGRITY_SCORE_MAX
    return score


def anomaly_penalty(score: float) -> float:
    """Return the (non-negative) integrity penalty for an anomaly ``score``.

    The score is first clamped to ``[0.0, 1.0]`` (14.7) then scaled linearly by
    :data:`MAX_ANOMALY_PENALTY`. The result is always ``>= 0`` so applying it can
    only hold or lower the integrity score (14.5).
    """
    return clamp_anomaly_score(score) * MAX_ANOMALY_PENALTY


def validate_alert_severity(severity: object) -> AlertSeverity:
    """Return ``severity`` as a valid :class:`AlertSeverity` or reject it (14.8).

    Accepts an :class:`AlertSeverity` member or a string equal to one of its
    values. Any other value raises a :class:`ValidationError`; callers must
    treat this as "leave the prior severity unchanged" — this function never
    mutates state.
    """
    if isinstance(severity, AlertSeverity):
        return severity
    try:
        return AlertSeverity(severity)  # type: ignore[arg-type]
    except (ValueError, KeyError):
        raise ValidationError(
            "Invalid alert severity value.",
            code=INVALID_SEVERITY_CODE,
            details={"allowed": [s.value for s in AlertSeverity]},
        )


class IntegrityService:
    """Use-case logic for evolving a session's integrity score."""

    def __init__(
        self,
        *,
        sessions: ExamSessionRepository,
        event_bus: EventBus | None = None,
    ) -> None:
        self._sessions = sessions
        self._bus = event_bus

    async def record_confirmed_anomaly(
        self, session_id: str, anomaly_score: float
    ) -> SessionRead:
        """Apply a confirmed anomaly's penalty to the session's integrity score.

        Computes ``new = clamp(prior - penalty(anomaly_score))`` where the
        penalty is non-negative (14.5), clamps the result to ``[0, 100]`` (14.6),
        persists it via :meth:`ExamSessionRepository.set_integrity_score`, and —
        when the value actually changed — emits a ``session.update`` event.
        """
        session_row = self._sessions.get(session_id)
        if session_row is None:
            raise NotFoundError(
                "Session not found.", code=SESSION_NOT_FOUND_CODE
            )

        prior = session_row.integrity_score
        new_score = clamp_integrity_score(prior - anomaly_penalty(anomaly_score))

        if new_score == prior:
            # No change: nothing to persist or emit (idempotent).
            return SessionRead.model_validate(session_row)

        updated = self._sessions.set_integrity_score(session_id, new_score)
        if updated is None:  # pragma: no cover - row existed moments ago
            raise NotFoundError(
                "Session not found.", code=SESSION_NOT_FOUND_CODE
            )

        await self._emit_session_update(updated.id, new_score, updated.status)
        logger.info(
            "integrity.score_updated",
            extra={
                "sessionId": updated.id,
                "priorScore": prior,
                "newScore": new_score,
            },
        )
        return SessionRead.model_validate(updated)

    async def set_status(
        self, session_id: str, status: SessionStatus
    ) -> SessionRead:
        """Update a session's status and emit ``session.update`` on change.

        Provided so a status transition surfaced on the dashboard tile follows
        the same change-emission contract as an integrity-score change
        (Requirement 12.6). Persists via the repository's ``set_status``.
        """
        session_row = self._sessions.get(session_id)
        if session_row is None:
            raise NotFoundError(
                "Session not found.", code=SESSION_NOT_FOUND_CODE
            )

        prior_status = session_row.status
        if status == prior_status:
            return SessionRead.model_validate(session_row)

        updated = self._sessions.set_status(session_id, status)
        if updated is None:  # pragma: no cover - row existed moments ago
            raise NotFoundError(
                "Session not found.", code=SESSION_NOT_FOUND_CODE
            )

        await self._emit_session_update(
            updated.id, updated.integrity_score, updated.status
        )
        return SessionRead.model_validate(updated)

    async def _emit_session_update(
        self, session_id: str, integrity_score: float, status: SessionStatus
    ) -> None:
        """Publish a ``session.update`` event if an event bus is wired.

        The payload mirrors the dashboard ``session.update`` WS message: the
        affected session's new integrity score and status (Requirement 12.6).
        The bus is optional so the service is usable in tests without the full
        app lifespan; publishing is best-effort (the bus captures handler
        errors).
        """
        if self._bus is None:
            return
        await self._bus.publish(
            Event(
                type=EventType.SESSION_UPDATE,
                payload={
                    "sessionId": session_id,
                    "integrityScore": integrity_score,
                    "status": str(status),
                },
                source=EVENT_SOURCE,
                session_id=session_id,
            )
        )


__all__ = [
    "IntegrityService",
    "anomaly_penalty",
    "clamp_anomaly_score",
    "clamp_integrity_score",
    "validate_alert_severity",
    "MAX_ANOMALY_PENALTY",
    "EVENT_SOURCE",
    "SESSION_NOT_FOUND_CODE",
    "INVALID_SEVERITY_CODE",
]
