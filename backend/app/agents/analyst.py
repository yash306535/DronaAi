"""Analyst agent: post-exam analytics & intelligence (Requirement 10).

The Analyst turns a completed exam into dashboard-ready intelligence. It
subscribes to ``exam.completed`` and, for the exam, aggregates every completed
session's results into an :class:`~app.models.orm.ExamAnalytics` record:

1. **Score summary** (Requirement 10.2) — a score distribution grouped into
   fixed bands that cover the full ``0..100`` scoring range, the arithmetic mean
   score rounded to 2 decimal places, and a total count of flagged anomalies.
2. **Difficulty heatmap** (Requirement 10.3) — each topic mapped to an accuracy
   value (``0..100`` percent of correct answers) and a difficulty value
   (``0..100`` percent, derived from the questions' ``0..1`` difficulty).
3. **Per-student improvement suggestions** (Requirement 10.4) — at least one
   actionable suggestion for every student who completed the exam, derived from
   that student's own results.

The aggregation in (1) and (2) — and the per-student score/topic-accuracy that
feeds (3) — is **deterministic Python** (no model involved). The *only* part
that uses the (mockable) language model is the per-student narrative in (3).

Timing & robustness (Requirements 10.1, 10.5-10.7):

- **10.1/10.5** When all sections are produced the Analyst persists the report
  and emits a ``report.ready`` event identifying the exam, well within 120s
  (the aggregation is O(answers) in-process; the single LLM round-trip is bounded
  by ``config.llm_timeout_seconds``).
- **10.6** If the LLM call fails after the configured retries are exhausted, the
  Analyst persists a **partial** report — the deterministic sections are present
  and the suggestions section is marked ``pending`` — and schedules a retry of
  the failed section within 300s. No ``report.ready`` is emitted yet (10.5
  requires all sections).
- **10.7** When the scheduled retry completes the suggestions, the Analyst
  updates the persisted report and emits ``report.ready``.

The agent never blocks event delivery: :meth:`AnalystAgent.on_exam_completed`
schedules :meth:`AnalystAgent.build_report` as a background task so the DB
aggregation and LLM round-trip run off the event-bus delivery path.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.agents.llm import LLMClient, LLMError
from app.agents.prompts.analyst import (
    ANALYST_PROMPT_VERSION,
    build_analyst_prompt,
)
from app.core.events import Event, EventBus, EventType
from app.core.logging import get_logger
from app.models.enums import SessionStatus
from app.repositories.analytics import ExamAnalyticsRepository
from app.repositories.anomaly import AnomalyRepository
from app.repositories.answer import AnswerRepository
from app.repositories.exam import ExamRepository
from app.repositories.paper import PaperRepository
from app.repositories.session import ExamSessionRepository

logger = get_logger("app.agents.analyst")

ANALYST_SOURCE = "Analyst"

# A section's completion status, persisted alongside the section so a partial
# report is unambiguous to consumers (Requirement 10.6).
STATUS_READY = "ready"
STATUS_PENDING = "pending"

# Score bands are fixed and cover the full 0..100 scoring range (Requirement
# 10.2). Ten 10-wide bands; the top band ``90-100`` is inclusive of 100.
BAND_WIDTH = 10


def score_bands() -> list[str]:
    """Return the ordered fixed score-band labels covering ``0..100`` (10.2)."""
    return [f"{lo}-{lo + BAND_WIDTH}" for lo in range(0, 100, BAND_WIDTH)]


def band_for(score: float) -> str:
    """Return the fixed band label a ``0..100`` ``score`` falls into (10.2)."""
    if score >= 100:
        return f"{100 - BAND_WIDTH}-100"
    if score < 0:
        return f"0-{BAND_WIDTH}"
    index = int(score // BAND_WIDTH)
    lo = index * BAND_WIDTH
    return f"{lo}-{lo + BAND_WIDTH}"


def build_distribution(scores: list[float]) -> dict[str, int]:
    """Group ``scores`` (0..100) into the fixed bands; every band is present (10.2)."""
    distribution = {band: 0 for band in score_bands()}
    for score in scores:
        distribution[band_for(score)] += 1
    return distribution


def mean_score(scores: list[float]) -> float:
    """Return the arithmetic mean of ``scores`` rounded to 2 decimals (10.2)."""
    if not scores:
        return 0.0
    return round(sum(scores) / len(scores), 2)


def is_correct(response: str | None, answer_key: str) -> bool:
    """Deterministic grading: a response matches its answer key (normalized).

    Comparison is whitespace-trimmed and case-insensitive so trivial formatting
    differences do not mismark an otherwise-correct answer. A missing response is
    never correct.
    """
    if response is None:
        return False
    return response.strip().casefold() == (answer_key or "").strip().casefold()


@dataclass(slots=True)
class _Aggregate:
    """Deterministic aggregation result feeding the report sections."""

    subject: str
    scores: list[float]
    summary: dict[str, Any]
    difficulty_heatmap: dict[str, Any]
    # student_id -> {"score": float, "topicAccuracy": {topic: pct}}
    student_results: dict[str, dict[str, Any]]


@dataclass(slots=True)
class AnalystConfig:
    """Tunable Analyst parameters (Requirement defaults)."""

    max_retries: int = 3  # 10.6: LLM call fails after 3 attempts
    llm_timeout_seconds: float = 30.0  # bounded so the 120s budget holds (10.1)
    retry_delay_seconds: float = 300.0  # 10.6: schedule retry within 300s
    temperature: float = 0.4


@dataclass(slots=True)
class AnalystAgent:
    """Aggregate completed-exam results and produce analytics (Requirement 10).

    ``session_factory`` returns a fresh SQLAlchemy :class:`Session` per report
    build so background tasks never share a request's session. ``llm`` is the
    mockable model backend used only for the per-student suggestion narrative;
    ``bus`` is the event bus the agent publishes ``report.ready`` onto.
    """

    llm: LLMClient
    bus: EventBus | None
    session_factory: Callable[[], Session]
    config: AnalystConfig = field(default_factory=AnalystConfig)
    _tasks: set[asyncio.Task] = field(default_factory=set, init=False)

    # -- event handler -------------------------------------------------------

    async def on_exam_completed(self, event: Event) -> None:
        """Handle an ``exam.completed`` event by scheduling report generation (10.1).

        Returns immediately after scheduling :meth:`build_report` as a background
        task so it never blocks event-bus delivery (the DB aggregation + LLM
        round-trip run off the delivery path). A payload missing the exam id is
        ignored with a warning.
        """
        payload = event.payload or {}
        exam_id = payload.get("examId")
        if not exam_id:
            logger.warning(
                "analyst.completed.skipped_incomplete_event",
                extra={"eventId": event.id},
            )
            return
        self._spawn(self.build_report(exam_id))

    async def wait_for_pending(self) -> None:
        """Await all in-flight background tasks (used by tests/shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _spawn(self, coro) -> None:
        """Schedule ``coro`` as a tracked background task (kept from GC)."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- report pipeline -----------------------------------------------------

    async def build_report(self, exam_id: str) -> None:
        """Aggregate results and produce the analytics report for ``exam_id``.

        Deterministic sections (summary, difficulty heatmap, per-student
        score/topic-accuracy) are computed first (10.2, 10.3). The per-student
        suggestions are then generated via the LLM with retries (10.4). On
        success the full report is persisted and ``report.ready`` is emitted
        (10.5). On LLM failure a partial report is persisted with the suggestions
        section marked pending and a retry is scheduled (10.6).
        """
        aggregate = self._aggregate(exam_id)

        try:
            suggestions = await self._suggestions_with_retries(
                aggregate.subject, aggregate.student_results
            )
        except LLMError as exc:
            logger.warning(
                "analyst.suggestions.failed_partial",
                extra={"examId": exam_id, "error": str(exc)},
            )
            per_student = self._assemble_per_student(
                aggregate.student_results, suggestions=None, status=STATUS_PENDING
            )
            self._persist(exam_id, aggregate, per_student)
            self._schedule_retry(exam_id)
            return

        per_student = self._assemble_per_student(
            aggregate.student_results, suggestions=suggestions, status=STATUS_READY
        )
        analytics_id = self._persist(exam_id, aggregate, per_student)
        await self._emit_report_ready(exam_id, analytics_id)

    async def _complete_suggestions(self, exam_id: str) -> None:
        """Scheduled retry: complete the pending suggestions section (10.7).

        Re-runs the deterministic aggregation (cheap, stable) and retries the
        LLM suggestions. On success the persisted report is updated and
        ``report.ready`` is emitted (10.7). On continued failure the suggestions
        stay pending and another retry is scheduled.
        """
        aggregate = self._aggregate(exam_id)
        try:
            suggestions = await self._suggestions_with_retries(
                aggregate.subject, aggregate.student_results
            )
        except LLMError as exc:
            logger.warning(
                "analyst.suggestions.retry_failed",
                extra={"examId": exam_id, "error": str(exc)},
            )
            self._schedule_retry(exam_id)
            return

        per_student = self._assemble_per_student(
            aggregate.student_results, suggestions=suggestions, status=STATUS_READY
        )
        analytics_id = self._persist(exam_id, aggregate, per_student)
        await self._emit_report_ready(exam_id, analytics_id)
        logger.info("analyst.report.completed_on_retry", extra={"examId": exam_id})

    def _schedule_retry(self, exam_id: str) -> None:
        """Schedule a retry of the failed suggestions within 300s (10.6)."""

        async def _retry() -> None:
            await asyncio.sleep(self.config.retry_delay_seconds)
            await self._complete_suggestions(exam_id)

        self._spawn(_retry())
        logger.info(
            "analyst.retry.scheduled",
            extra={"examId": exam_id, "delaySeconds": self.config.retry_delay_seconds},
        )

    # -- deterministic aggregation -------------------------------------------

    def _aggregate(self, exam_id: str) -> _Aggregate:
        """Aggregate all completed sessions for ``exam_id`` (10.2, 10.3).

        Grades each answer deterministically against its server-side answer key,
        derives per-session scores (percentage of marks earned), the fixed-band
        distribution, the mean, the flagged-anomaly count, the per-topic
        accuracy/difficulty heatmap, and each student's score + per-topic
        accuracy (the input to the suggestion narrative).
        """
        session = self.session_factory()
        try:
            exam = ExamRepository(session).get(exam_id)
            subject = exam.subject if exam is not None else ""

            all_sessions = ExamSessionRepository(session).list_for_exam(exam_id)
            completed = [
                s for s in all_sessions if s.status == SessionStatus.SUBMITTED
            ]

            papers = PaperRepository(session)
            answers_repo = AnswerRepository(session)
            anomalies = AnomalyRepository(session)

            scores: list[float] = []
            student_results: dict[str, dict[str, Any]] = {}
            anomaly_count = 0

            # Topic-level accumulators across all completed sessions (10.3).
            topic_correct: dict[str, int] = defaultdict(int)
            topic_answered: dict[str, int] = defaultdict(int)
            topic_difficulty_sum: dict[str, float] = defaultdict(float)
            topic_question_count: dict[str, int] = defaultdict(int)

            for s in completed:
                questions = papers.list_questions(s.paper_id)
                answer_by_q = {
                    a.question_id: a
                    for a in answers_repo.list_for_session(s.id)
                }

                total_marks = 0.0
                correct_marks = 0.0
                stu_topic_correct: dict[str, int] = defaultdict(int)
                stu_topic_total: dict[str, int] = defaultdict(int)

                for q in questions:
                    total_marks += q.max_marks
                    topic_difficulty_sum[q.topic] += q.difficulty
                    topic_question_count[q.topic] += 1
                    topic_answered[q.topic] += 1
                    stu_topic_total[q.topic] += 1

                    answer = answer_by_q.get(q.id)
                    if answer is not None and is_correct(answer.response, q.answer_key):
                        correct_marks += q.max_marks
                        stu_topic_correct[q.topic] += 1
                        topic_correct[q.topic] += 1

                score_pct = (
                    (correct_marks / total_marks * 100.0) if total_marks > 0 else 0.0
                )
                scores.append(score_pct)

                topic_accuracy = {
                    topic: round(stu_topic_correct[topic] / total * 100.0, 2)
                    for topic, total in stu_topic_total.items()
                }
                student_results[s.student_id] = {
                    "score": round(score_pct, 2),
                    "topicAccuracy": topic_accuracy,
                }

                anomaly_count += len(anomalies.list_for_session(s.id))

            summary = {
                "distribution": build_distribution(scores),
                "mean": mean_score(scores),
                "anomalyCount": anomaly_count,
                "completedStudents": len(completed),
                "status": STATUS_READY,
            }

            heatmap_topics = {
                topic: {
                    "accuracy": round(
                        topic_correct[topic] / topic_answered[topic] * 100.0, 2
                    )
                    if topic_answered[topic] > 0
                    else 0.0,
                    "difficulty": round(
                        topic_difficulty_sum[topic]
                        / topic_question_count[topic]
                        * 100.0,
                        2,
                    )
                    if topic_question_count[topic] > 0
                    else 0.0,
                }
                for topic in topic_answered
            }
            difficulty_heatmap = {"topics": heatmap_topics, "status": STATUS_READY}

            return _Aggregate(
                subject=subject,
                scores=scores,
                summary=summary,
                difficulty_heatmap=difficulty_heatmap,
                student_results=student_results,
            )
        finally:
            session.close()

    # -- suggestions (LLM) ---------------------------------------------------

    async def _suggestions_with_retries(
        self, subject: str, student_results: dict[str, dict[str, Any]]
    ) -> dict[str, list[str]]:
        """Generate per-student suggestions, retrying on LLM failure (10.4/10.6).

        Runs up to ``config.max_retries`` attempts. Each attempt calls the LLM,
        parses the JSON, and validates that every completed student received at
        least one suggestion. Raises :class:`LLMError` if every attempt fails (the
        caller turns this into a partial report + scheduled retry, 10.6).
        """
        if not student_results:
            # No completed students: the suggestions section is trivially done.
            return {}

        prompt = build_analyst_prompt(
            subject=subject, student_results=student_results
        )
        attempts = max(1, self.config.max_retries)
        last_error = "unknown"
        for attempt in range(1, attempts + 1):
            try:
                raw = await self.llm.complete(
                    prompt,
                    temperature=self.config.temperature,
                    timeout=self.config.llm_timeout_seconds,
                )
                suggestions = self._parse_suggestions(raw, set(student_results))
                logger.info(
                    "analyst.suggestions.ok",
                    extra={
                        "attempt": attempt,
                        "promptVersion": ANALYST_PROMPT_VERSION,
                        "students": len(suggestions),
                    },
                )
                return suggestions
            except LLMError as exc:
                last_error = str(exc)
                logger.warning(
                    "analyst.suggestions.attempt_failed",
                    extra={"attempt": attempt, "error": last_error},
                )
                continue
        raise LLMError(f"suggestions generation failed after {attempts} attempts: {last_error}")

    @staticmethod
    def _parse_suggestions(
        raw: str, student_ids: set[str]
    ) -> dict[str, list[str]]:
        """Parse the model JSON into ``{student_id: [suggestion, ...]}`` (10.4).

        Raises :class:`LLMError` (so the caller retries) when the output is
        malformed or any completed student lacks at least one suggestion.
        """
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise LLMError("suggestions output was not valid JSON") from exc

        students = data.get("students") if isinstance(data, dict) else None
        if not isinstance(students, dict):
            raise LLMError("suggestions output missing 'students' object")

        result: dict[str, list[str]] = {}
        for student_id in student_ids:
            raw_list = students.get(student_id)
            if not isinstance(raw_list, list):
                raise LLMError(f"no suggestions for student {student_id}")
            cleaned = [str(item).strip() for item in raw_list if str(item).strip()]
            if not cleaned:  # 10.4: at least one suggestion per student
                raise LLMError(f"empty suggestions for student {student_id}")
            result[student_id] = cleaned
        return result

    # -- assembly / persistence ---------------------------------------------

    @staticmethod
    def _assemble_per_student(
        student_results: dict[str, dict[str, Any]],
        *,
        suggestions: dict[str, list[str]] | None,
        status: str,
    ) -> dict[str, Any]:
        """Build the ``per_student`` section, ready or pending (10.4/10.6)."""
        students: dict[str, Any] = {}
        for student_id, result in student_results.items():
            entry = {
                "score": result["score"],
                "topicAccuracy": result["topicAccuracy"],
            }
            if suggestions is not None:
                entry["suggestions"] = suggestions.get(student_id, [])
                entry["suggestionsStatus"] = STATUS_READY
            else:
                entry["suggestions"] = []
                entry["suggestionsStatus"] = STATUS_PENDING
            students[student_id] = entry
        return {"students": students, "status": status}

    def _persist(
        self, exam_id: str, aggregate: _Aggregate, per_student: dict[str, Any]
    ) -> str:
        """Upsert the analytics record for ``exam_id`` and return its id."""
        session = self.session_factory()
        try:
            repo = ExamAnalyticsRepository(session)
            row = repo.upsert(
                exam_id=exam_id,
                summary=aggregate.summary,
                difficulty_heatmap=aggregate.difficulty_heatmap,
                per_student=per_student,
            )
            return row.id
        finally:
            session.close()

    # -- events --------------------------------------------------------------

    async def _emit_report_ready(self, exam_id: str, analytics_id: str) -> None:
        """Emit a ``report.ready`` event identifying the exam (10.5/10.7)."""
        if self.bus is None:
            return
        await self.bus.publish(
            Event(
                type=EventType.REPORT_READY,
                payload={"examId": exam_id, "analyticsId": analytics_id},
                source=ANALYST_SOURCE,
            )
        )
        logger.info(
            "analyst.report_ready",
            extra={"examId": exam_id, "analyticsId": analytics_id},
        )


def register_analyst(
    orchestrator,
    *,
    llm: LLMClient,
    bus: EventBus,
    session_factory: Callable[[], Session],
    config: AnalystConfig | None = None,
) -> AnalystAgent:
    """Build an :class:`AnalystAgent` and register it on the orchestrator.

    Wires the agent's :meth:`on_exam_completed` handler to
    :data:`EventType.EXAM_COMPLETED` so it is subscribed before any event is
    published (Requirement 11.1), following the Architect/Guardian/Herald/
    Sentinel registration pattern. Returns the agent so the caller can hold a
    reference (e.g. to await pending report builds on shutdown).
    """
    agent = AnalystAgent(
        llm=llm,
        bus=bus,
        session_factory=session_factory,
        config=config or AnalystConfig(),
    )
    orchestrator.register_handler(
        EventType.EXAM_COMPLETED, agent.on_exam_completed
    )
    return agent


__all__ = [
    "AnalystAgent",
    "AnalystConfig",
    "ANALYST_SOURCE",
    "STATUS_READY",
    "STATUS_PENDING",
    "score_bands",
    "band_for",
    "build_distribution",
    "mean_score",
    "is_correct",
    "register_analyst",
]
