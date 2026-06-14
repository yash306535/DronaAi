"""In-process asynchronous event bus (publish/subscribe).

This module is the orchestration backbone described in the design's *Event Bus
Contract*. Agents never call each other directly; they publish typed
:class:`Event` instances onto a shared :class:`EventBus` and subscribe handlers
to the :class:`EventType` values they care about. The bus is intentionally
simple so it fits the 72h build window, but it is shaped so it could later be
swapped for Redis/NATS without touching agents.

Delivery semantics (Requirement 11):

- **11.1** ``subscribe`` registers handlers so the orchestrator can wire every
  agent before any event is published.
- **11.2** ``publish`` fans the event out to all subscribed handlers and
  completes well within the 1000ms delivery budget for in-process handlers.
- **11.3** Handlers are *invoked* in their registration order (the order in
  which they were subscribed for that event type).
- **11.4** A handler that raises does not abort delivery: the exception is
  captured, logged centrally with the event id and a stable handler identifier,
  and the remaining handlers still run. The exception is never re-raised to the
  publisher.
- **11.6** Each ``event.id`` is delivered at most once. Republishing an event
  whose id has already been processed is discarded so replays cannot create
  duplicate anomalies/alerts (Correctness Property 7).
- **11.7** Publishing an event with no subscribers is a safe no-op.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Awaitable, Callable

from app.core.logging import get_logger

logger = get_logger(__name__)

# Bound on the dedup registry so a long-running process cannot grow it without
# limit. Once exceeded, the oldest processed ids are evicted (FIFO). The bound
# is generous relative to the number of in-flight events in a single exam.
_DEFAULT_DEDUP_CAPACITY = 100_000


class EventType(StrEnum):
    """The set of events agents publish and subscribe to.

    Values mirror the wire/event names used across the system (and the
    dashboard feed), so a string round-trips cleanly to and from transport.
    """

    EXAM_PROVISION = "exam.provision"
    PAPER_GENERATED = "paper.generated"
    # Emitted by the Architect when paper generation for a student cannot
    # complete — either schema validation failed after the configured retries
    # were exhausted (Requirement 4.6) or persistence of a generated paper
    # failed (Requirement 4.8). Carries the student and the failure cause so
    # downstream observers (dashboard, provisioning status) can react without a
    # ``paper.generated`` event ever being emitted for that student.
    GENERATION_FAILED = "generation.failed"
    AUDIT_COMPLETED = "audit.completed"
    # Emitted by the Auditor when a paper's fairness review cannot be completed
    # (e.g. the language model is unavailable or returns unparseable output).
    # The paper's audit status is left unchanged from its pre-review value and
    # the event carries the reason the review could not be completed
    # (Requirement 13.6). This is distinct from ``audit.completed``, which
    # always carries a produced verdict (approved/needs_revision).
    AUDIT_FAILED = "audit.failed"
    SESSION_EVENT = "session.event"
    FRAME_ESCALATED = "frame.escalated"
    ANOMALY_DETECTED = "anomaly.detected"
    ALERT_BROADCAST = "alert.broadcast"
    # Emitted whenever a session's integrity score or status changes
    # (Requirements 14.5/14.6). The value matches the dashboard's
    # ``session.update`` WS message type (``WSMessageType.SESSION_UPDATE``) so a
    # WS-facing subscriber can forward it to the ``dashboard`` room without
    # translation (design "WebSocket Event Schema": ``session.update`` updates a
    # session tile's integrity score / status — Requirement 12.6).
    SESSION_UPDATE = "session.update"
    EXAM_COMPLETED = "exam.completed"
    REPORT_READY = "report.ready"
    AGENT_MESSAGE = "agent.message"  # the "agents talking" feed


@dataclass(slots=True)
class Event:
    """A single immutable-by-convention message on the bus.

    ``id`` is the dedup key (Requirement 11.6); it defaults to a fresh UUID but
    may be supplied explicitly to model a replay of a previously emitted event.
    ``ts`` is the ISO-8601 UTC creation time. ``source`` is the emitting agent
    name (e.g. ``"guardian"``) and ``session_id`` scopes the event to an exam
    session when applicable.
    """

    type: EventType
    payload: dict[str, Any]
    source: str
    session_id: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


Handler = Callable[[Event], Awaitable[None]]


def _handler_id(handler: Handler) -> str:
    """Return a stable, human-readable identifier for a handler.

    Used in central log entries (Requirement 11.4) so a failing handler can be
    pinpointed. Falls back to ``repr`` when a qualified name is unavailable.
    """
    name = getattr(handler, "__qualname__", None) or getattr(
        handler, "__name__", None
    )
    if name:
        module = getattr(handler, "__module__", "?")
        return f"{module}.{name}"
    return repr(handler)


class EventBus:
    """An in-process async pub/sub hub with at-most-once delivery per event id."""

    def __init__(self, dedup_capacity: int = _DEFAULT_DEDUP_CAPACITY) -> None:
        # Preserve registration order per event type (Requirement 11.3).
        self._subs: dict[EventType, list[Handler]] = {}
        # Ordered set of processed event ids for dedup (Requirement 11.6). An
        # OrderedDict gives us O(1) membership plus FIFO eviction.
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._dedup_capacity = dedup_capacity

    def subscribe(self, event_type: EventType, handler: Handler) -> None:
        """Register ``handler`` for ``event_type``.

        Handlers are appended, so delivery later follows subscription order
        (Requirement 11.3). The orchestrator calls this for every agent before
        any event is published (Requirement 11.1).
        """
        self._subs.setdefault(event_type, []).append(handler)

    def _is_duplicate(self, event_id: str) -> bool:
        """Record ``event_id`` and report whether it was already processed.

        First sight returns ``False`` and remembers the id; any later sighting
        returns ``True`` so the publisher can discard the replay.
        """
        if event_id in self._seen:
            return True
        self._seen[event_id] = None
        # FIFO eviction once the registry is full.
        while len(self._seen) > self._dedup_capacity:
            self._seen.popitem(last=False)
        return False

    async def publish(self, event: Event) -> None:
        """Deliver ``event`` to every handler subscribed to its type.

        Duplicate event ids are discarded (11.6). Events with no subscribers are
        a no-op (11.7). Handlers are invoked in registration order (11.3),
        concurrently fanned out, and a handler raising does not stop the others;
        its exception is logged centrally and never re-raised (11.4).
        """
        if self._is_duplicate(event.id):
            logger.debug(
                "Discarding duplicate event",
                extra={"eventId": event.id, "eventType": str(event.type)},
            )
            return

        handlers = self._subs.get(event.type, [])
        if not handlers:
            # No subscribers: discard silently (Requirement 11.7).
            return

        # Invoke in registration order; gather preserves the ordering of the
        # supplied coroutines and runs them concurrently (Requirement 11.2/11.3).
        results = await asyncio.gather(
            *(handler(event) for handler in handlers),
            return_exceptions=True,
        )

        for handler, result in zip(handlers, results):
            if isinstance(result, BaseException):
                # Capture, log centrally with event id + handler id, continue
                # (Requirement 11.4). Never re-raise to the publisher.
                logger.error(
                    "Event handler raised during delivery",
                    extra={
                        "eventId": event.id,
                        "eventType": str(event.type),
                        "handlerId": _handler_id(handler),
                        "error": repr(result),
                    },
                )
