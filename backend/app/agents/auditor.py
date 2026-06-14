"""Auditor agent: question fairness audit (Requirement 13, optional P3).

The Auditor is the optional sixth agent. It reviews the Architect's generated
papers for fairness *before* they reach students. It subscribes to
``paper.generated`` and, for each paper, reviews every question against three
fairness dimensions (Requirement 13.1):

- **cultural_bias** — region-, gender-, religion-, or socioeconomic-specific
  references not required by the subject.
- **difficulty_calibration** — deviation of assessed difficulty from the
  question's declared target difficulty.
- **language_clarity** — ambiguous phrasing, undefined terms, or multiple
  defensible correct answers.

The (mockable) language model assigns each dimension a ``pass``/``fail`` result
per question (Requirement 13.2); the Auditor then derives the overall verdict in
deterministic Python:

- **approved** — every question passes all three dimensions (Requirement 13.3).
  The paper's ``audit_status`` is set to ``approved`` and an ``audit.completed``
  event is emitted within 60s (Requirement 13.4).
- **needs_revision** — at least one question fails at least one dimension
  (Requirement 13.3). The paper's ``audit_status`` is set to ``flagged`` and,
  for each failing question, the question id, failing dimension(s), and an issue
  description per failing dimension are recorded (Requirement 13.5). An
  ``audit.completed`` event carrying those details is emitted.

If the review cannot be completed (the model is unavailable or returns
unparseable output after the configured retries), the paper's ``audit_status``
is left **unchanged** from its pre-review value, and an ``audit.failed`` event is
emitted carrying the reason the review could not be completed (Requirement 13.6).

The agent never blocks event delivery: :meth:`AuditorAgent.on_paper_generated`
schedules :meth:`AuditorAgent.audit_paper` as a background task so the LLM
round-trip runs off the event-bus delivery path.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.agents.llm import LLMClient, LLMError
from app.agents.prompts.auditor import (
    AUDITOR_PROMPT_VERSION,
    DIMENSIONS,
    build_auditor_prompt,
)
from app.core.events import Event, EventBus, EventType
from app.core.logging import get_logger
from app.models.enums import AuditStatus
from app.repositories.paper import PaperRepository

logger = get_logger("app.agents.auditor")

AUDITOR_SOURCE = "Auditor"

# Overall verdict labels (Requirement 13.3).
VERDICT_APPROVED = "approved"
VERDICT_NEEDS_REVISION = "needs_revision"

# Per-dimension result labels (Requirement 13.2).
RESULT_PASS = "pass"
RESULT_FAIL = "fail"


class AuditError(Exception):
    """A paper's fairness review could not be completed (triggers retry / 13.6).

    Carries a short, safe ``reason`` describing why the review failed (e.g. the
    model output was unparseable or a question was missing from the result). The
    reason never contains model output verbatim beyond a minimal descriptor.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(slots=True)
class QuestionAudit:
    """Per-question audit result: each dimension's pass/fail + failing issues."""

    question_id: str
    # dimension -> "pass" | "fail" (all three dimensions always present).
    dimensions: dict[str, str]
    # failing dimension -> one-line issue description (only failing dims, 13.5).
    issues: dict[str, str]

    @property
    def failing_dimensions(self) -> list[str]:
        """The dimensions this question failed (empty when it passes all)."""
        return [d for d, r in self.dimensions.items() if r == RESULT_FAIL]

    @property
    def passed(self) -> bool:
        """True when the question passes all three fairness dimensions (13.3)."""
        return not self.failing_dimensions


@dataclass(slots=True)
class FairnessAudit:
    """The completed review of a paper: overall verdict + per-question results."""

    verdict: str
    questions: list[QuestionAudit]

    @property
    def flagged(self) -> list[QuestionAudit]:
        """The failing questions recorded for a ``needs_revision`` verdict (13.5)."""
        return [q for q in self.questions if not q.passed]


@dataclass(slots=True)
class AuditConfig:
    """Tunable Auditor parameters (Requirement defaults)."""

    max_retries: int = 3  # retry unparseable/failed model output
    llm_timeout_seconds: float = 30.0  # bounded so the 60s budget holds (13.4)
    temperature: float = 0.2  # low: fairness review wants consistent judgments


