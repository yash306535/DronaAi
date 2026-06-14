"""Herald agent: real-time alert broadcasting (Requirement 9).

The Herald is the action arm of the crew. Whenever an ``anomaly.detected`` event
lands on the bus it:

1. **Persists an alert** with exactly one severity from ``{info, warning, danger}``
   as indicated by the event, within 2s (Requirement 9.1). Persistence uses a
   fresh :class:`AlertRepository` bound to a new DB session so concurrent
   anomalies never share a session.
2. **Broadcasts an** ``alert.broadcast`` **WebSocket message** to the
   ``dashboard`` room *and* the ``invigilator:{exam_id}`` room for the anomaly's
   exam session — but **only when the anomaly's** ``confirmed`` **flag is true**
   (Requirements 9.2, 9.3). A Guardian-sourced anomaly with ``confirmed == false``
   is persisted but never broadcast. The broadcast payload always includes the
   anomaly's reasons (Requirement 9.7).
3. **Optionally emails** the alert when an SMTP configuration is present, within
   30s (Requirement 9.4). Email is failure-tolerant: if sending fails the alert
   is retained, the WebSocket broadcast still completes, and the email failure is
   recorded on the alert (Requirement 9.5). When no SMTP config is present the
   WebSocket channel is the primary delivery (Requirement 9.6).

The ``anomaly.detected`` payload mirrors what the Guardian emits
(``app/agents/guardian.py`` :meth:`_emit_anomaly_detected`): ``anomalyId``,
``sessionId``, ``sourceAgent``, ``category``, ``score``, ``reasons``,
``confirmed``, ``severity``. The exam id needed to resolve the invigilator room
is looked up via :class:`ExamSessionRepository`.
"""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import EmailMessage

from app.core.config import Settings, get_settings
from app.core.events import Event
from app.core.logging import get_logger
from app.core.ws import (
    DASHBOARD_ROOM,
    INVIGILATOR_ROOM_PREFIX,
    WSMessage,
    WSMessageType,
    WebSocketManager,
    get_ws_manager,
)
from app.models.enums import AlertSeverity
from app.models.orm import Alert
from app.repositories.alert import AlertRepository
from app.repositories.session import ExamSessionRepository

logger = get_logger("app.agents.herald")

HERALD_SOURCE = "Herald"

# An email sender takes a built alert plus its reasons and delivers it. It
# returns ``True`` when an email was actually sent, ``False`` when delivery was
# skipped because no SMTP configuration is present (a no-op), and raises on a
# genuine send failure so the Herald can record the failure (Requirement 9.5).
EmailSender = Callable[[Alert, list[str]], bool]


def _default_severity(value: object) -> AlertSeverity:
    """Coerce an event severity value to exactly one :class:`AlertSeverity`.

    The event indicates the severity (Requirement 9.1). An absent or
    unrecognized value falls back to ``warning`` so an alert is still persisted
    with a single valid severity rather than dropped.
    """
    if isinstance(value, AlertSeverity):
        return value
    try:
        return AlertSeverity(str(value))
    except (ValueError, TypeError):
        logger.warning("herald.severity.coerced", extra={"received": repr(value)})
        return AlertSeverity.WARNING


def _build_message(category: str, reasons: list[str]) -> str:
    """Derive a concise human-facing alert message from the anomaly."""
    label = (category or "anomaly").replace("_", " ")
    if reasons:
        return f"{label}: {reasons[0]}"
    return f"{label} detected"


