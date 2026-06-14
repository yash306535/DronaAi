"""Request-scoped access to process-wide runtime singletons.

The application factory (``app/main.py``) builds the in-process
:class:`~app.core.events.EventBus` during lifespan startup and stashes it on
``app.state.event_bus`` (wiring the agent orchestrator onto it before any event
is published, Requirement 11.1). Routers need that same bus to publish domain
events (e.g. ``exam.completed`` on submit, ``session.event`` per ingested
telemetry record) without reaching across layers or importing ``main``.

:func:`get_event_bus` is a tiny FastAPI dependency that reads the bus off the
running app's state. It returns ``None`` when no bus is present (e.g. a minimal
test app that mounts the router without the full lifespan) so callers can treat
publishing as best-effort rather than crashing a request when the backbone is
not wired.
"""

from __future__ import annotations

from fastapi import Request

from app.core.events import EventBus


def get_event_bus(request: Request) -> EventBus | None:
    """Return the process-wide :class:`EventBus`, or ``None`` if unwired.

    The bus is created in the app lifespan and stored on ``app.state.event_bus``
    (see ``app/main.py``). Reading it here keeps routers decoupled from the app
    factory while still publishing onto the same bus the orchestrator wired.
    """
    return getattr(request.app.state, "event_bus", None)


__all__ = ["get_event_bus"]
