"""Unit tests for the Auditor agent fairness-audit logic (task 24.1).

Covers Requirement 13 with a mocked LLM (no network):

- approved verdict when every question passes all three dimensions, audit_status
  set to ``approved`` and an ``audit.completed`` event emitted (13.3, 13.4)
- needs_revision verdict when any dimension fails: audit_status set to
  ``flagged`` and the failing question's id + failing dimension(s) + issue
  descriptions recorded in the emitted event (13.3, 13.5)
- audit failure (model unavailable / unparseable output) leaves audit_status
  unchanged from its pre-review value and emits ``audit.failed`` with a reason
  (13.6)

A real ``PaperRepository`` over in-memory SQLite backs persistence so the
audit-status writes are exercised against the actual ORM. The LLM is always a
deterministic in-process double; the event bus is a capturing stand-in.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.auditor import (
    AUDITOR_SOURCE,
    VERDICT_APPROVED,
    VERDICT_NEEDS_REVISION,
    AuditConfig,
    AuditorAgent,
)
from app.agents.llm import CallableLLMClient, LLMClient, LLMError
from app.agents.prompts.auditor import DIMENSIONS
from app.core.db import Base
from app.core.events import Event, EventType
from app.models.enums import AuditStatus, QuestionType, Role
from app.models.orm import Exam, GeneratedPaper, Question, User
from app.repositories.paper import PaperRepository


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

    async def complete(self, prompt, *, temperature=0.2, timeout=None) -> str:
        self.calls += 1
        raise LLMError("simulated model outage")


def _all_pass_llm(question_ids):
    """LLM returning every dimension 'pass' for each question (approved)."""

    def _fn(_prompt: str) -> str:
        return json.dumps(
            {
                "questions": {
                    qid: {dim: {"result": "pass", "issue": ""} for dim in DIMENSIONS}
                    for qid in question_ids
                }
            }
        )

    return CallableLLMClient(_fn)


def _fail_one_llm(question_ids, failing_qid, failing_dim, issue_text):
    """LLM that fails one dimension on one question (needs_revision)."""

    def _fn(_prompt: str) -> str:
        questions = {}
        for qid in question_ids:
            dims = {}
            for dim in DIMENSIONS:
                if qid == failing_qid and dim == failing_dim:
                    dims[dim] = {"result": "fail", "issue": issue_text}
                else:
                    dims[dim] = {"result": "pass", "issue": ""}
            questions[qid] = dims
        return json.dumps({"questions": questions})

    return CallableLLMClient(_fn)


def _bad_json_llm():
    """LLM returning unparseable output (review cannot be completed)."""
    return CallableLLMClient(lambda _p: "this is not json")


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


def _seed_paper(session_factory, *, audit_status=AuditStatus.PENDING) -> dict:
    """Seed an exam + a 2-question paper for one student; return ids."""
    session = session_factory()

    student = User(
        email="s@example.com", full_name="S", role=Role.STUDENT, password_hash="x"
    )
    session.add(student)
    session.flush()

    exam = Exam(
        title="Algebra",
        subject="Mathematics",
        blueprint={"topics": [{"name": "algebra", "count": 2}]},
        duration_minutes=60,
        starts_at=datetime.now(timezone.utc),
        created_by=student.id,
    )
    session.add(exam)
    session.flush()

    paper = GeneratedPaper(
        exam_id=exam.id,
        student_id=student.id,
        seed="seed-1",
        audit_status=audit_status,
    )
    session.add(paper)
    session.flush()

    question_ids = []
    for idx in range(2):
        q = Question(
            paper_id=paper.id,
            index=idx,
            type=QuestionType.MCQ,
            prompt=f"Q{idx}?",
            options=["A", "B", "C", "D"],
            answer_key="A",
            topic="algebra",
            difficulty=0.5,
            max_marks=1.0,
        )
        session.add(q)
        session.flush()
        question_ids.append(q.id)

    session.commit()
    ids = {"paper": paper.id, "questions": question_ids}
    session.close()
    return ids


def _make_auditor(session_factory, llm, bus) -> AuditorAgent:
    return AuditorAgent(
        llm=llm,
        bus=bus,
        paper_repo_factory=lambda: PaperRepository(session_factory()),
        config=AuditConfig(),
    )


def _audit_status(session_factory, paper_id: str) -> AuditStatus:
    db = session_factory()
    try:
        return PaperRepository(db).get(paper_id).audit_status
    finally:
        db.close()


# --- approved verdict (13.3, 13.4) ------------------------------------------


@pytest.mark.asyncio
async def test_all_pass_yields_approved_and_sets_status(session_factory) -> None:
    """13.3/13.4: all dimensions pass -> approved verdict + audit_status approved."""
    ids = _seed_paper(session_factory)
    bus = CapturingBus()
    auditor = _make_auditor(session_factory, _all_pass_llm(ids["questions"]), bus)

    audit = await auditor.audit_paper(ids["paper"])

    assert audit is not None
    assert audit.verdict == VERDICT_APPROVED
    assert _audit_status(session_factory, ids["paper"]) == AuditStatus.APPROVED

    completed = [e for e in bus.events if e.type == EventType.AUDIT_COMPLETED]
    assert len(completed) == 1
    assert completed[0].source == AUDITOR_SOURCE
    assert completed[0].payload["verdict"] == VERDICT_APPROVED
    assert completed[0].payload["auditStatus"] == AuditStatus.APPROVED.value
    assert completed[0].payload["flagged"] == []
    assert not [e for e in bus.events if e.type == EventType.AUDIT_FAILED]


@pytest.mark.asyncio
async def test_paper_with_no_questions_is_approved(session_factory) -> None:
    """13.3: a paper with no questions trivially passes -> approved (no LLM call)."""
    session = session_factory()
    student = User(
        email="t@example.com", full_name="T", role=Role.STUDENT, password_hash="x"
    )
    session.add(student)
    session.flush()
    exam = Exam(
        title="Empty",
        subject="Math",
        blueprint={"topics": [{"name": "x", "count": 0}]},
        duration_minutes=60,
        starts_at=datetime.now(timezone.utc),
        created_by=student.id,
    )
    session.add(exam)
    session.flush()
    paper = GeneratedPaper(exam_id=exam.id, student_id=student.id, seed="s")
    session.add(paper)
    session.commit()
    paper_id = paper.id
    session.close()

    failing = AlwaysFailLLM()  # must not be called for an empty paper
    bus = CapturingBus()
    auditor = _make_auditor(session_factory, failing, bus)

    audit = await auditor.audit_paper(paper_id)

    assert audit is not None
    assert audit.verdict == VERDICT_APPROVED
    assert failing.calls == 0
    assert _audit_status(session_factory, paper_id) == AuditStatus.APPROVED


# --- needs_revision verdict + flagged recording (13.3, 13.5) ----------------


@pytest.mark.asyncio
async def test_any_fail_yields_needs_revision_and_records_flags(
    session_factory,
) -> None:
    """13.3/13.5: a failing dimension -> needs_revision, flagged + status flagged."""
    ids = _seed_paper(session_factory)
    failing_qid = ids["questions"][0]
    bus = CapturingBus()
    llm = _fail_one_llm(
        ids["questions"], failing_qid, "cultural_bias", "Region-specific reference."
    )
    auditor = _make_auditor(session_factory, llm, bus)

    audit = await auditor.audit_paper(ids["paper"])

    assert audit is not None
    assert audit.verdict == VERDICT_NEEDS_REVISION
    assert _audit_status(session_factory, ids["paper"]) == AuditStatus.FLAGGED

    completed = [e for e in bus.events if e.type == EventType.AUDIT_COMPLETED]
    assert len(completed) == 1
    payload = completed[0].payload
    assert payload["verdict"] == VERDICT_NEEDS_REVISION
    assert payload["auditStatus"] == AuditStatus.FLAGGED.value

    # 13.5: exactly the failing question recorded, with its failing dimension
    # and an issue description.
    flagged = payload["flagged"]
    assert len(flagged) == 1
    entry = flagged[0]
    assert entry["questionId"] == failing_qid
    assert entry["failingDimensions"] == ["cultural_bias"]
    assert entry["issues"]["cultural_bias"] == "Region-specific reference."
    assert not [e for e in bus.events if e.type == EventType.AUDIT_FAILED]


# --- audit failure leaves status unchanged (13.6) ---------------------------


@pytest.mark.asyncio
async def test_llm_outage_leaves_status_unchanged_and_emits_failed(
    session_factory,
) -> None:
    """13.6: an LLM outage leaves audit_status unchanged and emits audit.failed."""
    ids = _seed_paper(session_factory, audit_status=AuditStatus.PENDING)
    bus = CapturingBus()
    failing = AlwaysFailLLM()
    auditor = _make_auditor(session_factory, failing, bus)

    result = await auditor.audit_paper(ids["paper"])

    assert result is None
    # Status is unchanged from its pre-review value.
    assert _audit_status(session_factory, ids["paper"]) == AuditStatus.PENDING
    # Retried the configured number of times before giving up.
    assert failing.calls == auditor.config.max_retries

    failed = [e for e in bus.events if e.type == EventType.AUDIT_FAILED]
    assert len(failed) == 1
    assert failed[0].payload["paperId"] == ids["paper"]
    assert failed[0].payload["reason"]  # a non-empty reason is included
    assert not [e for e in bus.events if e.type == EventType.AUDIT_COMPLETED]


@pytest.mark.asyncio
async def test_unparseable_output_leaves_status_unchanged_and_emits_failed(
    session_factory,
) -> None:
    """13.6: unparseable model output -> audit.failed, status unchanged."""
    # Pre-review status is APPROVED; a failed review must not overwrite it.
    ids = _seed_paper(session_factory, audit_status=AuditStatus.APPROVED)
    bus = CapturingBus()
    auditor = _make_auditor(session_factory, _bad_json_llm(), bus)

    result = await auditor.audit_paper(ids["paper"])

    assert result is None
    assert _audit_status(session_factory, ids["paper"]) == AuditStatus.APPROVED
    failed = [e for e in bus.events if e.type == EventType.AUDIT_FAILED]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "invalid_json"


@pytest.mark.asyncio
async def test_missing_paper_emits_failed(session_factory) -> None:
    """13.6: auditing a non-existent paper emits audit.failed (nothing to set)."""
    bus = CapturingBus()
    auditor = _make_auditor(session_factory, _all_pass_llm([]), bus)

    result = await auditor.audit_paper("does-not-exist")

    assert result is None
    failed = [e for e in bus.events if e.type == EventType.AUDIT_FAILED]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "paper_not_found"


# --- event handler scheduling -----------------------------------------------


@pytest.mark.asyncio
async def test_on_paper_generated_schedules_audit(session_factory) -> None:
    """on_paper_generated schedules a background audit that sets the status."""
    ids = _seed_paper(session_factory)
    bus = CapturingBus()
    auditor = _make_auditor(session_factory, _all_pass_llm(ids["questions"]), bus)

    await auditor.on_paper_generated(
        Event(
            type=EventType.PAPER_GENERATED,
            payload={"paperId": ids["paper"]},
            source="Architect",
        )
    )
    await auditor.wait_for_pending()

    assert _audit_status(session_factory, ids["paper"]) == AuditStatus.APPROVED
    assert [e for e in bus.events if e.type == EventType.AUDIT_COMPLETED]


@pytest.mark.asyncio
async def test_on_paper_generated_ignores_incomplete_event(session_factory) -> None:
    """A payload without a paperId is ignored (no audit scheduled)."""
    bus = CapturingBus()
    auditor = _make_auditor(session_factory, _all_pass_llm([]), bus)

    await auditor.on_paper_generated(
        Event(type=EventType.PAPER_GENERATED, payload={}, source="Architect")
    )
    await auditor.wait_for_pending()

    assert bus.events == []
