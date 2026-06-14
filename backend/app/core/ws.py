"""In-process WebSocket connection manager with room-scoped fan-out.

This module owns the live WebSocket connection registry for the DRONA AI
backend. Connections are grouped into *rooms* and events are delivered only to
the connections bound to a target room, never to other rooms.

Implements Requirement 12A acceptance criteria:

- 12A.1: maintains up to ``MAX_CONNECTIONS`` (10,000) concurrent connections
  grouped by room, where a room is one of ``dashboard``,
  ``invigilator:{exam_id}``, or ``session:{session_id}``.
- 12A.2: :meth:`WebSocketManager.connect` binds an (already-authenticated)
  connection to a room and makes it eligible for room-targeted delivery.
- 12A.3: while a connection is open the heartbeat loop sends a ping every
  ``ping_interval`` seconds (default 30s) and expects a pong within
  ``pong_timeout`` seconds (default 10s).
- 12A.4: a connection that misses ``max_missed_pings`` (default 3) consecutive
  heartbeats is pruned — closed, removed from its room, and its resources
  released — promptly (well within the 5s budget, since pruning is immediate
  once the third miss is observed).
- 12A.5: :meth:`WebSocketManager.broadcast` delivers an event only to the
  connections bound to the target room and to no other room.
- 12A.7: if delivery to a bound connection fails, that connection is pruned and
  delivery continues to the remaining connections in the room.

Auth (JWT + role validation) and room-name validation against concrete exam /
session identifiers are the responsibility of the routes layer (task 6.2). This
manager only knows the *shape* of a valid room name and treats the supplied
``user`` object as opaque.

Design references: design.md "Component: WebSocket Manager" and the
"WebSocket Event Schema" envelope.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.core.logging import get_logger

logger = get_logger(__name__)

# Upper bound on simultaneously tracked connections (Requirement 12A.1).
MAX_CONNECTIONS = 10_000

# Heartbeat defaults (Requirements 12A.3, 12A.4).
DEFAULT_PING_INTERVAL_SECONDS = 30.0
DEFAULT_PONG_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_MISSED_PINGS = 3

# Reserved room name for all admins.
DASHBOARD_ROOM = "dashboard"
# Prefixes for the scoped rooms.
INVIGILATOR_ROOM_PREFIX = "invigilator:"
SESSION_ROOM_PREFIX = "session:"


# --- Message envelope -------------------------------------------------------


class WSMessageType(StrEnum):
    """The set of message types carried over the live socket.

    Mirrors the ``WSMessageType`` union in the design's WebSocket Event Schema.
    The full Pydantic envelope schema is defined alongside the routes in task
    6.2; this manager only needs the type tags and an envelope it can serialize.
    """

    AGENT_MESSAGE = "agent.message"
    AGENT_STATUS = "agent.status"
    ANOMALY_DETECTED = "anomaly.detected"
    ALERT_BROADCAST = "alert.broadcast"
    SESSION_UPDATE = "session.update"
    REPORT_READY = "report.ready"
    # Control frame used by the heartbeat loop; not part of the public schema.
    PING = "ping"


@dataclass(slots=True)
class WSMessage:
    """A minimal WebSocket message envelope (design "Message Envelope").

    The routes layer may construct these directly or pass a plain ``dict``; both
    are accepted by the manager's delivery methods. ``payload`` is an arbitrary
    JSON-serializable object.
    """

    type: str
    source: str
    payload: Any = None
    session_id: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        """Render the envelope as a JSON-serializable dict (camelCase keys)."""
        envelope: dict[str, Any] = {
            "type": self.type,
            "id": self.id,
            "ts": self.ts,
            "source": self.source,
            "payload": self.payload,
        }
        if self.session_id is not None:
            envelope["sessionId"] = self.session_id
        return envelope


def _as_payload(message: WSMessage | dict[str, Any]) -> dict[str, Any]:
    """Normalize a message argument to a JSON-serializable dict."""
    if isinstance(message, WSMessage):
        return message.to_dict()
    return message


# --- WebSocket abstraction --------------------------------------------------


@runtime_checkable
class WebSocketLike(Protocol):
    """The subset of the Starlette ``WebSocket`` API this manager relies on.

    Declaring a protocol keeps the manager decoupled from FastAPI/Starlette so
    it can be unit-tested with a lightweight fake socket.
    """

    async def send_json(self, data: Any) -> None: ...

    async def close(self, code: int = 1000) -> None: ...


# --- Errors -----------------------------------------------------------------


class InvalidRoomError(ValueError):
    """Raised when a room name does not match a known room shape (12A.1/12A.6).

    The routes layer is expected to catch this and close the connection without
    binding it to any room.
    """


def is_valid_room(room: str) -> bool:
    """Return whether ``room`` matches one of the known room shapes.

    Valid rooms are ``dashboard``, ``invigilator:{exam_id}``, and
    ``session:{session_id}`` where the identifier suffix is non-empty.
    """
    if not isinstance(room, str) or not room:
        return False
    if room == DASHBOARD_ROOM:
        return True
    for prefix in (INVIGILATOR_ROOM_PREFIX, SESSION_ROOM_PREFIX):
        if room.startswith(prefix) and len(room) > len(prefix):
            return True
    return False


# --- Connection record ------------------------------------------------------


@dataclass(slots=True, eq=False)
class Connection:
    """A single tracked WebSocket bound to exactly one room.

    ``eq=False`` keeps identity-based equality/hashing so connections can live
    in a ``set`` and be removed by identity even though they carry mutable
    heartbeat state.
    """

    ws: WebSocketLike
    room: str
    user: Any = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # Heartbeat bookkeeping (Requirements 12A.3, 12A.4).
    pong_pending: bool = False
    missed_pings: int = 0


# --- Manager ----------------------------------------------------------------


class WebSocketManager:
    """Tracks live connections per room and fans out room-scoped events.

    The manager is safe to share across the app; it is the single owner of the
    room registry. It does not perform authentication — callers pass an already
    authorized connection plus an opaque ``user`` for context.
    """

    def __init__(
        self,
        *,
        max_connections: int = MAX_CONNECTIONS,
        ping_interval: float = DEFAULT_PING_INTERVAL_SECONDS,
        pong_timeout: float = DEFAULT_PONG_TIMEOUT_SECONDS,
        max_missed_pings: int = DEFAULT_MAX_MISSED_PINGS,
    ) -> None:
        self._max_connections = max_connections
        self._ping_interval = ping_interval
        self._pong_timeout = pong_timeout
        self._max_missed_pings = max_missed_pings

        # room -> set of connections bound to that room.
        self._rooms: dict[str, set[Connection]] = {}
        # ws identity -> connection, so a raw socket can be located on pong/close.
        self._by_ws: dict[int, Connection] = {}
        self._heartbeat_task: asyncio.Task[None] | None = None

    # -- introspection -------------------------------------------------------

    @property
    def connection_count(self) -> int:
        """Total number of currently tracked connections across all rooms."""
        return len(self._by_ws)

    def room_size(self, room: str) -> int:
        """Number of connections bound to ``room`` (0 if the room is empty)."""
        return len(self._rooms.get(room, ()))

    def rooms(self) -> list[str]:
        """List the non-empty room names currently registered."""
        return [room for room, conns in self._rooms.items() if conns]

    # -- lifecycle -----------------------------------------------------------

    async def connect(
        self, ws: WebSocketLike, room: str, user: Any = None
    ) -> Connection:
        """Bind an authenticated ``ws`` to ``room`` and track it (12A.1, 12A.2).

        Raises :class:`InvalidRoomError` for an unknown room shape and
        :class:`ConnectionError` if the manager is already at capacity. The
        caller is responsible for having accepted the WebSocket handshake and
        validated the JWT/role before calling this.
        """
        if not is_valid_room(room):
            raise InvalidRoomError(f"Unknown room: {room!r}")
        if self.connection_count >= self._max_connections:
            # At capacity: refuse rather than exceed the 10,000 bound (12A.1).
            raise ConnectionError("WebSocket connection capacity reached")

        connection = Connection(ws=ws, room=room, user=user)
        self._rooms.setdefault(room, set()).add(connection)
        self._by_ws[id(ws)] = connection
        logger.info(
            "ws.connect",
            extra={"room": room, "connectionId": connection.id,
                   "roomSize": self.room_size(room)},
        )
        return connection

    def disconnect(self, ws: WebSocketLike, room: str | None = None) -> None:
        """Remove ``ws`` from its room and release its bookkeeping resources.

        Idempotent: disconnecting an unknown or already-removed socket is a
        no-op. ``room`` is accepted for API symmetry but the manager resolves
        the actual room from its registry.
        """
        connection = self._by_ws.pop(id(ws), None)
        if connection is None:
            return
        self._remove_from_room(connection)
        logger.info(
            "ws.disconnect",
            extra={"room": connection.room, "connectionId": connection.id},
        )

    def _remove_from_room(self, connection: Connection) -> None:
        members = self._rooms.get(connection.room)
        if members is not None:
            members.discard(connection)
            if not members:
                # Drop empty rooms so room enumeration stays tight.
                del self._rooms[connection.room]

    async def _prune(self, connection: Connection, *, reason: str) -> None:
        """Close and fully remove ``connection`` (12A.4, 12A.7).

        Removal from the registry happens first so a concurrent broadcast can
        never re-touch a pruned connection; the close is best-effort.
        """
        self._by_ws.pop(id(connection.ws), None)
        self._remove_from_room(connection)
        try:
            await connection.ws.close()
        except Exception:  # noqa: BLE001 - close failures must not propagate
            logger.debug(
                "ws.prune.close_failed",
                extra={"connectionId": connection.id, "reason": reason},
            )
        logger.info(
            "ws.prune",
            extra={"room": connection.room, "connectionId": connection.id,
                   "reason": reason},
        )

    # -- delivery ------------------------------------------------------------

    async def send_personal(
        self, ws: WebSocketLike, message: WSMessage | dict[str, Any]
    ) -> bool:
        """Send ``message`` to a single connection; prune it on failure.

        Returns ``True`` if delivery succeeded, ``False`` if the connection was
        pruned because delivery failed (12A.7).
        """
        connection = self._by_ws.get(id(ws))
        payload = _as_payload(message)
        try:
            await ws.send_json(payload)
            return True
        except Exception as exc:  # noqa: BLE001 - a failed socket is pruned
            logger.warning(
                "ws.send_personal.failed",
                extra={"error": str(exc),
                       "connectionId": getattr(connection, "id", None)},
            )
            if connection is not None:
                await self._prune(connection, reason="send_failed")
            return False

    async def broadcast(
        self, room: str, message: WSMessage | dict[str, Any]
    ) -> int:
        """Deliver ``message`` to every connection bound to ``room`` (12A.5).

        Delivery is room-scoped: connections in other rooms never receive the
        event. Failures are handled per-connection — a connection whose delivery
        raises is pruned and delivery continues to the rest (12A.7). Returns the
        number of connections that received the message successfully.
        """
        members = self._rooms.get(room)
        if not members:
            return 0

        payload = _as_payload(message)
        # Snapshot so pruning during fan-out does not mutate the set in place.
        targets = list(members)
        results = await asyncio.gather(
            *(t.ws.send_json(payload) for t in targets),
            return_exceptions=True,
        )

        delivered = 0
        for connection, result in zip(targets, results):
            if isinstance(result, Exception):
                logger.warning(
                    "ws.broadcast.delivery_failed",
                    extra={"room": room, "connectionId": connection.id,
                           "error": str(result)},
                )
                await self._prune(connection, reason="broadcast_failed")
            else:
                delivered += 1
        return delivered

    # -- heartbeat -----------------------------------------------------------

    def record_pong(self, ws: WebSocketLike) -> None:
        """Record that ``ws`` answered the current heartbeat (12A.3).

        The routes layer calls this when it receives the client's pong frame. A
        pong clears the pending flag and resets the consecutive-miss counter.
        """
        connection = self._by_ws.get(id(ws))
        if connection is None:
            return
        connection.pong_pending = False
        connection.missed_pings = 0

    async def _send_pings(self) -> list[Connection]:
        """Send a ping to every live connection; prune sockets that fail.

        Returns the connections that were successfully pinged and are now
        awaiting a pong.
        """
        awaiting: list[Connection] = []
        ping = WSMessage(type=WSMessageType.PING.value, source="orchestrator").to_dict()
        for connection in list(self._by_ws.values()):
            connection.pong_pending = True
            try:
                await connection.ws.send_json(ping)
                awaiting.append(connection)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ws.heartbeat.ping_failed",
                    extra={"connectionId": connection.id, "error": str(exc)},
                )
                await self._prune(connection, reason="ping_failed")
        return awaiting

    async def _evaluate_pongs(self, awaiting: list[Connection]) -> None:
        """After the pong window, account misses and prune dead connections.

        A connection that still has ``pong_pending`` set missed this heartbeat;
        three consecutive misses trigger a prune (12A.4).
        """
        for connection in awaiting:
            # The connection may already have been pruned/removed concurrently.
            if id(connection.ws) not in self._by_ws:
                continue
            if connection.pong_pending:
                connection.missed_pings += 1
                if connection.missed_pings >= self._max_missed_pings:
                    await self._prune(connection, reason="missed_heartbeats")

    async def _heartbeat_cycle(self) -> None:
        """Run one ping/await-pong/prune cycle (used by the loop and by tests)."""
        awaiting = await self._send_pings()
        await asyncio.sleep(self._pong_timeout)
        await self._evaluate_pongs(awaiting)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._ping_interval)
            try:
                await self._heartbeat_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never let the loop die silently
                logger.exception("ws.heartbeat.cycle_error")

    def start_heartbeat(self) -> None:
        """Start the background heartbeat loop (idempotent).

        Intended to be called from the app's lifespan startup. Pruning of stale
        connections happens within the loop (Requirements 12A.3, 12A.4).
        """
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop_heartbeat(self) -> None:
        """Cancel the background heartbeat loop if running."""
        task = self._heartbeat_task
        if task is None:
            return
        self._heartbeat_task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def shutdown(self) -> None:
        """Stop the heartbeat and close every tracked connection."""
        await self.stop_heartbeat()
        for connection in list(self._by_ws.values()):
            await self._prune(connection, reason="shutdown")


# Process-wide manager instance used by the routes/services layers.
_default_manager = WebSocketManager()


def get_ws_manager() -> WebSocketManager:
    """Return the process-wide WebSocket manager."""
    return _default_manager
