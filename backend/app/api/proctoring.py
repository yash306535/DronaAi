"""Stage-2 proctoring escalation endpoint (Requirement 7.2, 7.3, 7.7).

`POST /proctoring/{session_id}/escalate` is the single seam where the
browser-side Stage-1 screener escalates a captured frame to the server for
authoritative OpenAI Vision confirmation. It is the **only** path that triggers
a Vision call, and it does so *only* for a Stage-1-triggered escalation
(Requirement 7.2) — there is no other route that reaches the vision provider, so
normal frame screening never incurs a cloud call.

The route enforces, in order and *before any Vision call*:

1. **Auth / ownership / active session (7.7)** — the caller must present a
   Student JWT (``require_role(STUDENT)``); the target session must exist, be
   owned by the requesting student, and be ``active``. Any failure returns an
   authorization error and never calls Vision.
2. **Payload guards (7.3)** — the frame must decode to ≤ the configured maximum
   (default 5 MB) and carry a MIME type of ``image/jpeg`` or ``image/png``. A
   violation returns 422, discards the payload, and never calls Vision.

Only after both pass does the route drive the Guardian: it publishes a
``frame.escalated`` event (so the Guardian's ``on_frame_escalated`` handler and
any dashboard listeners observe the escalation) and invokes
:meth:`GuardianAgent.handle_escalation` to obtain the verdict-derived outcome,
which it renders as the design's escalation response (``anomaly_id``,
``confirmed``, ``category``, ``score``, ``reasons``, ``action``). This route is
also rate-limited per IP and per session by the app's existing middleware
(``main.py`` matches ``/proctoring/{id}/escalate``).
"""

from __future__ import annotations

import base64
import binascii

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.agents.guardian import GuardianAgent
from app.agents.vision import get_default_vision_client
from app.api.deps import AuthUser, enforce_student_ownership, require_role
from app.api.runtime import get_event_bus
from app.core.db import get_db, get_session_factory
from app.core.errors import AuthError, ValidationError
from app.core.events import Event, EventType
from app.core.logging import get_logger
from app.models.enums import Role, SessionStatus
from app.repositories.anomaly import AnomalyRepository
from app.repositories.session import ExamSessionRepository
from app.schemas.proctoring import EscalationRequest, EscalationResponse

logger = get_logger("app.api.proctoring")

router = APIRouter(prefix="/proctoring", tags=["proctoring"])

# Requirement 7.3 payload guards.
MAX_FRAME_BYTES = 5 * 1024 * 1024  # default 5 MB
ALLOWED_MIME_TYPES = frozenset({"image/jpeg", "image/png"})

# Stable, machine-readable error codes (kept stable for clients).
SESSION_NOT_ACTIVE_CODE = "session_not_active"
SESSION_NOT_FOUND_CODE = "session_not_found"
FRAME_TOO_LARGE_CODE = "frame_too_large"
FRAME_BAD_MIME_CODE = "unsupported_frame_type"
FRAME_INVALID_CODE = "invalid_frame"


def _build_default_guardian() -> GuardianAgent:
    """Construct a process-default Guardian (production OpenAI Vision backend).

    Used when no Guardian was registered on the orchestrator (e.g. a minimal
    test app). A fresh :class:`AnomalyRepository` session is created per
    persistence so concurrent escalations never share a session.
    """

    def _anomaly_repo_factory() -> AnomalyRepository:
        return AnomalyRepository(get_session_factory()())

    return GuardianAgent(
        vision=get_default_vision_client(),
        bus=None,
        anomaly_repo_factory=_anomaly_repo_factory,
    )


def get_guardian(request: Request) -> GuardianAgent:
    """Return the process Guardian agent for confirming escalations.

    Prefers a Guardian registered on the wired orchestrator
    (``app.state.orchestrator``) so adaptive per-session thresholds persist
    across requests; falls back to an explicit ``app.state.guardian`` override
    (used by tests) and finally to a lazily-built default cached on app state.
    """
    guardian = getattr(request.app.state, "guardian", None)
    if guardian is not None:
        return guardian

    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is not None:
        agent = orchestrator.get_agent("Guardian")
        if agent is not None:
            request.app.state.guardian = agent
            return agent

    guardian = _build_default_guardian()
    request.app.state.guardian = guardian
    return guardian


