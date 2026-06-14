"""Event-bus → WebSocket bridge (Requirements 11.5, 12.3, 12.4, 12.6, 10.5).

The orchestration backbone is two decoupled fan-out mechanisms:

- the in-process :class:`~app.core.events.EventBus`, onto which agents and the
  REST/service layer publish typed :class:`~app.core.events.Event` values, and
- the :class:`~app.core.ws.WebSocketManager`, which fans messages out to the
  live ``dashboard`` / ``invigilator:{exam_id}`` / ``session:{session_id}``
  rooms the browser dashboard subscribes to.

The Herald already bridges ``anomaly.detected`` → ``alert.broadcast`` itself
(Requirement 9.2). But the rest of the live dashboard feed — the inter-agent
communication log (``agent.message``, Requirement 11.5 / 12.3), agent status
cards (``agent.status``, Requirement 12.4), session tiles (``session.update``,
Requirement 12.6), and the analytics-ready signal (``report.ready``,
Requirement 10.5 / 12) — is published onto the :class:`EventBus` but has **no
transport-facing subscriber**. Without a bridge those events are delivered to
zero handlers and discarded by the bus (Requirement 11.7), so the dashboard
never sees them.

:class:`WebSocketBridge` is that missing subscriber. It registers one handler
per dashboard-facing :class:`EventType` on the bus and, for each event,
translates it into a :class:`~app.core.ws.WSMessage` and broadcasts it to the
``dashboard`` room (and, where the event is scoped to an exam session, the
matching ``invigilator:{exam_id}`` room as well). It owns no state beyond the
manager + an optional session→exam resolver, and every handler is failure
tolerant: a translation/broadcast error is logged and swallowed so the bus's
delivery to *other* handlers (e.g. the Herald) is never disrupted
(Requirement 11.4 is enforced by the bus; the bridge simply never raises).

Wiring: :func:`register_ws_bridge` subscribes the bridge onto a bus and is
called from the app lifespan (``app/main.py``) *after* the orchestrator wires
its agents but still before any event is published (Requirement 11.1).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.db import get_session_factory
from app.core.events import Event, EventBus, EventType
from app.core.logging import get_logger
from app.core.ws import (
    DASHBOARD_ROOM,
    INVIGILATOR_ROOM_PREFIX,
    WSMessage,
    WebSocketManager,
    get_ws_manager,
)

logger = get_logger("app.core.ws_bridge")

# The EventBus event types that should be mirrored onto the dashboard feed.
# ``anomaly.detected`` / ``alert.broadcast`` are intentionally excluded: the
# Herald owns those (it persists an alert and broadcasts ``alert.broadcast``
# only for a confirmed anomaly, Requirements 9.2/9.3). Bridging them here too
# would double-broadcast or leak unconfirmed anomalies.
_BRIDGED_EVENT_TYPES: tuple[EventType, ...] = (
    EventType.AGENT_MESSAGE,  # 11.5 / 12.3 — inter-agent communication log
    EventType.SESSION_UPDATE,  # 12.6 — session tile integrity/status
    EventType.REPORT_READY,  # 10.5 / 12 — analytics ready
)

# An exam-id resolver maps a session id to its exam id (for the invigilator
# room). It returns ``None`` when the session is unknown.
ExamIdResolver = Callable[[str], "str | None"]


def _default_exam_id_resolver(session_id: str) -> str | None:
    """Resolve a session's exam id via a short-lived repository session.

    Used to scope an exam-bound event to its ``invigilator:{exam_id}`` room in
    addition to the global ``dashboard`` room. Any lookup failure yields
    ``None`` (the event still reaches the dashboard).
    """
    try:
        from app.repositories.session import ExamSessionRepository

        db = get_session_factory()()
        try:
            row = ExamSessionRepository(db).get(session_id)
            return row.exam_id if row is not None else None
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 - lookup is best-effort
        logger.debug(
            "ws_bridge.exam_lookup.failed",
            extra={"sessionId": session_id, "error": repr(exc)},
        )
        return None


@dataclass(slots=True)
class WebSocketBridge:
    """Forward dashboard-facing :class:`EventBus` events onto the WS rooms.

    ``ws_manager`` is the process-wide room fan-out. ``exam_id_resolver`` maps a
    session id to an exam id so a session-scoped event also reaches the exam's
    ``invigilator`` room; it defaults to a repository-backed lookup but is
    injectable for tests.
    """

    ws_manager: WebSocketManager = field(default_factory=get_ws_manager)
    exam_id_resolver: ExamIdResolver = _default_exam_id_resolver

    # -- bus handler ---------------------------------------------------------

    async def on_event(self, event: Event) -> None:
        """Translate ``event`` to a :class:`WSMessage` and broadcast it.

        Always broadcasts to the ``dashboard`` room (Requirement 12.3/12.4/12.6).
        When the event carries a ``sessionId`` (directly or in its payload) and
        that session resolves to an exam, it is also delivered to the matching
        ``invigilator:{exam_id}`` room so per-exam invigilators see the same
        feed. Never raises: a failure is logged so the bus's delivery to other
        handlers is unaffected.
        """
        try:
            session_id = self._session_id_of(event)
            message = WSMessage(
                type=event.type.value,
                source=event.source,
                payload=event.payload,
                session_id=session_id,
            )
            await self.ws_manager.broadcast(DASHBOARD_ROOM, message)

            if session_id:
                exam_id = self._safe_resolve_exam_id(session_id)
                if exam_id:
                    await self.ws_manager.broadcast(
                        f"{INVIGILATOR_ROOM_PREFIX}{exam_id}", message
                    )
        except Exception as exc:  # noqa: BLE001 - never disrupt bus delivery
            logger.warning(
                "ws_bridge.forward.failed",
                extra={"eventType": str(event.type), "error": repr(exc)},
            )

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _session_id_of(event: Event) -> str | None:
        """Return the event's session id from the event or its payload."""
        if event.session_id:
            return event.session_id
        payload = event.payload or {}
        value = payload.get("sessionId")
        return value if isinstance(value, str) and value else None

    def _safe_resolve_exam_id(self, session_id: str) -> str | None:
        """Resolve the exam id for a session, swallowing any resolver error."""
        try:
            return self.exam_id_resolver(session_id)
        except Exception as exc:  # noqa: BLE001 - resolver is best-effort
            logger.debug(
                "ws_bridge.exam_lookup.error",
                extra={"sessionId": session_id, "error": repr(exc)},
            )
            return None


