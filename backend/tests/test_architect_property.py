"""Property-based test for Architect paper uniqueness & distribution (task 9.3).

**Property 4: Paper uniqueness** — for all pairs of distinct papers generated
for the same exam, their content similarity is strictly below the configured
``uniqueness_ceiling`` while each paper's topic and difficulty distribution
matches the blueprint exactly.

**Validates: Requirements 4.2, 4.3**

The LLM is a deterministic, no-network stub: a :class:`CallableLLMClient` whose
output is a function of the prompt. Because the Architect injects a per-student
uniqueness seed into the prompt (``hash(exam_id+student_id+nonce)``), the stub
emits per-student-distinct question prompts while honoring the blueprint's
per-topic and per-difficulty counts. The Architect's own validation enforces the
distribution match (it would raise/abort otherwise), and the test asserts the
pairwise similarity ceiling directly.
"""

from __future__ import annotations

import json
import re
from collections import Counter

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.architect import (
    ArchitectAgent,
    GenerationConfig,
    _expected_difficulty_counts,
    difficulty_level,
    paper_similarity,
)
from app.agents.llm import CallableLLMClient
from app.core.events import EventBus
from app.repositories.paper import PaperRepository
from app.schemas.exam import ExamBlueprint

UNIQUENESS_CEILING = 0.9

# Representative numeric difficulty per named band (lands squarely in the band).
_LEVEL_VALUE = {"easy": 0.1, "medium": 0.5, "hard": 0.9}

_SEED_RE = re.compile(r"Uniqueness seed: ([0-9a-f]+)")


def _seed_from_prompt(prompt: str) -> str:
    match = _SEED_RE.search(prompt)
    return match.group(1) if match else "noseed"


def _make_llm_for_blueprint(blueprint: ExamBlueprint) -> CallableLLMClient:
    """Build a stub LLM that emits a blueprint-conforming, seed-unique paper.

    The per-topic counts follow the blueprint exactly; difficulties are assigned
    to match the blueprint's difficulty mix (via the same largest-remainder
    apportionment the Architect validates against). Question prompts embed the
    per-student seed so distinct students get distinct content.
    """
    # Pre-compute the difficulty level to assign to each question index so the
    # produced paper matches the blueprint difficulty distribution exactly.
    total = blueprint.total_questions
    diff_counts = _expected_difficulty_counts(blueprint.difficulty_mix, total)
    difficulty_sequence: list[float] = []
    if diff_counts:
        for level, count in diff_counts.items():
            difficulty_sequence.extend([_LEVEL_VALUE[level]] * count)
    # Pad/truncate to total (no difficulty mix → all medium).
    while len(difficulty_sequence) < total:
        difficulty_sequence.append(0.5)
    difficulty_sequence = difficulty_sequence[:total]

    # Topic sequence following the blueprint per-topic counts.
    topic_sequence: list[str] = []
    for topic in blueprint.topics:
        topic_sequence.extend([topic.name] * topic.count)
    # If counts were unspecified (sum 0), fall back to the first topic.
    if not topic_sequence:
        topic_sequence = [blueprint.topics[0].name] * total

    def fn(prompt: str) -> str:
        seed = _seed_from_prompt(prompt)
        questions = []
        for i in range(total):
            questions.append(
                {
                    "index": i,
                    "type": "mcq",
                    # Seed embedded → unique prompt text per student.
                    "prompt": f"[{seed}] Q{i} on {topic_sequence[i]}?",
                    "options": ["A", "B", "C", "D"],
                    "answer_key": "A",
                    "topic": topic_sequence[i],
                    "difficulty": difficulty_sequence[i],
                    "max_marks": 1.0,
                }
            )
        return json.dumps({"questions": questions})

    return CallableLLMClient(fn)


@st.composite
def blueprints(draw) -> ExamBlueprint:
    """Generate a small, internally-consistent blueprint."""
    n_topics = draw(st.integers(min_value=1, max_value=3))
    counts = draw(
        st.lists(
            st.integers(min_value=1, max_value=4),
            min_size=n_topics,
            max_size=n_topics,
        )
    )
    topics = [{"name": f"topic{i}", "count": c} for i, c in enumerate(counts)]
    total = sum(counts)
    # Optionally pin a difficulty mix that the stub can satisfy exactly.
    use_mix = draw(st.booleans())
    difficulty_mix: dict[str, float] = {}
    if use_mix and total >= 2:
        difficulty_mix = {"easy": 0.5, "hard": 0.5}
    return ExamBlueprint.model_validate(
        {
            "topics": topics,
            "total_questions": total,
            "difficulty_mix": difficulty_mix,
            "question_types": ["mcq"],
        }
    )


def _session_factory():
    import app.models  # noqa: F401

    from app.core.db import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session), engine


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(blueprint=blueprints(), n_students=st.integers(min_value=2, max_value=5))
@pytest.mark.asyncio
async def test_paper_uniqueness_and_distribution(
    blueprint: ExamBlueprint, n_students: int
) -> None:
    """Property 4: pairwise similarity < ceiling and distribution matches blueprint."""
    factory, engine = _session_factory()
    try:
        bus = EventBus()
        agent = ArchitectAgent(
            llm=_make_llm_for_blueprint(blueprint),
            bus=bus,
            paper_repo_factory=lambda: PaperRepository(factory()),
            config=GenerationConfig(max_retries=2),
        )

        papers_prompts: list[list[str]] = []
        for s in range(n_students):
            paper = await agent.provision_student(
                exam_id="exam-1",
                student_id=f"student-{s}",
                blueprint=blueprint,
            )
            # A returned paper means generation + the Architect's distribution
            # validation both succeeded (distribution matches the blueprint).
            assert paper is not None
            repo = PaperRepository(factory())
            questions = repo.list_questions(paper.id)

            # Distribution match (4.2): per-topic counts equal the blueprint.
            topic_counts = Counter(q.topic for q in questions)
            for topic in blueprint.topics:
                if topic.count > 0:
                    assert topic_counts[topic.name] == topic.count

            # Difficulty distribution match (4.2) when a mix was pinned.
            expected_diff = _expected_difficulty_counts(
                blueprint.difficulty_mix, blueprint.total_questions
            )
            if expected_diff:
                diff_counts = Counter(
                    difficulty_level(q.difficulty) for q in questions
                )
                for level, count in expected_diff.items():
                    assert diff_counts.get(level, 0) == count

            papers_prompts.append([q.prompt for q in questions])

        # Pairwise uniqueness (4.3): every distinct pair is below the ceiling.
        for i in range(len(papers_prompts)):
            for j in range(i + 1, len(papers_prompts)):
                sim = paper_similarity(papers_prompts[i], papers_prompts[j])
                assert sim < UNIQUENESS_CEILING
    finally:
        engine.dispose()
