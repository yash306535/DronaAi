"""Student exam-session lifecycle service (Requirement 5).

Owns the use-case logic behind the session router so the API layer stays thin:

- :meth:`SessionService.start_session` (5.1, 5.2) — create an ``active`` session
  and return the student's own answer-key-free paper, rejecting a start when an
  ``active`` or ``submitted`` session already exists for that exam.
- :meth:`SessionService.get_session` (5 / 2.4, 2.5) — fetch session state; the
  router enforces ownership via :func:`enforce_student_ownership`.
- :meth:`SessionService.submit_answer` (5.4, 5.5) — persist/replace an answer
  while the session is ``active``, recording the *server*-measured time spent in
  whole seconds; reject when the session is not ``active`` leaving any prior
  answer unchanged.
- :meth:`SessionService.submit_exam` (5.6, 5.7) — transition ``active`` →
  ``submitted`` and emit exactly one ``exam.completed`` event.
- :meth:`SessionService.terminate_session` (5.10) — invigilator/admin force-end
  a session to ``terminated`` so later answer submissions are rejected.
- :meth:`SessionService.ingest_events` (5.8, 5.9, 14.3, 14.4) — persist a batch
  of 1-100 telemetry events with an authoritative UTC server timestamp,
  treating the client timestamp as untrusted, and publish ``session.event`` per
  event for Sentinel.

All persistence flows through the repositories (parameterized access + the
transient-fault retry policy); answer-key confidentiality is delegated to the
``StudentPaper`` schema and the serialization guard at the API boundary.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from app.core.errors import NotFoundError, ValidationError
from app.core.events import Event, EventBus, EventType
from app.core.logging import get_logger
from app.models.enums import SessionStatus
from app.models.orm import ExamSession, SessionEvent
from app.repositories.answer import AnswerRepository
from app.repositories.paper import PaperRepository
from app.repositories.session import ExamSessionRepository
from app.repositories.session_event import SessionEventRepository
from app.schemas.question import StudentPaper, StudentQuestion
from app.schemas.session import (
    AnswerRead,
    SessionEventBatch,
    SessionEventRead,
    SessionRead,
)

logger = get_logger("app.services.session")

# Stable, machine-readable domain error codes (kept stable for clients).
SESSION_EXISTS_CODE = "session_already_exists"
SESSION_NOT_ACTIVE_CODE = "session_not_active"
PAPER_NOT_FOUND_CODE = "paper_not_provisioned"
SESSION_NOT_FOUND_CODE = "session_not_found"

# Statuses that block starting a new session for the same exam (Req 5.2).
_BLOCKING_START_STATUSES = frozenset(
    {SessionStatus.ACTIVE, SessionStatus.SUBMITTED}
)

# The source name on events this service publishes (the session/REST layer).
EVENT_SOURCE = "session_service"

MS_PER_SECOND = 1000


def _utcnow() -> datetime:
    """Return the current time as an aware UTC datetime (authoritative clock)."""
    return datetime.now(timezone.utc)


class SessionService:
    """Use-case orchestration for the student exam-session lifecycle."""

    def __init__(
        self,
        *,
        sessions: ExamSessionRepository,
        papers: PaperRepository,
        answers: AnswerRepository,
        events: SessionEventRepository,
        event_bus: EventBus | None = None,
    ) -> None:
        self._sessions = sessions
        self._papers = papers
        self._answers = answers
        self._events = events
        self._bus = event_bus

    # -- start ---------------------------------------------------------------

    def start_session(self, exam_id: str, student_id: str) -> tuple[SessionRead, StudentPaper]:
        """Create or (re)open the student's session and return their own paper.

        Requirement 5.1: when the student has no session for the exam, create one
        with status ``active``. To keep the flow idempotent and demo-repeatable,
        an existing session for the exam is simply re-opened in place — a
        ``not_started`` / ``submitted`` / ``terminated`` session is moved back to
        ``active`` rather than rejected — so a single seeded account can take the
        exam again and again without re-seeding. No duplicate session is ever
        created.
        """
        existing = self._sessions.get_for_student(exam_id, student_id)
        if existing is not None:
            if existing.status != SessionStatus.ACTIVE:
                # Re-open any non-active session in place (resume / retake).
                self._sessions.set_status(existing.id, SessionStatus.ACTIVE)
                existing = self._sessions.get(existing.id) or existing
            logger.info(
                "session.resumed",
                extra={"sessionId": existing.id, "examId": exam_id},
            )
            return (
                SessionRead.model_validate(existing),
                self._build_student_paper(existing.paper_id, exam_id),
            )

        paper = self._papers.get_for_student(exam_id, student_id)
        if paper is None:
            # The student's paper has not been provisioned yet; nothing to start.
            raise NotFoundError(
                "No paper has been provisioned for this student and exam.",
                code=PAPER_NOT_FOUND_CODE,
            )

        session_row = ExamSession(
            exam_id=exam_id,
            student_id=student_id,
            paper_id=paper.id,
            status=SessionStatus.ACTIVE,
            started_at=_utcnow(),
        )
        created = self._sessions.add(session_row)
        logger.info(
            "session.started",
            extra={"sessionId": created.id, "examId": exam_id},
        )
        return SessionRead.model_validate(created), self._build_student_paper(paper.id, exam_id)

    def _build_student_paper(self, paper_id: str, exam_id: str) -> StudentPaper:
        """Assemble the student-facing paper (answer keys excluded by schema)."""
        questions = self._papers.list_questions(paper_id)
        return StudentPaper(
            id=paper_id,
            exam_id=exam_id,
            questions=[StudentQuestion.model_validate(q) for q in questions],
        )

    # -- read ----------------------------------------------------------------

    def get_session(self, session_id: str) -> ExamSession:
        """Return the session row or raise :class:`NotFoundError`.

        The router calls :func:`enforce_student_ownership` on the returned
        ``student_id`` so a student can only read their own session (2.4, 2.5).
        """
        session_row = self._sessions.get(session_id)
        if session_row is None:
            raise NotFoundError(
                "Session not found.", code=SESSION_NOT_FOUND_CODE
            )
        return session_row

    # -- answers -------------------------------------------------------------

    def submit_answer(
        self, session_row: ExamSession, question_id: str, response: str
    ) -> AnswerRead:
        """Persist/replace an answer while the session is ``active`` (5.4, 5.5).

        The time spent is *server-recorded* in whole seconds — measured from the
        session's ``started_at`` to now and floored to a whole second — so a
        client cannot forge timing. A submission against a non-``active`` session
        is rejected and any previously persisted answer is left unchanged (5.5).
        """
        if session_row.status != SessionStatus.ACTIVE:
            raise ValidationError(
                "The session is not active; answers cannot be submitted.",
                code=SESSION_NOT_ACTIVE_CODE,
            )

        time_spent_ms = self._server_time_spent_ms(session_row.started_at)
        answer = self._answers.upsert(
            session_id=session_row.id,
            question_id=question_id,
            response=response,
            time_spent_ms=time_spent_ms,
        )
        return AnswerRead.model_validate(answer)

    @staticmethod
    def _server_time_spent_ms(started_at: datetime | None) -> int:
        """Whole-second server-measured time spent, expressed in milliseconds.

        Floors the elapsed wall-clock seconds since ``started_at`` (never
        negative) so the persisted value carries no sub-second precision,
        satisfying "measured in whole seconds" (5.4).
        """
        if started_at is None:
            return 0
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        elapsed = (_utcnow() - started_at).total_seconds()
        whole_seconds = max(0, math.floor(elapsed))
        return whole_seconds * MS_PER_SECOND

    # -- submit --------------------------------------------------------------

    async def submit_exam(self, session_row: ExamSession) -> SessionRead:
        """Transition ``active`` → ``submitted`` and emit one completion event.

        Requirement 5.6/5.7: submitting an ``active`` session sets it to
        ``submitted`` and emits exactly one ``exam.completed`` event. A session
        that is not ``active`` is rejected (idempotent guard) so the event fires
        exactly once per transition.
        """
        if session_row.status != SessionStatus.ACTIVE:
            raise ValidationError(
                "Only an active session can be submitted.",
                code=SESSION_NOT_ACTIVE_CODE,
            )

        updated = self._sessions.set_status(session_row.id, SessionStatus.SUBMITTED)
        if updated is None:  # pragma: no cover - row existed moments ago
            raise NotFoundError(
                "Session not found.", code=SESSION_NOT_FOUND_CODE
            )
        # Record the submission time on the same row.
        updated.submitted_at = _utcnow()
        self._sessions.session.commit()
        self._sessions.session.refresh(updated)

        # Emit exactly one exam.completed event for this transition (5.7).
        await self._publish(
            Event(
                type=EventType.EXAM_COMPLETED,
                payload={"examId": updated.exam_id, "sessionId": updated.id},
                source=EVENT_SOURCE,
                session_id=updated.id,
            )
        )
        logger.info("session.submitted", extra={"sessionId": updated.id})
        return SessionRead.model_validate(updated)

    # -- terminate -----------------------------------------------------------

    def terminate_session(self, session_row: ExamSession) -> SessionRead:
        """Force-end a session to ``terminated`` (5.10).

        Invigilator/admin only (enforced by the router's role guard). Once
        terminated, later answer submissions are rejected by
        :meth:`submit_answer` because the status is no longer ``active``.
        """
        updated = self._sessions.set_status(session_row.id, SessionStatus.TERMINATED)
        if updated is None:  # pragma: no cover - row existed moments ago
            raise NotFoundError(
                "Session not found.", code=SESSION_NOT_FOUND_CODE
            )
        logger.info("session.terminated", extra={"sessionId": updated.id})
        return SessionRead.model_validate(updated)

    # -- events --------------------------------------------------------------

    async def ingest_events(
        self, session_row: ExamSession, batch: SessionEventBatch
    ) -> list[SessionEventRead]:
        """Persist a 1-100 event batch with authoritative server timestamps.

        Requirement 5.8/14.3/14.4: each event is stamped with an authoritative
        UTC ``server_ts`` (millisecond precision) recorded at receipt; the
        client-supplied timestamp is retained only as untrusted metadata. A
        batch larger than 100 is rejected upstream by the ``SessionEventBatch``
        schema (max 100) before this method runs, so none of its events are
        persisted (5.9). Each persisted event is published as a
        ``session.event`` for Sentinel.
        """
        # Single authoritative receipt instant for the whole batch, truncated to
        # millisecond precision (14.3). datetime carries microseconds; drop the
        # sub-millisecond component so the stored value is ms-precise.
        received_at = _utcnow()
        received_at = received_at.replace(
            microsecond=(received_at.microsecond // MS_PER_SECOND) * MS_PER_SECOND
        )

        rows = [
            SessionEvent(
                session_id=session_row.id,
                kind=event.kind,
                payload=event.payload,
                client_ts=event.client_ts,  # untrusted; metadata only (14.3)
                server_ts=received_at,  # authoritative (14.3/14.4)
            )
            for event in batch.events
        ]
        persisted = self._events.add_batch(rows)

        for row in persisted:
            await self._publish(
                Event(
                    type=EventType.SESSION_EVENT,
                    payload={
                        "sessionId": row.session_id,
                        "kind": str(row.kind),
                        "serverTs": row.server_ts.isoformat(),
                    },
                    source=EVENT_SOURCE,
                    session_id=row.session_id,
                )
            )

        return [SessionEventRead.model_validate(row) for row in persisted]

    # -- helpers -------------------------------------------------------------

    async def _publish(self, event: Event) -> None:
        """Publish ``event`` if an event bus is wired; otherwise a no-op.

        The bus is optional so the service is usable in tests that mount the
        router without the full app lifespan. Publishing is best-effort and a
        handler error never propagates to the request (the bus captures it).
        """
        if self._bus is None:
            return
        await self._bus.publish(event)


__all__ = [
    "SessionService",
    "SESSION_EXISTS_CODE",
    "SESSION_NOT_ACTIVE_CODE",
    "PAPER_NOT_FOUND_CODE",
    "SESSION_NOT_FOUND_CODE",
    "EVENT_SOURCE",
]
