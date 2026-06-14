"""Authenticated WebSocket endpoints.

Exposes the three live rooms from the design's "WebSocket Event Schema":

- ``GET /ws/dashboard`` — admins; bound to the ``dashboard`` room.
- ``GET /ws/invigilator/{exam_id}`` — invigilators (or admins); bound to
  ``invigilator:{exam_id}``.
- ``GET /ws/session/{session_id}`` — the owning student (or an invigilator /
  admin); bound to ``session:{session_id}``.

Every connection presents its access token as a ``?token=<jwt>`` query
parameter (browsers cannot set ``Authorization`` headers on a WebSocket
handshake, so the shared HTTP Bearer dependency cannot be reused). The token
and the caller's role are validated *before* the socket is bound to any room:

- Requirement 2.6: the JWT and role are validated before binding.
- Requirement 2.7 / 12.2: a missing, malformed, or expired token closes the
  connection without binding it to any room.
- Requirement 2.8 / 12A.6: a role not authorized for the requested room, or an
  unknown/invalid room identifier, closes the connection without binding it.
- Requirement 12.1: an admin with a valid token is bound to the ``dashboard``
  room and begins receiving streamed events.

On success the socket is accepted and registered with the
:class:`~app.core.ws.WebSocketManager`. Inbound frames are then read in a loop:
application-level ``pong`` frames refresh the heartbeat liveness via
``record_pong`` and any other frame is ignored. ``disconnect`` cleans the
connection out of its room on ``WebSocketDisconnect``.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.db import get_session_factory
from app.core.logging import get_logger
from app.core.security import TokenError, decode_access_token
from app.core.ws import (
    DASHBOARD_ROOM,
    INVIGILATOR_ROOM_PREFIX,
    SESSION_ROOM_PREFIX,
    Connection,
    InvalidRoomError,
    WebSocketManager,
    get_ws_manager,
    is_valid_room,
)
from app.models.enums import Role
from app.repositories.session import ExamSessionRepository

logger = get_logger(__name__)

router = APIRouter(tags=["websocket"])

# WebSocket close codes. 1008 (policy violation) is the closest standard code
# for an auth/authorization rejection; we use it for every "rejected before
# binding" path so the client can distinguish a refusal from a normal close.
WS_CLOSE_POLICY_VIOLATION = 1008
# 1013 (try again later) signals a transient capacity refusal.
WS_CLOSE_TRY_LATER = 1013


class _AuthUser:
    """Opaque per-connection identity passed to the manager for context.

    The manager treats this as opaque; it only exists so downstream tooling can
    attribute a connection to a user/role without re-decoding the token.
    """

    __slots__ = ("id", "role")

    def __init__(self, user_id: str, role: str) -> None:
        self.id = user_id
        self.role = role


def _extract_claims(token: str | None) -> dict[str, Any] | None:
    """Validate ``token`` and return its claims, or ``None`` if invalid (2.7).

    A missing token, or any token that fails signature/expiry/type validation,
    yields ``None`` so the caller can close the connection before binding it.
    """
    if not token:
        return None
    try:
        return decode_access_token(token)
    except TokenError:
        return None


def _role_of(claims: dict[str, Any]) -> str | None:
    """Return the role claim if it names one of the three known roles."""
    role = claims.get("role")
    if role in (Role.ADMIN.value, Role.INVIGILATOR.value, Role.STUDENT.value):
        return role
    return None


def _student_owns_session(session_id: str, user_id: str) -> bool:
    """Return whether ``user_id`` is the student bound to ``session_id``.

    Uses a short-lived DB session via the repository layer (parameterized
    access). A missing session yields ``False`` so a student can never bind to a
    session that does not exist or is not theirs.
    """
    factory = get_session_factory()
    db = factory()
    try:
        row = ExamSessionRepository(db).get(session_id)
        return row is not None and row.student_id == user_id
    finally:
        db.close()


async def _reject(ws: WebSocket, *, code: int = WS_CLOSE_POLICY_VIOLATION) -> None:
    """Close ``ws`` without accepting/binding it (2.7, 2.8, 12.2, 12A.6)."""
    await ws.close(code=code)


async def _bind_and_serve(
    websocket: WebSocket,
    room: str,
    user: _AuthUser,
    manager: WebSocketManager,
) -> None:
    """Accept the handshake, register the connection, and pump inbound frames.

    The socket is only accepted *after* auth + room validation have passed, so
    a rejected connection is never bound to a room (Requirement 2.6).
    """
    # Defensive: the manager also validates the room shape (12A.6). Validating
    # here lets us reject before accepting the handshake.
    if not is_valid_room(room):
        await _reject(websocket)
        return

    await websocket.accept()
    try:
        connection: Connection = await manager.connect(websocket, room, user)
    except InvalidRoomError:
        # Room shape was rejected by the manager (belt-and-suspenders with the
        # is_valid_room check above): close without leaving any binding.
        await _reject(websocket)
        return
    except ConnectionError:
        # At capacity (Requirement 12A.1): refuse with a try-again-later close.
        await _reject(websocket, code=WS_CLOSE_TRY_LATER)
        return

    logger.info(
        "ws.route.bound",
        extra={"room": room, "connectionId": connection.id, "userId": user.id},
    )
    try:
        while True:
            raw = await websocket.receive_text()
            _handle_inbound(raw, websocket, manager)
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket, room)


def _handle_inbound(
    raw: str, websocket: WebSocket, manager: WebSocketManager
) -> None:
    """Process one inbound text frame.

    The only frame we act on is an application-level ``pong`` answering the
    manager's heartbeat ping (Requirement 12A.3); everything else is ignored.
    Malformed JSON is tolerated and discarded.
    """
    try:
        message = json.loads(raw)
    except (ValueError, TypeError):
        return
    if isinstance(message, dict) and message.get("type") == "pong":
        manager.record_pong(websocket)


# --- Routes -----------------------------------------------------------------


@router.websocket("/ws/dashboard")
async def ws_dashboard(websocket: WebSocket) -> None:
    """Admin dashboard stream (Requirements 2.6, 2.7, 2.8, 12.1, 12.2)."""
    claims = _extract_claims(websocket.query_params.get("token"))
    if claims is None:
        await _reject(websocket)
        return
    role = _role_of(claims)
    if role != Role.ADMIN.value:
        # Only admins may observe the all-sessions dashboard room (2.8).
        await _reject(websocket)
        return

    user = _AuthUser(claims.get("sub", ""), role)
    await _bind_and_serve(websocket, DASHBOARD_ROOM, user, get_ws_manager())


@router.websocket("/ws/invigilator/{exam_id}")
async def ws_invigilator(websocket: WebSocket, exam_id: str) -> None:
    """Per-exam invigilator stream (Requirements 2.6, 2.7, 2.8, 12A.6)."""
    claims = _extract_claims(websocket.query_params.get("token"))
    if claims is None:
        await _reject(websocket)
        return
    role = _role_of(claims)
    if role not in (Role.INVIGILATOR.value, Role.ADMIN.value):
        await _reject(websocket)
        return

    room = f"{INVIGILATOR_ROOM_PREFIX}{exam_id}"
    user = _AuthUser(claims.get("sub", ""), role)
    await _bind_and_serve(websocket, room, user, get_ws_manager())


@router.websocket("/ws/session/{session_id}")
async def ws_session(websocket: WebSocket, session_id: str) -> None:
    """Per-session control stream (Requirements 2.6, 2.7, 2.8, 12A.6).

    The owning student may connect to their own session; invigilators and
    admins may observe any session. A student requesting a session that is not
    theirs is rejected before binding (Requirement 2.5 semantics for the WS
    surface).
    """
    claims = _extract_claims(websocket.query_params.get("token"))
    if claims is None:
        await _reject(websocket)
        return
    role = _role_of(claims)
    if role is None:
        await _reject(websocket)
        return

    if role == Role.STUDENT.value:
        if not _student_owns_session(session_id, claims.get("sub", "")):
            await _reject(websocket)
            return
    elif role not in (Role.INVIGILATOR.value, Role.ADMIN.value):
        await _reject(websocket)
        return

    room = f"{SESSION_ROOM_PREFIX}{session_id}"
    user = _AuthUser(claims.get("sub", ""), role)
    await _bind_and_serve(websocket, room, user, get_ws_manager())
