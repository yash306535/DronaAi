"""Unit tests for the Analyst agent aggregation and partial-report flow (task 21.2).

Covers Requirement 10:

- score distribution grouped into fixed bands covering 0..100, and the mean
  rounded to 2 decimal places (10.2)
- at least one improvement suggestion per completed student (10.4)
- on LLM failure after the configured retries: a *partial* report is persisted
  with the suggestions section marked ``pending`` and a retry is scheduled
  within 300s; no ``report.ready`` is emitted yet (10.6)
- when the scheduled retry completes the suggestions, the report is updated and
  ``report.ready`` is emitted (10.7)

A real in-memory SQLite DB backs the repositories so aggregation/persistence is
exercised end to end. The LLM is always a deterministic in-process double (no
network). The event bus is a capturing stand-in so ``report.ready`` emission can
be asserted.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.analyst import (
    STATUS_PENDING,
    STATUS_READY,
    AnalystAgent,
    AnalystConfig,
    band_for,
    build_distribution,
    mean_score,
    score_bands,
)
from app.agents.llm import CallableLLMClient, LLMError, LLMClient
from app.core.db import Base
from app.core.events import Event, EventType
from app.models.enums import (
    AnomalyCategory,
    AuditStatus,
    QuestionType,
    Role,
    SessionStatus,
    SourceAgent,
)
from app.models.orm import (
    Anomaly,
    Answer,
    Exam,
    ExamSession,
    GeneratedPaper,
    Question,
    User,
)
from app.repositories.analytics import ExamAnalyticsRepository


# --- test doubles -----------------------------------------------------------


class CapturingBus:
    """Records published events instead of fanning them out."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.events.append(event)