class SmtpEmailSender:
    """Default SMTP-backed :class:`EmailSender`.

    A no-op (returns ``False``) when ``SMTP_HOST`` is unset so that, with no SMTP
    configuration, the WebSocket channel is the sole/primary delivery path
    (Requirement 9.6). When SMTP is configured it sends a plain-text notification
    and returns ``True``; any transport failure propagates so the Herald records
    an email-delivery failure (Requirement 9.5).
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings

    def _resolve_settings(self) -> Settings:
        return self._settings if self._settings is not None else get_settings()

    def __call__(self, alert: Alert, reasons: list[str]) -> bool:
        settings = self._resolve_settings()
        host = settings.SMTP_HOST
        if not host:
            # No SMTP configuration present: WebSocket is primary (9.6).
            return False

        message = EmailMessage()
        message["Subject"] = f"[DRONA AI] {alert.severity} alert"
        message["From"] = settings.SMTP_USERNAME or "drona-ai@localhost"
        message["To"] = settings.SMTP_USERNAME or "alerts@localhost"
        body_lines = [alert.message, ""]
        if reasons:
            body_lines.append("Reasons:")
            body_lines.extend(f"- {reason}" for reason in reasons)
        message.set_content("\n".join(body_lines))

        with smtplib.SMTP(host, settings.SMTP_PORT, timeout=30) as smtp:
            username = settings.SMTP_USERNAME
            password = settings.SMTP_PASSWORD
            if username and password:
                smtp.starttls()
                smtp.login(username, password.get_secret_value())
            smtp.send_message(message)
        return True


@dataclass(slots=True)
class HeraldAgent:
    """Persist alerts and broadcast confirmed anomalies to humans (Requirement 9).

    ``alert_repo_factory`` / ``session_repo_factory`` return fresh repositories
    bound to new DB sessions per event so concurrent anomalies never share a
    session. ``ws_manager`` is the room-scoped WebSocket fan-out. ``email_sender``
    is an injectable callable (tests inject a stub) defaulting to an SMTP-based
    sender that is a no-op when ``SMTP_HOST`` is unset.
    """

    alert_repo_factory: Callable[[], AlertRepository]
    session_repo_factory: Callable[[], ExamSessionRepository]
    ws_manager: WebSocketManager = field(default_factory=get_ws_manager)
    email_sender: EmailSender = field(default_factory=SmtpEmailSender)

    # -- event handler -------------------------------------------------------

    async def on_anomaly_detected(self, event: Event) -> None:
        """Handle an ``anomaly.detected`` event (Requirements 9.1–9.7).

        Persists an alert, broadcasts over WebSocket iff the anomaly is
        confirmed, then optionally emails. A payload missing the anomaly id or
        session id is ignored with a warning (there is nothing to alert on).
        """
        payload = event.payload or {}
        anomaly_id = payload.get("anomalyId")
        session_id = payload.get("sessionId") or event.session_id
        if not anomaly_id or not session_id:
            logger.warning(
                "herald.anomaly.skipped_incomplete_event",
                extra={"eventId": event.id},
            )
            return

        severity = _default_severity(payload.get("severity"))
        reasons = list(payload.get("reasons") or [])
        category = str(payload.get("category") or "anomaly")
        confirmed = bool(payload.get("confirmed"))
        message = _build_message(category, reasons)

        # 1) Persist the alert with exactly one severity (Requirement 9.1).
        alert = self._persist_alert(
            anomaly_id=anomaly_id,
            session_id=session_id,
            severity=severity,
            message=message,
        )

        # 2) Broadcast only for a confirmed anomaly (Requirements 9.2, 9.3).
        if not confirmed:
            logger.info(
                "herald.alert.persisted_unconfirmed",
                extra={"alertId": alert.id, "sessionId": session_id},
            )
            return

        await self._broadcast(
            alert=alert,
            session_id=session_id,
            severity=severity,
            message=message,
            anomaly_id=anomaly_id,
            reasons=reasons,
        )

        # 3) Optional email; failure-tolerant fallback (Requirements 9.4–9.6).
        self._send_email(alert, reasons)

    # -- persistence / delivery ---------------------------------------------

    def _persist_alert(
        self,
        *,
        anomaly_id: str,
        session_id: str,
        severity: AlertSeverity,
        message: str,
    ) -> Alert:
        """Persist a new :class:`Alert` and return it (Requirement 9.1)."""
        repo = self.alert_repo_factory()
        alert = Alert(
            anomaly_id=anomaly_id,
            session_id=session_id,
            severity=severity,
            message=message,
        )
        return repo.add(alert)

    async def _broadcast(
        self,
        *,
        alert: Alert,
        session_id: str,
        severity: AlertSeverity,
        message: str,
        anomaly_id: str,
        reasons: list[str],
    ) -> None:
        """Broadcast ``alert.broadcast`` to the dashboard + invigilator rooms.

        Targets the ``dashboard`` room and the ``invigilator:{exam_id}`` room for
        the anomaly's exam session (Requirement 9.2). The payload always includes
        the anomaly reasons (Requirement 9.7). After delivery the alert's
        ``delivered_ws`` flag is set.
        """
        ws_message = WSMessage(
            type=WSMessageType.ALERT_BROADCAST.value,
            source=HERALD_SOURCE,
            session_id=session_id,
            payload={
                "severity": severity.value,
                "message": message,
                "anomalyId": anomaly_id,
                "reasons": reasons,
            },
        )

        await self.ws_manager.broadcast(DASHBOARD_ROOM, ws_message)

        exam_id = self._resolve_exam_id(session_id)
        if exam_id:
            await self.ws_manager.broadcast(
                f"{INVIGILATOR_ROOM_PREFIX}{exam_id}", ws_message
            )
        else:
            logger.warning(
                "herald.broadcast.no_exam_for_session",
                extra={"sessionId": session_id},
            )

        self._mark_delivered(alert.id, ws=True)

    def _send_email(self, alert: Alert, reasons: list[str]) -> None:
        """Send the optional email; tolerate failure (Requirements 9.4, 9.5, 9.6).

        On success the alert's ``delivered_email`` flag is set. A send failure is
        caught: the alert is retained, the WebSocket broadcast already completed,
        and the email-delivery failure is recorded (``delivered_email`` left
        false) without disrupting the handler.
        """
        try:
            sent = self.email_sender(alert, reasons)
        except Exception as exc:  # noqa: BLE001 - email is best-effort (9.5)
            logger.warning(
                "herald.email.failed",
                extra={"alertId": alert.id, "error": repr(exc)},
            )
            self._mark_delivered(alert.id, email=False)
            return

        if sent:
            self._mark_delivered(alert.id, email=True)
        else:
            # No SMTP configuration: WebSocket was the primary channel (9.6).
            logger.debug("herald.email.skipped_no_smtp", extra={"alertId": alert.id})

    # -- helpers -------------------------------------------------------------

    def _resolve_exam_id(self, session_id: str) -> str | None:
        """Look up the exam id for ``session_id`` to scope the invigilator room."""
        try:
            repo = self.session_repo_factory()
            session_row = repo.get(session_id)
        except Exception as exc:  # noqa: BLE001 - never abort broadcast on lookup
            logger.warning(
                "herald.exam_lookup.failed",
                extra={"sessionId": session_id, "error": repr(exc)},
            )
            return None
        return session_row.exam_id if session_row is not None else None

    def _mark_delivered(
        self, alert_id: str, *, ws: bool | None = None, email: bool | None = None
    ) -> None:
        """Best-effort update of an alert's delivery flags."""
        try:
            repo = self.alert_repo_factory()
            repo.mark_delivered(alert_id, ws=ws, email=email)
        except Exception as exc:  # noqa: BLE001 - delivery flags are non-critical
            logger.warning(
                "herald.mark_delivered.failed",
                extra={"alertId": alert_id, "error": repr(exc)},
            )


__all__ = [
    "HeraldAgent",
    "SmtpEmailSender",
    "EmailSender",
    "HERALD_SOURCE",
]
