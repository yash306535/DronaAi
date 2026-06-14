"""Tests for the in-process async event bus (``app.core.events``).

Two groups:

- **Property 7 — Event delivery idempotency** (sub-task 3.1, hypothesis):
  replaying the same ``event.id`` never produces duplicate side effects such as
  anomalies/alerts. **Validates: Requirements 11.6**
- **Delivery semantics** (sub-task 3.2, unit): registration-order invocation,
  one failing handler does not block others, no-subscriber publish is a safe
  no-op, and delivery completes within the timing bound.
  _Requirements: 11.1, 11.2, 11.3, 11.4, 11.7_
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.events import Event, EventBus, EventType


def _make_event(event_id: str | None = None, **overrides) -> Event:
    """Construct an ``anomaly.detected`` event, optionally with a fixed id."""
    kwargs: dict = {
        "type": EventType.ANOMALY_DETECTED,
        "payload": {"category": "face_absent", "score": 0.9},
        "source": "guardian",
        "session_id": "sess_1",
    }
    kwargs.update(overrides)
    if event_id is not None:
        kwargs["id"] = event_id
    return Event(**kwargs)


# --- Property 7: Event delivery idempotency (sub-task 3.1) ------------------
# **Validates: Requirements 11.6**


@settings(max_examples=200, deadline=None)
@given(
    event_id=st.uuids().map(str),
    # Number of times the SAME event id is (re)published, including the first.
    replays=st.integers(min_value=1, max_value=8),
    # Number of independent, distinct events also published alongside it.
    other_events=st.integers(min_value=0, max_value=5),
)
def test_replaying_event_id_creates_no_duplicate_side_effects(
    event_id: str, replays: int, other_events: int
) -> None:
    """Republishing an id only ever yields one delivery for that id.

    A subscribed handler records every event id it actually processes (modeling
    anomaly/alert creation). No matter how many times an id is replayed, the
    handler must observe it at most once, so no duplicate anomaly/alert can be
    created for that id.
    """

    async def scenario() -> None:
        bus = EventBus()
        processed: list[str] = []

        async def handler(event: Event) -> None:
            processed.append(event.id)

        bus.subscribe(EventType.ANOMALY_DETECTED, handler)

        # Publish the same event id `replays` times (a replay each iteration).
        for _ in range(replays):
            await bus.publish(_make_event(event_id=event_id))

        # Publish some distinct events to ensure dedup is per-id, not global.
        distinct_ids = [f"{event_id}-other-{i}" for i in range(other_events)]
        for other_id in distinct_ids:
            await bus.publish(_make_event(event_id=other_id))

        # The replayed id is processed exactly once.
        assert processed.count(event_id) == 1
        # Each distinct id is processed exactly once as well.
        for other_id in distinct_ids:
            assert processed.count(other_id) == 1
        # No id appears more than once: no duplicate side effects anywhere.
        assert len(processed) == len(set(processed))
        assert len(processed) == 1 + other_events

    asyncio.run(scenario())


# --- Delivery semantics (sub-task 3.2) -------------------------------------


@pytest.mark.asyncio
async def test_handlers_invoked_in_registration_order() -> None:
    """Requirement 11.3: handlers run in the order they were subscribed."""
    bus = EventBus()
    order: list[int] = []

    def make(idx: int):
        async def handler(_: Event) -> None:
            order.append(idx)

        return handler

    for i in range(5):
        bus.subscribe(EventType.SESSION_EVENT, make(i))

    await bus.publish(
        Event(type=EventType.SESSION_EVENT, payload={}, source="sentinel")
    )

    assert order == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_failing_handler_does_not_block_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Requirement 11.4: a raising handler is logged and the rest still run."""
    bus = EventBus()
    seen: list[str] = []

    async def ok_before(_: Event) -> None:
        seen.append("before")

    async def boom(_: Event) -> None:
        raise RuntimeError("handler exploded")

    async def ok_after(_: Event) -> None:
        seen.append("after")

    bus.subscribe(EventType.ANOMALY_DETECTED, ok_before)
    bus.subscribe(EventType.ANOMALY_DETECTED, boom)
    bus.subscribe(EventType.ANOMALY_DETECTED, ok_after)

    event = _make_event(event_id="evt-fail-1")
    with caplog.at_level(logging.ERROR):
        # Must not raise to the publisher.
        await bus.publish(event)

    # Both non-failing handlers ran despite the middle one raising.
    assert seen == ["before", "after"]

    # The central log entry includes the event id and a handler identifier.
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "expected a central error log for the failing handler"
    record = error_records[0]
    assert getattr(record, "eventId", None) == "evt-fail-1"
    assert "boom" in getattr(record, "handlerId", "")


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_is_safe_noop() -> None:
    """Requirement 11.7: publishing with no subscribers raises nothing."""
    bus = EventBus()
    # No subscribers registered for any type.
    await bus.publish(_make_event(event_id="evt-orphan"))
    # A type with a subscriber for a *different* type is also a no-op here.
    bus.subscribe(EventType.PAPER_GENERATED, _noop_handler)
    await bus.publish(_make_event(event_id="evt-orphan-2"))


async def _noop_handler(_: Event) -> None:
    return None


@pytest.mark.asyncio
async def test_delivery_within_timing_bound() -> None:
    """Requirement 11.2: in-process delivery completes well under 1000ms."""
    bus = EventBus()
    delivered = 0

    async def handler(_: Event) -> None:
        nonlocal delivered
        delivered += 1

    for _ in range(50):
        bus.subscribe(EventType.AGENT_MESSAGE, handler)

    start = time.perf_counter()
    await bus.publish(
        Event(type=EventType.AGENT_MESSAGE, payload={"text": "hi"}, source="herald")
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert delivered == 50
    assert elapsed_ms < 1000, f"delivery took {elapsed_ms:.1f}ms"


@pytest.mark.asyncio
async def test_subscribe_then_publish_reaches_handler() -> None:
    """Requirement 11.1: a subscribed handler receives published events."""
    bus = EventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe(EventType.EXAM_COMPLETED, handler)
    event = Event(type=EventType.EXAM_COMPLETED, payload={"examId": "e1"}, source="orchestrator")
    await bus.publish(event)

    assert len(received) == 1
    assert received[0].id == event.id