class AlwaysFailLLM(LLMClient):
    """An LLM client whose every call raises :class:`LLMError` (no network)."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, prompt, *, temperature=0.4, timeout=None) -> str:
        self.calls += 1
        raise LLMError("simulated model outage")


def _suggestions_llm(student_ids):
    """Return a CallableLLMClient that gives one suggestion per known student."""

    def _fn(_prompt: str) -> str:
        return json.dumps(
            {"students": {sid: [f"Review your weakest topic, {sid}."] for sid in student_ids}}
        )

    return CallableLLMClient(_fn)


# --- fixtures ---------------------------------------------------------------


@pytest.fixture()
def session_factory():
    import app.models  # noqa: F401 - register tables

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    yield factory
    engine.dispose()


def _seed_exam_with_results(session_factory) -> dict:
    """Seed an exam with two completed students whose answers differ in score.

    Student A answers both questions correctly (100%); student B answers one
    correctly (50%). Each paper has two single-mark MCQ questions over two
    topics so the per-topic heatmap is exercised. One confirmed anomaly is
    attached to student A's session so the anomaly count is non-zero.
    """
    session = session_factory()

    def _student(email: str) -> User:
        u = User(
            email=email, full_name=email, role=Role.STUDENT, password_hash="x"
        )
        session.add(u)
        session.flush()
        return u

    student_a = _student("a@example.com")
    student_b = _student("b@example.com")

    exam = Exam(
        title="Algebra",
        subject="Mathematics",
        blueprint={"topics": [{"name": "algebra", "count": 1}, {"name": "geometry", "count": 1}]},
        duration_minutes=60,
        starts_at=datetime.now(timezone.utc),
        created_by=student_a.id,
    )
    session.add(exam)
    session.flush()

    student_ids = []
    session_ids = {}
    for student, n_correct in ((student_a, 2), (student_b, 1)):
        paper = GeneratedPaper(
            exam_id=exam.id,
            student_id=student.id,
            seed=f"seed-{student.id}",
            audit_status=AuditStatus.APPROVED,
        )
        session.add(paper)
        session.flush()

        questions = []
        for idx, topic in enumerate(("algebra", "geometry")):
            q = Question(
                paper_id=paper.id,
                index=idx,
                type=QuestionType.MCQ,
                prompt=f"Q{idx} on {topic}?",
                options=["A", "B", "C", "D"],
                answer_key="A",
                topic=topic,
                difficulty=0.5,
                max_marks=1.0,
            )
            session.add(q)
            questions.append(q)
        session.flush()

        exam_session = ExamSession(
            exam_id=exam.id,
            student_id=student.id,
            paper_id=paper.id,
            status=SessionStatus.SUBMITTED,
            started_at=datetime.now(timezone.utc),
            submitted_at=datetime.now(timezone.utc),
        )
        session.add(exam_session)
        session.flush()
        session_ids[student.id] = exam_session.id

        # Answer the first ``n_correct`` questions correctly ("A"), the rest wrong.
        for i, q in enumerate(questions):
            session.add(
                Answer(
                    session_id=exam_session.id,
                    question_id=q.id,
                    response="A" if i < n_correct else "Z",
                    time_spent_ms=1000,
                )
            )
        student_ids.append(student.id)

    # One confirmed anomaly on student A's session (anomaly count == 1).
    session.add(
        Anomaly(
            session_id=session_ids[student_a.id],
            source_agent=SourceAgent.SENTINEL,
            category=AnomalyCategory.TAB_SWITCH,
            score=0.6,
            reasons=["tab switching"],
            evidence={},
            confirmed=True,
        )
    )

    session.commit()
    ids = {
        "exam": exam.id,
        "students": student_ids,
        "student_a": student_a.id,
        "student_b": student_b.id,
    }
    session.close()
    return ids


def _make_analyst(session_factory, llm, bus) -> AnalystAgent:
    return AnalystAgent(
        llm=llm,
        bus=bus,
        session_factory=session_factory,
        # Tiny retry delay so the scheduled-retry test does not actually wait
        # 300s; the agent's behavior (partial-then-complete) is unchanged.
        config=AnalystConfig(retry_delay_seconds=0.01),
    )


# --- pure helpers (10.2) ----------------------------------------------------


def test_score_bands_cover_full_range() -> None:
    """10.2: the fixed bands tile the whole 0..100 scoring range contiguously."""
    bands = score_bands()
    assert bands[0] == "0-10"
    assert bands[-1] == "90-100"
    assert len(bands) == 10


@pytest.mark.parametrize(
    "score,expected",
    [(0, "0-10"), (9.9, "0-10"), (10, "10-20"), (55, "50-60"), (100, "90-100")],
)
def test_band_for_assignment(score, expected) -> None:
    """10.2: a score is assigned to its fixed band, with 100 in the top band."""
    assert band_for(score) == expected


def test_build_distribution_counts_every_band() -> None:
    """10.2: the distribution includes every band (zero-filled) and counts scores."""
    dist = build_distribution([5.0, 5.0, 95.0])
    assert dist["0-10"] == 2
    assert dist["90-100"] == 1
    assert set(dist) == set(score_bands())  # all bands present


def test_mean_rounds_to_two_decimals() -> None:
    """10.2: the mean score is rounded to 2 decimal places."""
    # (100 + 50 + 33.333...) / 3 = 61.111... -> 61.11
    assert mean_score([100.0, 50.0, 100.0 / 3.0]) == 61.11
    assert mean_score([]) == 0.0


# --- full report (10.2, 10.4, 10.5) -----------------------------------------


@pytest.mark.asyncio
async def test_full_report_aggregates_and_emits_report_ready(session_factory) -> None:
    """10.2/10.4/10.5: a full report is persisted and report.ready is emitted."""
    ids = _seed_exam_with_results(session_factory)
    bus = CapturingBus()
    llm = _suggestions_llm(ids["students"])
    analyst = _make_analyst(session_factory, llm, bus)

    await analyst.build_report(ids["exam"])

    db = session_factory()
    try:
        row = ExamAnalyticsRepository(db).get_for_exam(ids["exam"])
    finally:
        db.close()
    assert row is not None

    # 10.2: distribution covers the full range; A=100% -> 90-100, B=50% -> 50-60.
    dist = row.summary["distribution"]
    assert set(dist) == set(score_bands())
    assert dist["90-100"] == 1
    assert dist["50-60"] == 1
    # 10.2: mean of {100, 50} = 75.0, rounded to 2 dp.
    assert row.summary["mean"] == 75.0
    # 10.2: anomaly count summary.
    assert row.summary["anomalyCount"] == 1

    # 10.3: per-topic accuracy/difficulty present and in 0..100.
    topics = row.difficulty_heatmap["topics"]
    assert set(topics) == {"algebra", "geometry"}
    for stats in topics.values():
        assert 0 <= stats["accuracy"] <= 100
        assert 0 <= stats["difficulty"] <= 100

    # 10.4: at least one suggestion for every completed student.
    students = row.per_student["students"]
    assert set(students) == set(ids["students"])
    for entry in students.values():
        assert entry["suggestionsStatus"] == STATUS_READY
        assert len(entry["suggestions"]) >= 1

    # 10.5: exactly one report.ready emitted for the exam.
    ready = [e for e in bus.events if e.type == EventType.REPORT_READY]
    assert len(ready) == 1
    assert ready[0].payload["examId"] == ids["exam"]


# --- partial report + scheduled retry (10.6, 10.7) --------------------------


@pytest.mark.asyncio
async def test_llm_failure_produces_partial_report_and_no_report_ready(
    session_factory,
) -> None:
    """10.6: after retries exhaust, a partial report is persisted (suggestions pending)."""
    ids = _seed_exam_with_results(session_factory)
    bus = CapturingBus()
    failing = AlwaysFailLLM()
    analyst = _make_analyst(session_factory, failing, bus)
    # Avoid the auto-scheduled retry firing during this test: make the delay long.
    analyst.config.retry_delay_seconds = 3600.0

    await analyst.build_report(ids["exam"])

    db = session_factory()
    try:
        row = ExamAnalyticsRepository(db).get_for_exam(ids["exam"])
    finally:
        db.close()
    assert row is not None

    # 10.6: deterministic sections present...
    assert row.summary["mean"] == 75.0
    assert set(row.difficulty_heatmap["topics"]) == {"algebra", "geometry"}
    # ...suggestions section marked pending with no suggestions yet.
    assert row.per_student["status"] == STATUS_PENDING
    for entry in row.per_student["students"].values():
        assert entry["suggestionsStatus"] == STATUS_PENDING
        assert entry["suggestions"] == []

    # The LLM was retried the configured number of times before giving up.
    assert failing.calls == analyst.config.max_retries
    # 10.5: no report.ready until all sections complete.
    assert not [e for e in bus.events if e.type == EventType.REPORT_READY]


@pytest.mark.asyncio
async def test_llm_failure_schedules_retry_that_completes_report(
    session_factory,
) -> None:
    """10.6/10.7: the scheduled retry completes the suggestions and emits report.ready."""
    ids = _seed_exam_with_results(session_factory)
    bus = CapturingBus()
    failing = AlwaysFailLLM()
    analyst = _make_analyst(session_factory, failing, bus)

    # First build fails -> partial report + a retry scheduled (delay 0.01s).
    await analyst.build_report(ids["exam"])
    assert not [e for e in bus.events if e.type == EventType.REPORT_READY]

    # Swap in a working LLM so the scheduled retry succeeds (10.7).
    analyst.llm = _suggestions_llm(ids["students"])

    # Let the scheduled retry task run to completion.
    await analyst.wait_for_pending()

    # 10.7: the report is updated and report.ready is now emitted.
    ready = [e for e in bus.events if e.type == EventType.REPORT_READY]
    assert len(ready) == 1
    assert ready[0].payload["examId"] == ids["exam"]

    db = session_factory()
    try:
        row = ExamAnalyticsRepository(db).get_for_exam(ids["exam"])
    finally:
        db.close()
    assert row.per_student["status"] == STATUS_READY
    for entry in row.per_student["students"].values():
        assert entry["suggestionsStatus"] == STATUS_READY
        assert len(entry["suggestions"]) >= 1