@dataclass(slots=True)
class AuditorAgent:
    """Review generated papers for fairness and persist the verdict (Req 13).

    ``paper_repo_factory`` returns a fresh :class:`PaperRepository` (bound to a
    new DB session) per audit so background tasks never share a request's
    session. ``llm`` is the mockable model backend; ``bus`` is the event bus the
    agent publishes ``audit.completed`` / ``audit.failed`` onto.
    """

    llm: LLMClient
    bus: EventBus | None
    paper_repo_factory: Callable[[], PaperRepository]
    config: AuditConfig = field(default_factory=AuditConfig)
    _tasks: set[asyncio.Task] = field(default_factory=set, init=False)

    # -- event handler -------------------------------------------------------

    async def on_paper_generated(self, event: Event) -> None:
        """Handle a ``paper.generated`` event by scheduling an audit (13.1).

        Returns immediately after scheduling :meth:`audit_paper` as a background
        task so it never blocks event-bus delivery (the LLM round-trip runs off
        the delivery path). A payload missing the paper id is ignored with a
        warning.
        """
        payload = event.payload or {}
        paper_id = payload.get("paperId")
        if not paper_id:
            logger.warning(
                "auditor.paper_generated.skipped_incomplete_event",
                extra={"eventId": event.id},
            )
            return
        self._spawn(self.audit_paper(paper_id))

    async def wait_for_pending(self) -> None:
        """Await all in-flight background audits (used by tests/shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _spawn(self, coro) -> None:
        """Schedule ``coro`` as a tracked background task (kept from GC)."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- audit pipeline ------------------------------------------------------

    async def audit_paper(self, paper_id: str) -> FairnessAudit | None:
        """Review one paper's questions and persist/announce the verdict (Req 13).

        Loads the paper + questions, reviews each question across the three
        fairness dimensions via the LLM (13.1/13.2), derives the overall verdict
        (13.3), sets the paper's ``audit_status`` and emits ``audit.completed``
        (13.4/13.5). If the review cannot be completed, leaves the audit status
        unchanged and emits ``audit.failed`` (13.6). Returns the
        :class:`FairnessAudit` on success or ``None`` on failure.
        """
        loaded = self._load_paper(paper_id)
        if loaded is None:
            # No paper to audit: there is no status to leave unchanged, but the
            # review still "could not be completed" (13.6).
            await self._emit_audit_failed(
                paper_id, reason="paper_not_found", detail="paper_not_found"
            )
            return None
        subject, questions = loaded

        if not questions:
            # A paper with no questions trivially passes every dimension (13.3).
            audit = FairnessAudit(verdict=VERDICT_APPROVED, questions=[])
        else:
            try:
                audit = await self._review_with_retries(subject, questions)
            except (LLMError, AuditError) as exc:
                reason = exc.reason if isinstance(exc, AuditError) else "llm_unavailable"
                detail = str(exc)
                logger.warning(
                    "auditor.review.failed",
                    extra={"paperId": paper_id, "reason": reason},
                )
                # 13.6: leave audit_status unchanged, emit audit.failed.
                await self._emit_audit_failed(
                    paper_id, reason=reason, detail=detail
                )
                return None

        if audit.verdict == VERDICT_APPROVED:
            self._set_status(paper_id, AuditStatus.APPROVED)  # 13.4
            await self._emit_audit_completed(paper_id, audit)
        else:
            self._set_status(paper_id, AuditStatus.FLAGGED)  # 13.5
            await self._emit_audit_completed(paper_id, audit)
        return audit

    async def _review_with_retries(
        self, subject: str, questions: list[dict[str, Any]]
    ) -> FairnessAudit:
        """Review all questions, retrying on LLM/parse failure (13.1/13.2).

        Runs up to ``config.max_retries`` attempts. Each attempt calls the LLM,
        parses the per-question/per-dimension results, and derives the overall
        verdict. Raises :class:`LLMError` or :class:`AuditError` if every attempt
        fails (the caller turns this into ``audit.failed``, 13.6).
        """
        prompt = build_auditor_prompt(subject=subject, questions=questions)
        question_ids = [q["id"] for q in questions]
        attempts = max(1, self.config.max_retries)
        last_error: Exception = AuditError("unknown")
        for attempt in range(1, attempts + 1):
            try:
                raw = await asyncio.wait_for(
                    self.llm.complete(
                        prompt,
                        temperature=self.config.temperature,
                        timeout=self.config.llm_timeout_seconds,
                    ),
                    timeout=self.config.llm_timeout_seconds,
                )
                per_question = self._parse_audit(raw, question_ids)
                verdict = (
                    VERDICT_APPROVED
                    if all(q.passed for q in per_question)
                    else VERDICT_NEEDS_REVISION
                )
                logger.info(
                    "auditor.review.ok",
                    extra={
                        "attempt": attempt,
                        "promptVersion": AUDITOR_PROMPT_VERSION,
                        "verdict": verdict,
                        "questions": len(per_question),
                    },
                )
                return FairnessAudit(verdict=verdict, questions=per_question)
            except AuditError as exc:
                last_error = exc
                logger.warning(
                    "auditor.review.invalid",
                    extra={"attempt": attempt, "reason": exc.reason},
                )
                continue  # discard partial result, retry
            except asyncio.TimeoutError as exc:
                raise LLMError("audit review timed out") from exc
            except LLMError as exc:
                last_error = exc
                logger.warning(
                    "auditor.review.attempt_failed",
                    extra={"attempt": attempt, "error": str(exc)},
                )
                continue
        # All attempts exhausted (13.6).
        if isinstance(last_error, LLMError):
            raise last_error
        raise last_error

    # -- parsing -------------------------------------------------------------

    @staticmethod
    def _parse_audit(raw: str, question_ids: list[str]) -> list[QuestionAudit]:
        """Parse the model JSON into per-question audits (13.2/13.5).

        Raises :class:`AuditError` (so the caller retries) when the output is
        malformed, a question is missing, a dimension is absent, or a dimension
        result is not ``pass``/``fail``.
        """
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise AuditError("invalid_json") from exc

        questions = data.get("questions") if isinstance(data, dict) else None
        if not isinstance(questions, dict):
            raise AuditError("missing_questions_object")

        results: list[QuestionAudit] = []
        for qid in question_ids:
            entry = questions.get(qid)
            if not isinstance(entry, dict):
                raise AuditError("missing_question_result")

            dimensions: dict[str, str] = {}
            issues: dict[str, str] = {}
            for dim in DIMENSIONS:
                dim_entry = entry.get(dim)
                if not isinstance(dim_entry, dict):
                    raise AuditError("missing_dimension")
                result = dim_entry.get("result")
                if result not in (RESULT_PASS, RESULT_FAIL):
                    raise AuditError("invalid_dimension_result")
                dimensions[dim] = result
                if result == RESULT_FAIL:
                    # 13.5: record a description of each detected issue.
                    issue = str(dim_entry.get("issue", "")).strip()
                    issues[dim] = issue or "unspecified issue"
            results.append(
                QuestionAudit(question_id=qid, dimensions=dimensions, issues=issues)
            )
        return results

    # -- persistence ---------------------------------------------------------

    def _load_paper(
        self, paper_id: str
    ) -> tuple[str, list[dict[str, Any]]] | None:
        """Load ``(subject, questions)`` for ``paper_id`` via PaperRepository.

        Returns ``None`` when the paper does not exist. Each question dict
        carries only the fields the fairness review needs (id, prompt, topic,
        declared difficulty, options) — never the answer key.
        """
        repo = self.paper_repo_factory()
        try:
            paper = repo.get(paper_id)
            if paper is None:
                return None
            # Subject is best-effort context for the prompt; the relationship is
            # loaded within the repo's still-open session.
            subject = ""
            exam = getattr(paper, "exam", None)
            if exam is not None:
                subject = getattr(exam, "subject", "") or ""
            rows = repo.list_questions(paper_id)
            questions = [
                {
                    "id": q.id,
                    "prompt": q.prompt,
                    "topic": q.topic,
                    "difficulty": q.difficulty,
                    "type": str(q.type),
                    "options": q.options,
                }
                for q in rows
            ]
            return subject, questions
        finally:
            repo.session.close()

    def _set_status(self, paper_id: str, status: AuditStatus) -> None:
        """Persist the paper's ``audit_status`` via PaperRepository (13.4/13.5).

        Uses the repository's existing ``get`` plus a session commit; the
        attribute write goes through the ORM so the access stays parameterized.
        """
        repo = self.paper_repo_factory()
        try:
            paper = repo.get(paper_id)
            if paper is None:
                # The paper vanished between review and persist; nothing to set.
                logger.warning(
                    "auditor.set_status.paper_not_found",
                    extra={"paperId": paper_id},
                )
                return
            paper.audit_status = status
            repo.session.add(paper)
            repo.session.commit()
            logger.info(
                "auditor.audit_status_set",
                extra={"paperId": paper_id, "status": str(status)},
            )
        finally:
            repo.session.close()

    # -- events --------------------------------------------------------------

    async def _emit_audit_completed(
        self, paper_id: str, audit: FairnessAudit
    ) -> None:
        """Emit an ``audit.completed`` event with the verdict + flags (13.4/13.5)."""
        if self.bus is None:
            return
        flagged = [
            {
                "questionId": q.question_id,
                "failingDimensions": q.failing_dimensions,
                "issues": q.issues,
            }
            for q in audit.flagged
        ]
        await self.bus.publish(
            Event(
                type=EventType.AUDIT_COMPLETED,
                payload={
                    "paperId": paper_id,
                    "verdict": audit.verdict,
                    "auditStatus": (
                        AuditStatus.APPROVED
                        if audit.verdict == VERDICT_APPROVED
                        else AuditStatus.FLAGGED
                    ).value,
                    "questionCount": len(audit.questions),
                    "flagged": flagged,
                },
                source=AUDITOR_SOURCE,
            )
        )
        logger.info(
            "auditor.audit_completed",
            extra={
                "paperId": paper_id,
                "verdict": audit.verdict,
                "flaggedCount": len(flagged),
            },
        )

    async def _emit_audit_failed(
        self, paper_id: str, *, reason: str, detail: str
    ) -> None:
        """Emit an ``audit.failed`` event naming the paper + reason (13.6)."""
        if self.bus is None:
            return
        await self.bus.publish(
            Event(
                type=EventType.AUDIT_FAILED,
                payload={
                    "paperId": paper_id,
                    "reason": reason,
                    "detail": detail,
                },
                source=AUDITOR_SOURCE,
            )
        )
        logger.warning(
            "auditor.audit_failed",
            extra={"paperId": paper_id, "reason": reason},
        )