def register_ws_bridge(
    bus: EventBus,
    *,
    ws_manager: WebSocketManager | None = None,
    exam_id_resolver: ExamIdResolver | None = None,
) -> WebSocketBridge:
    """Build a :class:`WebSocketBridge` and subscribe it onto ``bus``.

    Subscribes one handler per bridged :class:`EventType` so the dashboard feed
    (``agent.message``, ``session.update``, ``report.ready``) reaches the live
    WebSocket rooms. Called from the app lifespan after the orchestrator wires
    its agents but before any event is published (Requirement 11.1). Returns the
    bridge so the caller can hold a reference (kept alive on ``app.state``).
    """
    bridge = WebSocketBridge(
        ws_manager=ws_manager if ws_manager is not None else get_ws_manager(),
        exam_id_resolver=(
            exam_id_resolver
            if exam_id_resolver is not None
            else _default_exam_id_resolver
        ),
    )
    for event_type in _BRIDGED_EVENT_TYPES:
        bus.subscribe(event_type, bridge.on_event)
    logger.info(
        "ws_bridge.registered",
        extra={"eventTypes": [str(t) for t in _BRIDGED_EVENT_TYPES]},
    )
    return bridge


__all__ = [
    "WebSocketBridge",
    "register_ws_bridge",
    "ExamIdResolver",
]