def _split_frame(frame: str) -> tuple[str, str]:
    """Return ``(mime_type, raw_b64)`` from a frame field (7.3).

    Accepts either a bare base64 string (assumed ``image/jpeg``) or a
    ``data:<mime>;base64,<payload>`` data URL. Raises :class:`ValidationError`
    (422) on a malformed data URL so the payload is rejected before any Vision
    call.
    """
    if frame.startswith("data:"):
        header, _, payload = frame.partition(",")
        if not payload or ";base64" not in header:
            raise ValidationError(
                "Escalation frame is not a valid base64 image.",
                code=FRAME_INVALID_CODE,
            )
        mime_type = header[len("data:") :].split(";", 1)[0].strip().lower()
        return mime_type, payload
    # Bare base64 with no declared MIME: treat as JPEG (the Stage-1 capture).
    return "image/jpeg", frame


def _validate_frame(frame: str) -> tuple[str, str]:
    """Validate frame MIME + size and return ``(mime_type, raw_b64)`` (7.3).

    Rejects (422, no Vision call) a MIME other than jpeg/png, an undecodable
    payload, or a decoded size exceeding :data:`MAX_FRAME_BYTES`.
    """
    mime_type, raw_b64 = _split_frame(frame)
    if mime_type not in ALLOWED_MIME_TYPES:
        raise ValidationError(
            "Unsupported frame type; only image/jpeg and image/png are accepted.",
            code=FRAME_BAD_MIME_CODE,
        )
    try:
        decoded = base64.b64decode(raw_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError(
            "Escalation frame is not valid base64.", code=FRAME_INVALID_CODE
        ) from exc
    if len(decoded) > MAX_FRAME_BYTES:
        raise ValidationError(
            f"Escalation frame exceeds the maximum size of {MAX_FRAME_BYTES} bytes.",
            code=FRAME_TOO_LARGE_CODE,
        )
    return mime_type, raw_b64


@router.post("/{session_id}/escalate", response_model=EscalationResponse)
async def escalate_frame(
    session_id: str,
    body: EscalationRequest,
    user: AuthUser = Depends(require_role(Role.STUDENT)),
    db: Session = Depends(get_db),
    guardian: GuardianAgent = Depends(get_guardian),
    bus=Depends(get_event_bus),
) -> EscalationResponse:
    """Confirm a Stage-1 escalation via Vision and return the verdict (7.x).

    Performs the auth/ownership/active-session guards (7.7) and the payload
    size/MIME guards (7.3) — both before any Vision call — then drives the
    Guardian Stage-2 confirmation (7.1, 7.4, 7.5, 7.6, 7.8).
    """
    # --- 7.7: session must exist, be owned by the caller, and be active. ---
    session_row = ExamSessionRepository(db).get(session_id)
    if session_row is None:
        # No session: an authorization failure (the caller cannot escalate a
        # session that does not exist). No Vision call.
        raise AuthError(
            "Cannot escalate: session not found.",
            code=SESSION_NOT_FOUND_CODE,
            status_code=403,
        )
    # Student may only escalate their own session (403 before any Vision call).
    enforce_student_ownership(user, session_row.student_id)
    if session_row.status != SessionStatus.ACTIVE:
        raise AuthError(
            "Cannot escalate: the session is not active.",
            code=SESSION_NOT_ACTIVE_CODE,
            status_code=403,
        )

    # --- 7.3: validate payload (size + MIME) before any Vision call. ---
    mime_type, raw_b64 = _validate_frame(body.frame)

    # --- Drive the Guardian (Stage 2 invoked only here, 7.2). ---
    # Publish frame.escalated so the Guardian handler + dashboard observe it.
    if bus is not None:
        await bus.publish(
            Event(
                type=EventType.FRAME_ESCALATED,
                payload={
                    "sessionId": session_id,
                    "localSignal": body.local_signal.model_dump(mode="json"),
                    "mimeType": mime_type,
                    # The raw frame is intentionally omitted from the event
                    # payload; the route hands it directly to the Guardian below
                    # so it is not retained on the bus (7.8).
                },
                source="proctoring",
                session_id=session_id,
            )
        )

    outcome = await guardian.handle_escalation(
        session_id, raw_b64, body.local_signal, mime_type=mime_type
    )
    logger.info(
        "proctoring.escalation.scored",
        extra={
            "sessionId": session_id,
            "confirmed": outcome.confirmed,
            "category": outcome.category,
            "action": outcome.action,
        },
    )
    return EscalationResponse(
        anomaly_id=outcome.anomaly_id,
        confirmed=outcome.confirmed,
        category=outcome.category,
        score=outcome.score,
        reasons=outcome.reasons,
        action=outcome.action,
    )


__all__ = [
    "router",
    "get_guardian",
    "MAX_FRAME_BYTES",
    "ALLOWED_MIME_TYPES",
    "SESSION_NOT_ACTIVE_CODE",
    "SESSION_NOT_FOUND_CODE",
    "FRAME_TOO_LARGE_CODE",
    "FRAME_BAD_MIME_CODE",
    "FRAME_INVALID_CODE",
]
