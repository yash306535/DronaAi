"""Unit tests for the Architect agent parsing and failure handling (task 9.4).

Covers Requirement 4 with a mocked LLM (no network):

- schema-validation retry then success (4.5)
- abort + ``generation.failed`` after retries are exhausted (4.6)
- ``generation.failed`` on persistence failure, and NO ``paper.generated`` (4.8)
- ``paper.generated`` emitted on success; answer keys persisted server-side (4.7, 4.9)
- exact total count, topic + difficulty distribution match (4.1, 4.2)
- MCQ option constraints: 2..max options with exactly one correct (4.4)

A real ``PaperRepository`` over in-memory SQLite is used for persistence so the
answer-key-server-side behavior is exercised against the actual ORM.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.architect import (
    ArchitectAgent,
    GenerationConfig,
    PaperValidationError,
    difficulty_level,
    make_seed,
)
from app.agents.llm import CallableLLMClient, StaticMockLLMClient
from app.core.events import EventBus, Event, EventType
from app.models.orm import Question
from app.repositories.paper import PaperRepository
from app.schemas.exam import ExamBlueprint


# --- helpers ----------------------------------------------------------------


def _mcq(index: int, topic: str, difficulty: float, correct: str = "A") -> dict:
    options = ["A", "B", "C", "D"]
    return {
        "index": index,
        "type": "mcq",
        "prompt": f"Question {index}?",
        "options": options,
        "answer_key": correct,
        "topic": topic,
        "difficulty": difficulty,
        "max_marks": 1.0,
    }


def _paper_json(questions: list[dict]) -> str:
    return json.dumps({"questions": questions})


def _simple_blueprint(total: int = 3, topic: str = "algebra") -> ExamBlueprint:
    return ExamBlueprint.model_validate(
        {
            "topics": [{"name": topic, "count": total}],
            "total_questions": total,
            "difficulty_mix": {},
            "question_types": ["mcq"],
        }
    )


@pytest.fixture()
def session_factory():
    import app.models  # noqa: F401

    from app.core.db import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    yield factory
    engine.dispose()


@pytest.fixture()
def collecting_bus():
    """An event bus that records every published event for assertions."""
    bus = EventBus()
    collected: list[Event] = []

    async def _collect(event: Event) -> None:
        collected.append(event)

    for et in (EventType.PAPER_GENERATED, EventType.GENERATION_FAILED):
        bus.subscribe(et, _collect)
    return bus, collected


def _make_agent(llm, bus, session_factory, **cfg) -> ArchitectAgent:
    return ArchitectAgent(
        llm=llm,
        bus=bus,
        paper_repo_factory=lambda: PaperRepository(session_factory()),
        config=GenerationConfig(**cfg),
    )


# --- seed -------------------------------------------------------------------


def test_seed_is_deterministic_for_fixed_nonce() -> None:
    """The seed is a stable hash of exam+student+nonce."""
    a = make_seed("e1", "s1", nonce="n")
    b = make_seed("e1", "s1", nonce="n")
    assert a == b
    assert make_seed("e1", "s2", nonce="n") != a


def test_seed_varies_across_students_with_random_nonce() -> None:
    """Distinct students get distinct seeds even without a fixed nonce."""
    assert make_seed("e1", "s1") != make_seed("e1", "s2")


# --- success path (4.7, 4.9) -----------------------------------------------


@pytest.mark.asyncio
async def test_successful_generation_persists_and_emits(
    collecting_bus, session_factory
) -> None:
    """4.7/4.9: a valid paper is persisted (answer keys server-side) + announced."""
    bus, collected = collecting_bus
    questions = [_mcq(i, "algebra", 0.5) for i in range(3)]
    llm = StaticMockLLMClient([_paper_json(questions)])
    agent = _make_agent(llm, bus, session_factory)

    paper = await agent.provision_student(
        exam_id="e1", student_id="s1", blueprint=_simple_blueprint(3)
    )

    assert paper is not None
    # paper.generated emitted, no generation.failed.
    types = [e.type for e in collected]
    assert EventType.PAPER_GENERATED in types
    assert EventType.GENERATION_FAILED not in types

    # Answer keys are persisted server-side.
    repo = PaperRepository(session_factory())
    rows = repo.list_questions(paper.id)
    assert len(rows) == 3
    assert all(isinstance(q, Question) and q.answer_key for q in rows)


# --- retry then success (4.5) ----------------------------------------------


@pytest.mark.asyncio
async def test_invalid_then_valid_retries_and_succeeds(
    collecting_bus, session_factory
) -> None:
    """4.5: an invalid first output is discarded and a retry succeeds."""
    bus, collected = collecting_bus
    good = _paper_json([_mcq(i, "algebra", 0.5) for i in range(3)])
    # First response has the wrong total count (2 != 3) → triggers a retry.
    bad = _paper_json([_mcq(i, "algebra", 0.5) for i in range(2)])
    llm = StaticMockLLMClient([bad, good])
    agent = _make_agent(llm, bus, session_factory, max_retries=3)

    paper = await agent.provision_student(
        exam_id="e1", student_id="s1", blueprint=_simple_blueprint(3)
    )

    assert paper is not None
    assert len(llm.calls) == 2  # one failed attempt + one success
    assert EventType.PAPER_GENERATED in [e.type for e in collected]


# --- abort after retries exhausted (4.6) -----------------------------------


@pytest.mark.asyncio
async def test_abort_after_retries_emits_generation_failed(
    collecting_bus, session_factory
) -> None:
    """4.6: persistent validation failure aborts with generation.failed, no paper."""
    bus, collected = collecting_bus
    bad = _paper_json([_mcq(i, "algebra", 0.5) for i in range(2)])  # wrong count
    llm = StaticMockLLMClient([bad])  # always returns the bad paper
    agent = _make_agent(llm, bus, session_factory, max_retries=3)

    paper = await agent.provision_student(
        exam_id="e1", student_id="s1", blueprint=_simple_blueprint(3)
    )

    assert paper is None
    assert len(llm.calls) == 3  # exhausted the 3 attempts
    types = [e.type for e in collected]
    assert EventType.GENERATION_FAILED in types
    assert EventType.PAPER_GENERATED not in types
    failed = next(e for e in collected if e.type == EventType.GENERATION_FAILED)
    assert failed.payload["studentId"] == "s1"
    assert failed.payload["cause"] == "schema_validation"


# --- persistence failure (4.8) ---------------------------------------------


@pytest.mark.asyncio
async def test_persistence_failure_emits_generation_failed_only(
    collecting_bus, session_factory
) -> None:
    """4.8: a persistence failure emits generation.failed and NOT paper.generated."""
    bus, collected = collecting_bus
    questions = [_mcq(i, "algebra", 0.5) for i in range(3)]
    llm = StaticMockLLMClient([_paper_json(questions)])

    class FailingRepo:
        def add_with_questions(self, *a, **k):
            raise RuntimeError("db down")

    agent = ArchitectAgent(
        llm=llm,
        bus=bus,
        paper_repo_factory=lambda: FailingRepo(),
        config=GenerationConfig(),
    )

    paper = await agent.provision_student(
        exam_id="e1", student_id="s1", blueprint=_simple_blueprint(3)
    )

    assert paper is None
    types = [e.type for e in collected]
    assert EventType.GENERATION_FAILED in types
    assert EventType.PAPER_GENERATED not in types
    failed = next(e for e in collected if e.type == EventType.GENERATION_FAILED)
    assert failed.payload["cause"] == "persistence"


# --- MCQ option constraints (4.4) ------------------------------------------


@pytest.mark.asyncio
async def test_mcq_more_than_one_correct_is_rejected(
    collecting_bus, session_factory
) -> None:
    """4.4: an MCQ whose answer_key matches no/2 options fails validation."""
    bus, collected = collecting_bus
    # answer_key "Z" matches none of the options → schema invalid.
    q = _mcq(0, "algebra", 0.5)
    q["answer_key"] = "Z"
    questions = [q] + [_mcq(i, "algebra", 0.5) for i in range(1, 3)]
    llm = StaticMockLLMClient([_paper_json(questions)])
    agent = _make_agent(llm, bus, session_factory, max_retries=2)

    paper = await agent.provision_student(
        exam_id="e1", student_id="s1", blueprint=_simple_blueprint(3)
    )
    assert paper is None
    assert EventType.GENERATION_FAILED in [e.type for e in collected]


@pytest.mark.asyncio
async def test_mcq_too_many_options_is_rejected(
    collecting_bus, session_factory
) -> None:
    """4.4: an MCQ with more than the configured max options is rejected."""
    bus, collected = collecting_bus
    q = _mcq(0, "algebra", 0.5)
    q["options"] = ["A", "B", "C", "D", "E"]  # 5 > default max of 4
    # answer_key "A" still matches exactly one, so only the count rule trips.
    questions = [q] + [_mcq(i, "algebra", 0.5) for i in range(1, 3)]
    llm = StaticMockLLMClient([_paper_json(questions)])
    agent = _make_agent(llm, bus, session_factory, max_retries=1)

    paper = await agent.provision_student(
        exam_id="e1", student_id="s1", blueprint=_simple_blueprint(3)
    )
    assert paper is None


# --- distribution match (4.2) ----------------------------------------------


@pytest.mark.asyncio
async def test_topic_distribution_must_match_blueprint(
    collecting_bus, session_factory
) -> None:
    """4.2: per-topic counts must equal the blueprint exactly."""
    bus, collected = collecting_bus
    blueprint = ExamBlueprint.model_validate(
        {
            "topics": [
                {"name": "algebra", "count": 2},
                {"name": "geometry", "count": 1},
            ],
            "total_questions": 3,
            "difficulty_mix": {},
            "question_types": ["mcq"],
        }
    )
    # All three questions tagged "algebra" → geometry count is wrong.
    questions = [_mcq(i, "algebra", 0.5) for i in range(3)]
    llm = StaticMockLLMClient([_paper_json(questions)])
    agent = _make_agent(llm, bus, session_factory, max_retries=1)

    paper = await agent.provision_student(
        exam_id="e1", student_id="s1", blueprint=blueprint
    )
    assert paper is None


@pytest.mark.asyncio
async def test_difficulty_distribution_must_match_blueprint(
    collecting_bus, session_factory
) -> None:
    """4.2: per-difficulty-level counts must equal the blueprint mix exactly."""
    bus, collected = collecting_bus
    blueprint = ExamBlueprint.model_validate(
        {
            "topics": [{"name": "algebra", "count": 4}],
            "total_questions": 4,
            # Expect 2 easy, 2 hard.
            "difficulty_mix": {"easy": 0.5, "hard": 0.5},
            "question_types": ["mcq"],
        }
    )
    # Provide 4 easy questions → hard count is 0, mismatching the expected 2.
    questions = [_mcq(i, "algebra", 0.1) for i in range(4)]
    llm = StaticMockLLMClient([_paper_json(questions)])
    agent = _make_agent(llm, bus, session_factory, max_retries=1)

    paper = await agent.provision_student(
        exam_id="e1", student_id="s1", blueprint=blueprint
    )
    assert paper is None


@pytest.mark.asyncio
async def test_difficulty_distribution_match_succeeds(
    collecting_bus, session_factory
) -> None:
    """4.2: a paper whose difficulty levels match the mix is accepted."""
    bus, collected = collecting_bus
    blueprint = ExamBlueprint.model_validate(
        {
            "topics": [{"name": "algebra", "count": 4}],
            "total_questions": 4,
            "difficulty_mix": {"easy": 0.5, "hard": 0.5},
            "question_types": ["mcq"],
        }
    )
    questions = [
        _mcq(0, "algebra", 0.1),
        _mcq(1, "algebra", 0.2),
        _mcq(2, "algebra", 0.9),
        _mcq(3, "algebra", 0.95),
    ]
    llm = StaticMockLLMClient([_paper_json(questions)])
    agent = _make_agent(llm, bus, session_factory, max_retries=1)

    paper = await agent.provision_student(
        exam_id="e1", student_id="s1", blueprint=blueprint
    )
    assert paper is not None


def test_difficulty_level_banding() -> None:
    """difficulty_level buckets numeric difficulty into named bands."""
    assert difficulty_level(0.0) == "easy"
    assert difficulty_level(0.5) == "medium"
    assert difficulty_level(0.9) == "hard"
    assert difficulty_level(1.0) == "hard"


# --- malformed JSON ---------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_json_is_rejected(collecting_bus, session_factory) -> None:
    """A non-JSON completion fails validation and (exhausted) aborts."""
    bus, collected = collecting_bus
    llm = StaticMockLLMClient(["not valid json"])
    agent = _make_agent(llm, bus, session_factory, max_retries=2)

    paper = await agent.provision_student(
        exam_id="e1", student_id="s1", blueprint=_simple_blueprint(3)
    )
    assert paper is None
    assert EventType.GENERATION_FAILED in [e.type for e in collected]


# --- event-handler scheduling ----------------------------------------------


@pytest.mark.asyncio
async def test_on_exam_provision_schedules_generation(
    collecting_bus, session_factory
) -> None:
    """on_exam_provision runs generation as a background task off the bus path."""
    bus, collected = collecting_bus
    questions = [_mcq(i, "algebra", 0.5) for i in range(3)]
    llm = StaticMockLLMClient([_paper_json(questions)])
    agent = _make_agent(llm, bus, session_factory)

    event = Event(
        type=EventType.EXAM_PROVISION,
        payload={
            "examId": "e1",
            "studentId": "s1",
            "subject": "Math",
            "blueprint": _simple_blueprint(3).model_dump(mode="json"),
        },
        source="exam_service",
    )
    await agent.on_exam_provision(event)
    await agent.wait_for_pending()

    assert EventType.PAPER_GENERATED in [e.type for e in collected]


@pytest.mark.asyncio
async def test_on_exam_provision_ignores_incomplete_event(
    collecting_bus, session_factory
) -> None:
    """An event without a studentId is ignored (no generation, no failure)."""
    bus, collected = collecting_bus
    llm = StaticMockLLMClient([_paper_json([_mcq(0, "algebra", 0.5)])])
    agent = _make_agent(llm, bus, session_factory)

    event = Event(
        type=EventType.EXAM_PROVISION,
        payload={"examId": "e1"},  # no studentId / blueprint
        source="exam_service",
    )
    await agent.on_exam_provision(event)
    await agent.wait_for_pending()
    assert collected == []
