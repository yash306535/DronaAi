"""Guardian agent: Stage-2 cloud confirmation of escalated frames (Requirement 7).

The Guardian owns identity & presence integrity through the two-stage proctoring
pipeline. Stage 1 screens locally in the browser at zero network cost; the
Guardian server component owns **Stage 2**, which is invoked *only* when a
Stage-1 local anomaly is escalated (Requirement 7.2) — never during normal
screening. Given a single captured frame plus the debounced local signal that
triggered the escalation, the Guardian:

1. **Confirm** (:meth:`GuardianAgent.confirm_escalation`) — validate the frame
   and make a *single* OpenAI Vision call (via the mockable
   :class:`~app.agents.vision.VisionClient`) bounded to ≤10s, returning a
   structured :class:`~app.agents.vision.VisionVerdict` (verdict label +
   confidence in ``[0,1]``) (Requirement 7.1).
2. **Act on the verdict** (:meth:`GuardianAgent.handle_escalation`):
   - *Confirmed* — anomalous with confidence ≥ ``confirm_threshold`` (default
     0.70): persist a confirmed anomaly and emit ``anomaly.detected`` with
     ``confirmed=true`` so the Herald can broadcast it (Requirement 7.4).
   - *Benign* — the local signal was a false positive: record the false positive
     and raise the session's local screening threshold by 0.05, capped at 0.95
     (Requirement 7.5). No confirmed anomaly is emitted.
   - *Vision unavailable / timeout* — record the anomaly as **unconfirmed** with
     severity ``warning`` and leave the session's threshold unchanged
     (Requirement 7.6).
3. **Discard the frame** — regardless of outcome, the raw escalated frame is
   discarded within the retention window (default: discard immediately after
   scoring, ≤60s) (Requirement 7.8).
4. **Announce** — emit an ``agent.message`` via the orchestrator for the
   dashboard feed (design "Component: Guardian Agent").

The :meth:`GuardianAgent.on_frame_escalated` handler wires the agent onto
``frame.escalated`` so an escalation published onto the bus drives the same
pipeline as a direct REST escalation.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from app.agents.prompts.guardian import GUARDIAN_PROMPT_VERSION, build_guardian_prompt
from app.agents.vision import (
    VERDICT_BENIGN,
    VisionClient,
    VisionError,
    VisionTimeoutError,
    VisionVerdict,
)
from app.core.events import Event, EventBus, EventType
from app.core.logging import get_logger
from app.models.enums import AlertSeverity, AnomalyCategory, SourceAgent
from app.models.orm import Anomaly
from app.repositories.anomaly import AnomalyRepository
from app.schemas.proctoring import LocalSignal

logger = get_logger("app.agents.guardian")

GUARDIAN_SOURCE = "guardian"

# Stage-2 confirmation parameters (Requirement 7 defaults).
CONFIRM_THRESHOLD = 0.70  # 7.4: anomalous + confidence ≥ 0.70 → confirmed
VISION_TIMEOUT_SECONDS = 10.0  # 7.1: verdict within 10s
THRESHOLD_STEP = 0.05  # 7.5: raise local threshold by 0.05 on a benign verdict
THRESHOLD_CAP = 0.95  # 7.5: capped at a maximum of 0.95
DEFAULT_LOCAL_THRESHOLD = 0.30  # Stage-1 default (design Stage-1 gaze default)
FRAME_RETENTION_SECONDS = 60.0  # 7.8: discard the raw frame within 60s

# The action returned to the client / recorded for an escalation outcome.
ACTION_ALERT_BROADCAST = "alert_broadcast"
ACTION_SUPPRESSED = "suppressed"


@dataclass(slots=True)
class EscalationOutcome:
    """The result of handling one escalation (mirrors the API response shape).

    ``confirmed`` is true only for an authoritative anomalous verdict at/above
    the confirm threshold (7.4). ``anomaly_id`` is ``None`` when no anomaly was
    persisted (a benign false positive). ``action`` is ``alert_broadcast`` on
    confirmation, otherwise ``suppressed``.
    """

    confirmed: bool
    category: str
    score: float
    reasons: list[str]
    action: str
    anomaly_id: str | None = None
    severity: AlertSeverity = AlertSeverity.INFO


@dataclass(slots=True)
class GuardianAgent:
    """Stage-2 confirmer: turn an escalated frame into an authoritative verdict.

    ``vision`` is the mockable vision backend (tests inject a stub; production
    uses OpenAI Vision). ``bus`` is the event bus the agent publishes onto.
    ``anomaly_repo_factory`` returns a fresh :class:`AnomalyRepository` (bound to
    a new DB session) per persistence so concurrent escalations never share a
    session. ``orchestrator`` is optional; when present the agent routes
    ``agent.message`` through it for the dashboard feed.
    """

    vision: VisionClient
    bus: EventBus | None
    anomaly_repo_factory: Callable[[], AnomalyRepository]
    orchestrator: object | None = None
    confirm_threshold: float = CONFIRM_THRESHOLD
    timeout_seconds: float = VISION_TIMEOUT_SECONDS
    default_threshold: float = DEFAULT_LOCAL_THRESHOLD
    retention_seconds: float = FRAME_RETENTION_SECONDS
    # Per-session adaptive local screening threshold (in-memory; 7.5).
    _thresholds: dict[str, float] = field(default_factory=dict, init=False)
    _false_positives: dict[str, int] = field(default_factory=dict, init=False)
    # Raw escalated frames held only for the duration of scoring (7.8).
    _frame_store: dict[str, str] = field(default_factory=dict, init=False)

    # -- threshold / false-positive accessors --------------------------------

    def get_threshold(self, session_id: str) -> float:
        """Return the session's current local screening threshold (7.5)."""
        return self._thresholds.get(session_id, self.default_threshold)

    def false_positive_count(self, session_id: str) -> int:
        """Return how many benign false positives were recorded for a session."""
        return self._false_positives.get(session_id, 0)

    @property
    def retained_frame_count(self) -> int:
        """Number of raw frames currently retained (0 once scoring completes)."""
        return len(self._frame_store)

    # -- frame retention (7.8) ----------------------------------------------

    def _store_frame(self, frame_b64: str) -> str:
        """Hold a raw frame for the duration of scoring; return its token."""
        token = uuid.uuid4().hex
        self._frame_store[token] = frame_b64
        return token

    def _discard_frame(self, token: str) -> None:
        """Discard a retained raw frame (7.8: within the retention window)."""
        self._frame_store.pop(token, None)

    # -- Stage 2: vision confirmation (7.1) ----------------------------------

    async def confirm_escalation(
        self,
        session_id: str,
        frame_b64: str,
        local_signal: LocalSignal,
        *,
        mime_type: str = "image/jpeg",
    ) -> VisionVerdict:
        """Submit ``frame_b64`` to Vision and return a structured verdict (7.1).

        Validates the frame, renders the tightly-scoped Guardian vision prompt
        from the triggering ``local_signal``, and makes a *single* vision call
        bounded to :attr:`timeout_seconds` (≤10s). A timeout is normalized to
        :class:`~app.agents.vision.VisionTimeoutError`; any other provider/
        transport failure surfaces as :class:`~app.agents.vision.VisionError`.
        """
        if not frame_b64 or not frame_b64.strip():
            raise VisionError("escalation frame is empty")

        prompt = build_guardian_prompt(
            kind=local_signal.kind,
            duration_ms=local_signal.duration_ms,
            confidence_local=local_signal.confidence_local,
        )
        try:
            verdict = await asyncio.wait_for(
                self.vision.analyze(
                    frame_b64,
                    prompt,
                    mime_type=mime_type,
                    timeout=self.timeout_seconds,
                ),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise VisionTimeoutError("OpenAI Vision did not respond in time") from exc
        logger.info(
            "guardian.vision.verdict",
            extra={
                "sessionId": session_id,
                "label": verdict.label,
                "confidence": verdict.confidence,
                "promptVersion": GUARDIAN_PROMPT_VERSION,
            },
        )
        return verdict

    # -- full escalation pipeline -------------------------------------------

    async def handle_escalation(
        self,
        session_id: str,
        frame_b64: str,
        local_signal: LocalSignal,
        *,
        mime_type: str = "image/jpeg",
    ) -> EscalationOutcome:
        """Confirm an escalation and act on the verdict (7.4, 7.5, 7.6, 7.8).

        Always discards the raw frame after scoring (7.8), regardless of
        outcome. Returns the :class:`EscalationOutcome` the REST layer renders.
        """
        token = self._store_frame(frame_b64)
        try:
            try:
                verdict = await self.confirm_escalation(
                    session_id, frame_b64, local_signal, mime_type=mime_type
                )
            except VisionError as exc:
                # Includes VisionTimeoutError: Vision unavailable / timed out (7.6).
                return await self._handle_unavailable(session_id, local_signal, exc)

            if verdict.is_confirmed(self.confirm_threshold):
                return await self._handle_confirmed(session_id, verdict, local_signal)
            if verdict.anomalous:
                # Anomalous but below the confirm threshold: not authoritative
                # enough to broadcast. Record as unconfirmed warning; the local
                # threshold is left unchanged (it was not a benign false positive).
                return await self._handle_unconfirmed(
                    session_id, verdict, local_signal
                )
            # Benign verdict: a local false positive (7.5).
            return await self._handle_benign(session_id, verdict, local_signal)
        finally:
            # Discard the raw frame within the retention window (7.8).
            self._discard_frame(token)

    # -- verdict branches ----------------------------------------------------

    async def _handle_confirmed(
        self, session_id: str, verdict: VisionVerdict, local_signal: LocalSignal
    ) -> EscalationOutcome:
        """Persist + emit a confirmed anomaly (7.4)."""
        category = self._coerce_category(verdict.label, local_signal.kind)
        score = _clamp(verdict.confidence)
        reasons = verdict.reasons or [f"Vision confirmed {verdict.label}"]
        anomaly = self._persist_anomaly(
            session_id=session_id,
            category=category,
            score=score,
            reasons=reasons,
            evidence={
                "stage": "vision",
                "verdict": verdict.label,
                "confidence": verdict.confidence,
                "localSignal": local_signal.model_dump(mode="json"),
                "raw": verdict.raw,
            },
            confirmed=True,
        )
        await self._emit_anomaly_detected(
            session_id,
            anomaly,
            confirmed=True,
            severity=AlertSeverity.DANGER,
        )
        await self._emit_agent_message(
            session_id,
            text=f"{verdict.label} confirmed for session {session_id}",
            level="danger",
        )
        return EscalationOutcome(
            confirmed=True,
            category=str(category),
            score=score,
            reasons=reasons,
            action=ACTION_ALERT_BROADCAST,
            anomaly_id=anomaly.id,
            severity=AlertSeverity.DANGER,
        )

    async def _handle_benign(
        self, session_id: str, verdict: VisionVerdict, local_signal: LocalSignal
    ) -> EscalationOutcome:
        """Record a false positive and raise the local threshold (7.5)."""
        self._false_positives[session_id] = self.false_positive_count(session_id) + 1
        new_threshold = self._raise_threshold(session_id)
        reasons = verdict.reasons or ["Vision found no violation in the frame"]
        await self._emit_agent_message(
            session_id,
            text=(
                f"Benign verdict for session {session_id}; local threshold raised "
                f"to {new_threshold:.2f}"
            ),
            level="info",
        )
        logger.info(
            "guardian.false_positive",
            extra={"sessionId": session_id, "newThreshold": new_threshold},
        )
        return EscalationOutcome(
            confirmed=False,
            category=VERDICT_BENIGN,
            score=_clamp(verdict.confidence),
            reasons=reasons,
            action=ACTION_SUPPRESSED,
            anomaly_id=None,
            severity=AlertSeverity.INFO,
        )

    async def _handle_unconfirmed(
        self, session_id: str, verdict: VisionVerdict, local_signal: LocalSignal
    ) -> EscalationOutcome:
        """Record an anomalous-but-low-confidence verdict as unconfirmed warning.

        The verdict was anomalous but below the confirm threshold, so it is not
        authoritative enough to broadcast (Requirement 7.4 only confirms at/above
        the threshold). It is recorded unconfirmed with severity ``warning`` and
        the local threshold is left unchanged.
        """
        category = self._coerce_category(verdict.label, local_signal.kind)
        score = _clamp(verdict.confidence)
        reasons = verdict.reasons or [f"Vision verdict {verdict.label} below confirm threshold"]
        anomaly = self._persist_anomaly(
            session_id=session_id,
            category=category,
            score=score,
            reasons=reasons,
            evidence={
                "stage": "vision",
                "verdict": verdict.label,
                "confidence": verdict.confidence,
                "unconfirmed": True,
                "localSignal": local_signal.model_dump(mode="json"),
            },
            confirmed=False,
        )
        await self._emit_anomaly_detected(
            session_id, anomaly, confirmed=False, severity=AlertSeverity.WARNING
        )
        return EscalationOutcome(
            confirmed=False,
            category=str(category),
            score=score,
            reasons=reasons,
            action=ACTION_SUPPRESSED,
            anomaly_id=anomaly.id,
            severity=AlertSeverity.WARNING,
        )

    async def _handle_unavailable(
        self, session_id: str, local_signal: LocalSignal, exc: VisionError
    ) -> EscalationOutcome:
        """Record an unconfirmed ``warning`` anomaly; leave threshold unchanged (7.6)."""
        category = self._coerce_category(local_signal.kind, local_signal.kind)
        score = _clamp(local_signal.confidence_local)
        reasons = [
            "Vision confirmation unavailable; recorded as unconfirmed warning",
            f"Local signal: {local_signal.kind}",
        ]
        anomaly = self._persist_anomaly(
            session_id=session_id,
            category=category,
            score=score,
            reasons=reasons,
            evidence={
                "stage": "vision",
                "visionAvailable": False,
                "error": type(exc).__name__,
                "localSignal": local_signal.model_dump(mode="json"),
            },
            confirmed=False,
        )
        await self._emit_anomaly_detected(
            session_id, anomaly, confirmed=False, severity=AlertSeverity.WARNING
        )
        await self._emit_agent_message(
            session_id,
            text=f"Vision unavailable for session {session_id}; recorded warning",
            level="warning",
        )
        logger.warning(
            "guardian.vision.unavailable",
            extra={"sessionId": session_id, "error": type(exc).__name__},
        )
        return EscalationOutcome(
            confirmed=False,
            category=str(category),
            score=score,
            reasons=reasons,
            action=ACTION_SUPPRESSED,
            anomaly_id=anomaly.id,
            severity=AlertSeverity.WARNING,
        )

    # -- event handler -------------------------------------------------------

    async def on_frame_escalated(self, event: Event) -> None:
        """Handle a ``frame.escalated`` event by running the Stage-2 pipeline.

        The payload carries the session id, the captured ``frame`` (base64), and
        the triggering ``localSignal``. A payload missing the frame or session is
        ignored with a warning (there is nothing to confirm).
        """
        payload = event.payload or {}
        session_id = payload.get("sessionId") or event.session_id
        frame_b64 = payload.get("frame")
        if not session_id or not frame_b64:
            logger.warning(
                "guardian.escalation.skipped_incomplete_event",
                extra={"eventId": event.id},
            )
            return
        local_signal = _coerce_local_signal(payload.get("localSignal"))
        mime_type = payload.get("mimeType", "image/jpeg")
        await self.handle_escalation(
            session_id, frame_b64, local_signal, mime_type=mime_type
        )

    # -- persistence / events ------------------------------------------------

    def _persist_anomaly(
        self,
        *,
        session_id: str,
        category: AnomalyCategory,
        score: float,
        reasons: list[str],
        evidence: dict,
        confirmed: bool,
    ) -> Anomaly:
        """Persist a Guardian-sourced anomaly via the repository."""
        repo = self.anomaly_repo_factory()
        anomaly = Anomaly(
            session_id=session_id,
            source_agent=SourceAgent.GUARDIAN,
            category=category,
            score=score,
            reasons=reasons,
            evidence=evidence,
            confirmed=confirmed,
        )
        return repo.add(anomaly)

    async def _emit_anomaly_detected(
        self,
        session_id: str,
        anomaly: Anomaly,
        *,
        confirmed: bool,
        severity: AlertSeverity,
    ) -> None:
        """Publish an ``anomaly.detected`` event (Herald broadcasts iff confirmed)."""
        if self.bus is None:
            return
        await self.bus.publish(
            Event(
                type=EventType.ANOMALY_DETECTED,
                payload={
                    "anomalyId": anomaly.id,
                    "sessionId": session_id,
                    "sourceAgent": GUARDIAN_SOURCE,
                    "category": str(anomaly.category),
                    "score": anomaly.score,
                    "reasons": list(anomaly.reasons),
                    "confirmed": confirmed,
                    "severity": severity.value,
                },
                source=GUARDIAN_SOURCE,
                session_id=session_id,
            )
        )

    async def _emit_agent_message(
        self, session_id: str, *, text: str, level: str
    ) -> None:
        """Emit an ``agent.message`` via the orchestrator (dashboard feed).

        Prefers the orchestrator's :meth:`emit_agent_message` so the message
        flows through the standard inter-agent path; falls back to publishing the
        ``agent.message`` event directly when no orchestrator is wired.
        """
        emit = getattr(self.orchestrator, "emit_agent_message", None)
        if callable(emit):
            await emit("Guardian", "Herald", text, level, session_id)
            return
        if self.bus is None:
            return
        await self.bus.publish(
            Event(
                type=EventType.AGENT_MESSAGE,
                payload={"to": "Herald", "text": text, "level": level},
                source="Guardian",
                session_id=session_id,
            )
        )

    # -- helpers -------------------------------------------------------------

    def _raise_threshold(self, session_id: str) -> float:
        """Raise the session's local threshold by 0.05, capped at 0.95 (7.5)."""
        current = self.get_threshold(session_id)
        new_threshold = min(THRESHOLD_CAP, round(current + THRESHOLD_STEP, 10))
        self._thresholds[session_id] = new_threshold
        return new_threshold

    @staticmethod
    def _coerce_category(label: str, fallback_kind: str) -> AnomalyCategory:
        """Map a verdict label / local kind to an :class:`AnomalyCategory`."""
        for candidate in (label, fallback_kind):
            try:
                return AnomalyCategory(candidate)
            except (ValueError, TypeError):
                continue
        return AnomalyCategory.GAZE_AWAY


def _clamp(value: float) -> float:
    """Clamp a score into the inclusive ``[0.0, 1.0]`` range (anomaly schema)."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, value))


def _coerce_local_signal(raw: object) -> LocalSignal:
    """Build a :class:`LocalSignal` from an event payload (tolerant of shape)."""
    if isinstance(raw, LocalSignal):
        return raw
    if isinstance(raw, dict):
        try:
            return LocalSignal.model_validate(raw)
        except Exception:  # noqa: BLE001 - fall back to a minimal signal
            kind = raw.get("kind", "unknown")
            return LocalSignal(kind=str(kind))
    return LocalSignal(kind="unknown")


def register_guardian(
    orchestrator,
    *,
    vision: VisionClient,
    bus: EventBus,
    anomaly_repo_factory: Callable[[], AnomalyRepository],
) -> GuardianAgent:
    """Build a :class:`GuardianAgent` and register it on the orchestrator.

    Wires :meth:`GuardianAgent.on_frame_escalated` to
    :data:`EventType.FRAME_ESCALATED` so an escalation published onto the bus is
    confirmed by the Guardian (Requirement 11.1). Returns the agent so the caller
    can hold a reference (e.g. the escalation endpoint reuses the same instance
    so adaptive thresholds persist across requests).
    """
    agent = GuardianAgent(
        vision=vision,
        bus=bus,
        anomaly_repo_factory=anomaly_repo_factory,
        orchestrator=orchestrator,
    )
    orchestrator.register_handler(EventType.FRAME_ESCALATED, agent.on_frame_escalated)
    return agent


__all__ = [
    "GuardianAgent",
    "EscalationOutcome",
    "register_guardian",
    "GUARDIAN_SOURCE",
    "CONFIRM_THRESHOLD",
    "THRESHOLD_STEP",
    "THRESHOLD_CAP",
    "DEFAULT_LOCAL_THRESHOLD",
    "VISION_TIMEOUT_SECONDS",
    "FRAME_RETENTION_SECONDS",
]
