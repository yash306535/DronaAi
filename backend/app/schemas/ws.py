"""WebSocket message envelope schema.

This is the API/validation mirror of the design's *WebSocket Event Schema*
(section "Message Envelope"). It is the canonical, validated representation of
every message carried over the live socket and is shared, in spirit, with the
frontend ``WSMessage<T>`` type in ``src/types``.

``app/core/ws.py`` defines a lightweight ``WSMessage`` *dataclass* used by the
connection manager's hot delivery path (it avoids per-message Pydantic
construction during fan-out). This Pydantic model is kept compatible with that
dataclass:

- identical field names (``type``, ``id``, ``ts``, ``source``, ``payload``) plus
  the optional ``session_id``,
- identical serialized shape — :meth:`WSMessage.to_envelope` emits the same
  camelCase ``sessionId`` key and omits it when absent, exactly like
  ``app.core.ws.WSMessage.to_dict``.

So a value validated/produced here can be handed to the manager (or serialized
for the client) without translation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WSMessageType(StrEnum):
    """The public set of message types carried over the live socket.

    Mirrors the ``WSMessageType`` union in the design's WebSocket Event Schema.
    The heartbeat ``ping``/``pong`` control frames are intentionally *not* part
    of this public schema; they are handled by the connection manager and the
    routes layer respectively.
    """

    AGENT_MESSAGE = "agent.message"
    AGENT_STATUS = "agent.status"
    ANOMALY_DETECTED = "anomaly.detected"
    ALERT_BROADCAST = "alert.broadcast"
    SESSION_UPDATE = "session.update"
    REPORT_READY = "report.ready"


class WSMessage(BaseModel):
    """The shared WebSocket message envelope (design "Message Envelope").

    Fields mirror the design schema:
    ``type``, ``id`` (uuid), ``ts`` (ISO-8601), ``sessionId`` (optional),
    ``source`` (emitting agent or ``"orchestrator"``), and ``payload``.

    The model accepts either ``session_id`` or its ``sessionId`` alias on input
    (``populate_by_name=True``) and serializes to the camelCase wire shape via
    :meth:`to_envelope`.
    """

    model_config = ConfigDict(populate_by_name=True)

    type: WSMessageType
    source: str = Field(min_length=1)
    payload: Any = None
    session_id: str | None = Field(default=None, alias="sessionId")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    ts: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_envelope(self) -> dict[str, Any]:
        """Render the JSON-serializable wire envelope (camelCase keys).

        Matches ``app.core.ws.WSMessage.to_dict``: ``sessionId`` is included
        only when ``session_id`` is set.
        """
        envelope: dict[str, Any] = {
            "type": self.type.value,
            "id": self.id,
            "ts": self.ts,
            "source": self.source,
            "payload": self.payload,
        }
        if self.session_id is not None:
            envelope["sessionId"] = self.session_id
        return envelope
