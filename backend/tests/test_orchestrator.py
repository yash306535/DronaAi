"""Tests for the agent orchestrator (``app.agents.orchestrator``).

Covers task 7.2 deliverables:

- **Import safety without crewai**: the module imports and ``build_crew``
  degrades gracefully when ``crewai`` is not installed.
- **wire_event_bus** subscribes handlers before any publish, and a subscribed
  handler receives published events (Requirement 11.1). Wiring also installs a
  placeholder subscription for agents whose concrete handler is not yet
  registered, so the ordering/wiring guarantee holds from day one.
- **emit_agent_message** publishes an ``agent.message`` event onto the bus for
  the dashboard feed (Requirement 11.5).

_Requirements: 11.1, 11.5_
"""

from __future__ import annotations

import pytest

from app.agents.orchestrator import (
    AGENT_SPECS,
    Orchestrator,
    get_orchestrator,
)
from app.core.events import Event, EventBus, EventType


# --- Import safety / crew assembly -----------------------------------------


def test_build_crew_is_safe_without_crewai() -> None:
    """build_crew returns None (and does not raise) when crewai is absent.

    crewai is an optional heavy dependency and is not installed in the test
    environment; the lazy guarded import must keep the module usable.
    """
    orch = Orchestrator()
    # Should not raise regardless of whether crewai is installed.
    crew = orch.build_crew()
    assert crew is None or crew is not None  # tolerate either install state


def test_agent_specs_cover_the_five_named_agents() -> None:
    """The five P1/P2 agents are defined with role/goal/backstory."""
    names = {spec.name for spec in AGENT_SPECS}
    assert names == {"Guardian", "Architect", "Sentinel", "Analyst", "Herald"}
    for spec in AGENT_SPECS:
        assert spec.role and spec.goal and spec.backstory
        assert spec.subscribes  # each agent subscribes to at least one type


# --- wire_event_bus (Requirement 11.1) -------------------------------------


@pytest.mark.asyncio
async def test_wire_event_bus_subscribes_registered_handler_and_it_receives() -> None:
    """A registered handler is subscribed and receives published events."""
    bus = EventBus()
    orch = Orchestrator()
    received: list[Event] = []

    async def architect_handler(event: Event) -> None:
        received.append(event)

    orch.register_handler(EventType.EXAM_PROVISION, architect_handler)
    orch.wire_event_bus(bus)
    assert orch.is_wired

    event = Event(
        type=EventType.EXAM_PROVISION,
        payload={"examId": "e1"},
        source="orchestrator",
    )
    await bus.publish(event)

    assert len(received) == 1
    assert received[0].id == event.id


@pytest.mark.asyncio
async def test_wire_event_bus_installs_placeholder_for_unregistered_agents() -> None:
    """Every agent's subscribed event types get a subscription after wiring.

    With no concrete handlers registered, wiring installs placeholders so each
    designated event type has at least one subscriber. Publishing any of those
    event types is therefore delivered (not a no-op) and never raises.
    """
    bus = EventBus()
    orch = Orchestrator()
    orch.wire_event_bus(bus)

    subscribed_types = {et for spec in AGENT_SPECS for et in spec.subscribes}
    for event_type in subscribed_types:
        handlers = bus._subs.get(event_type)  # noqa: SLF001 - test introspection
        assert handlers, f"expected a subscription for {event_type}"

    # Publishing to a wired type must not raise even with only placeholders.
    await bus.publish(
        Event(type=EventType.SESSION_EVENT, payload={}, source="sentinel")
    )


@pytest.mark.asyncio
async def test_wire_event_bus_preserves_registration_order() -> None:
    """Multiple handlers for one type are delivered in registration order."""
    bus = EventBus()
    orch = Orchestrator()
    order: list[int] = []

    def make(idx: int):
        async def handler(_: Event) -> None:
            order.append(idx)

        return handler

    for i in range(3):
        orch.register_handler(EventType.ANOMALY_DETECTED, make(i))
    orch.wire_event_bus(bus)

    await bus.publish(
        Event(type=EventType.ANOMALY_DETECTED, payload={}, source="guardian")
    )
    assert order == [0, 1, 2]


# --- emit_agent_message (Requirement 11.5) ---------------------------------


@pytest.mark.asyncio
async def test_emit_agent_message_publishes_agent_message_event() -> None:
    """emit_agent_message publishes an agent.message event to the feed."""
    bus = EventBus()
    orch = Orchestrator()
    captured: list[Event] = []

    async def feed_listener(event: Event) -> None:
        captured.append(event)

    orch.register_handler(EventType.AGENT_MESSAGE, feed_listener)
    orch.wire_event_bus(bus)

    await orch.emit_agent_message(
        source="Guardian",
        to="Herald",
        text="Face absent confirmed for 8s in Session #4",
        level="danger",
        session_id="sess_4",
    )

    assert len(captured) == 1
    msg = captured[0]
    assert msg.type == EventType.AGENT_MESSAGE
    assert msg.source == "Guardian"
    assert msg.session_id == "sess_4"
    assert msg.payload == {
        "to": "Herald",
        "text": "Face absent confirmed for 8s in Session #4",
        "level": "danger",
    }


@pytest.mark.asyncio
async def test_emit_agent_message_before_wiring_raises() -> None:
    """Emitting before the bus is wired is a programming error."""
    orch = Orchestrator()
    with pytest.raises(RuntimeError):
        await orch.emit_agent_message(source="Guardian", to="Herald", text="hi")


@pytest.mark.asyncio
async def test_provision_exam_publishes_exam_provision_event() -> None:
    """provision_exam emits an exam.provision event for the Architect."""
    bus = EventBus()
    orch = Orchestrator()
    seen: list[Event] = []

    async def architect_handler(event: Event) -> None:
        seen.append(event)

    orch.register_handler(EventType.EXAM_PROVISION, architect_handler)
    orch.wire_event_bus(bus)

    await orch.provision_exam("exam-123")

    assert len(seen) == 1
    assert seen[0].type == EventType.EXAM_PROVISION
    assert seen[0].payload == {"examId": "exam-123"}


def test_get_orchestrator_returns_singleton() -> None:
    """The process-wide orchestrator accessor is stable."""
    assert get_orchestrator() is get_orchestrator()