def register_auditor(
    orchestrator,
    *,
    llm: LLMClient,
    bus: EventBus,
    paper_repo_factory: Callable[[], PaperRepository],
    config: AuditConfig | None = None,
) -> AuditorAgent:
    """Build an :class:`AuditorAgent` and register it on the orchestrator.

    Wires the agent's :meth:`on_paper_generated` handler to
    :data:`EventType.PAPER_GENERATED` so it is subscribed before any event is
    published (Requirement 11.1), following the Architect/Guardian/Herald/
    Sentinel/Analyst registration pattern. Returns the agent so the caller can
    hold a reference (e.g. to await pending audits on shutdown).
    """
    agent = AuditorAgent(
        llm=llm,
        bus=bus,
        paper_repo_factory=paper_repo_factory,
        config=config or AuditConfig(),
    )
    orchestrator.register_handler(
        EventType.PAPER_GENERATED, agent.on_paper_generated
    )
    return agent


__all__ = [
    "AuditorAgent",
    "AuditConfig",
    "AuditError",
    "FairnessAudit",
    "QuestionAudit",
    "AUDITOR_SOURCE",
    "VERDICT_APPROVED",
    "VERDICT_NEEDS_REVISION",
    "RESULT_PASS",
    "RESULT_FAIL",
    "register_auditor",
]
