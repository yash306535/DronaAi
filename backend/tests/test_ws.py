"""Unit tests for the WebSocket connection manager (app/core/ws.py).

Covers Requirement 12A acceptance criteria:
- 12A.1: capacity bound + rooms grouped by known room shapes.
- 12A.2: connect binds a connection to a room.
- 12A.3/12A.4: heartbeat pings, pong tracking, prune after 3 missed pings.
- 12A.5: room-scoped delivery only (no cross-room leakage).
- 12A.7: prune-on-delivery-failure and continue to remaining connections.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.ws import (
    InvalidRoomError,
    WebSocketManager,
    WSMessage,
    is_valid_room,
)


class FakeWebSocket:
    """A minimal in-memory stand-in for a Starlette WebSocket.

    Records every JSON frame it receives. When ``fail`` is set, ``send_json``
    raises to simulate a broken connection so prune-on-failure can be exercised.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[Any] = []
        self.fail = fail
        self.closed = False

    async def send_json(self, data: Any) -> None:
        if self.fail:
            raise ConnectionError("socket is broken")
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed = True

    def data_frames(self) -> list[Any]:
        """Frames excluding heartbeat pings."""
        return [f for f in self.sent if f.get("type") != "ping"]


# --- room validation (12A.1, 12A.6) -----------------------------------------


@pytest.mark.parametrize(
    "room",
    ["dashboard", "invigilator:exam-1", "session:sess-42"],
)
def test_valid_room_shapes(room: str) -> None:
    assert is_valid_room(room) is True


@pytest.mark.parametrize(
    "room",
    ["", "unknown", "invigilator:", "session:", "lobby", "dashboard:extra"],
)
def test_invalid_room_shapes(room: str) -> None:
    assert is_valid_room(room) is False


async def test_connect_rejects_unknown_room() -> None:
    manager = WebSocketManager()
    with pytest.raises(InvalidRoomError):
        await manager.connect(FakeWebSocket(), "not-a-room")


async def test_connect_binds_to_room() -> None:
    """12A.2: a connected socket is tracked and counted in its room."""
    manager = WebSocketManager()
    ws = FakeWebSocket()
    await manager.connect(ws, "dashboard")

    assert manager.connection_count == 1
    assert manager.room_size("dashboard") == 1
    assert manager.rooms() == ["dashboard"]


async def test_capacity_bound_is_enforced() -> None:
    """12A.1: the manager refuses connections beyond its capacity."""
    manager = WebSocketManager(max_connections=2)
    await manager.connect(FakeWebSocket(), "dashboard")
    await manager.connect(FakeWebSocket(), "dashboard")
    with pytest.raises(ConnectionError):
        await manager.connect(FakeWebSocket(), "dashboard")


# --- room-scoped delivery isolation (12A.5) ---------------------------------


async def test_broadcast_is_room_scoped() -> None:
    """12A.5: an event for one room is never delivered to another room."""
    manager = WebSocketManager()
    dash = FakeWebSocket()
    invig = FakeWebSocket()
    other_session = FakeWebSocket()

    await manager.connect(dash, "dashboard")
    await manager.connect(invig, "invigilator:exam-1")
    await manager.connect(other_session, "session:sess-1")

    msg = WSMessage(type="alert.broadcast", source="Herald", payload={"x": 1})
    delivered = await manager.broadcast("dashboard", msg)

    assert delivered == 1
    assert len(dash.data_frames()) == 1
    assert dash.data_frames()[0]["type"] == "alert.broadcast"
    # Connections in other rooms received nothing.
    assert invig.data_frames() == []
    assert other_session.data_frames() == []


async def test_broadcast_reaches_all_in_room_only() -> None:
    """12A.5: every connection in the target room receives the event."""
    manager = WebSocketManager()
    a, b = FakeWebSocket(), FakeWebSocket()
    outsider = FakeWebSocket()
    await manager.connect(a, "session:sess-1")
    await manager.connect(b, "session:sess-1")
    await manager.connect(outsider, "session:sess-2")

    delivered = await manager.broadcast(
        "session:sess-1", {"type": "session.update", "payload": {}}
    )

    assert delivered == 2
    assert len(a.data_frames()) == 1
    assert len(b.data_frames()) == 1
    assert outsider.data_frames() == []


async def test_broadcast_to_empty_room_is_noop() -> None:
    manager = WebSocketManager()
    delivered = await manager.broadcast("dashboard", {"type": "agent.status"})
    assert delivered == 0


# --- prune-on-delivery-failure (12A.7) --------------------------------------


async def test_broadcast_prunes_failed_and_continues() -> None:
    """12A.7: a failed delivery prunes that connection; others still receive."""
    manager = WebSocketManager()
    good1 = FakeWebSocket()
    bad = FakeWebSocket(fail=True)
    good2 = FakeWebSocket()
    await manager.connect(good1, "dashboard")
    await manager.connect(bad, "dashboard")
    await manager.connect(good2, "dashboard")

    delivered = await manager.broadcast("dashboard", {"type": "alert.broadcast"})

    # Both healthy connections received the message despite the failing one.
    assert delivered == 2
    assert len(good1.data_frames()) == 1
    assert len(good2.data_frames()) == 1
    # The broken connection was pruned and closed.
    assert bad.closed is True
    assert manager.room_size("dashboard") == 2
    assert manager.connection_count == 2


async def test_send_personal_prunes_on_failure() -> None:
    manager = WebSocketManager()
    bad = FakeWebSocket(fail=True)
    await manager.connect(bad, "dashboard")

    ok = await manager.send_personal(bad, {"type": "agent.status"})

    assert ok is False
    assert bad.closed is True
    assert manager.connection_count == 0


async def test_disconnect_is_idempotent() -> None:
    manager = WebSocketManager()
    ws = FakeWebSocket()
    await manager.connect(ws, "dashboard")
    manager.disconnect(ws)
    # Second disconnect is a harmless no-op.
    manager.disconnect(ws)
    assert manager.connection_count == 0
    assert manager.rooms() == []


# --- heartbeat pruning (12A.3, 12A.4) ---------------------------------------


async def test_heartbeat_prunes_after_three_missed_pings() -> None:
    """12A.4: 3 consecutive missed pongs prune the connection."""
    manager = WebSocketManager(pong_timeout=0)
    ws = FakeWebSocket()
    await manager.connect(ws, "dashboard")

    # Never call record_pong -> every cycle counts as a miss.
    await manager._heartbeat_cycle()
    assert manager.connection_count == 1  # 1 miss
    await manager._heartbeat_cycle()
    assert manager.connection_count == 1  # 2 misses
    await manager._heartbeat_cycle()
    # 3rd miss -> pruned and closed.
    assert manager.connection_count == 0
    assert ws.closed is True


async def test_heartbeat_pong_resets_miss_counter() -> None:
    """12A.3: a pong clears the pending flag and resets the miss counter."""
    manager = WebSocketManager(pong_timeout=0)
    ws = FakeWebSocket()
    await manager.connect(ws, "dashboard")

    await manager._heartbeat_cycle()  # 1 miss
    await manager._heartbeat_cycle()  # 2 misses
    # Client answers the next ping promptly.
    await manager._send_pings()
    manager.record_pong(ws)
    await manager._evaluate_pongs(list(manager._by_ws.values()))

    # Counter reset; a single subsequent miss should not prune.
    await manager._heartbeat_cycle()
    assert manager.connection_count == 1
    assert ws.closed is False


async def test_heartbeat_sends_ping_frames() -> None:
    """12A.3: the heartbeat sends a ping control frame to live connections."""
    manager = WebSocketManager(pong_timeout=0)
    ws = FakeWebSocket()
    await manager.connect(ws, "dashboard")

    await manager._send_pings()
    pings = [f for f in ws.sent if f.get("type") == "ping"]
    assert len(pings) == 1
