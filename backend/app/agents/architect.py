"""Architect agent: unique per-student exam paper generation (Requirement 4).

The Architect turns an exam blueprint into a *unique yet equivalently fair*
paper for each enrolled student. It subscribes to ``exam.provision`` (one event
per student) and, for each, runs the generation pipeline:

1. **Seed** — derive a per-student uniqueness seed
   ``sha256(exam_id + student_id + nonce)`` (design: "Uniqueness guarantee").
   The seed is injected into the prompt so wording/values diversify across
   students while the blueprint pins the topic/difficulty distribution.
2. **Generate** — call the (mockable) :class:`~app.agents.llm.LLMClient` with the
   versioned Architect prompt, within the configured generation timeout
   (default 60s, Requirement 4.1).
3. **Parse & validate** — parse the JSON completion and validate it against the
   blueprint with *zero deviation*: exactly ``total_questions`` questions
   (4.1), per-topic and per-difficulty counts equal to the blueprint (4.2), and
   every MCQ carrying 2..max options with exactly one correct option (4.4). Any
   failure discards the whole paper (no partial paper is retained, 4.5) and the
   generation is retried up to the configured maximum (default 3 attempts).
4. **Abort** — if validation still fails after the retries are exhausted,
   persist no paper and emit a ``generation.failed`` event naming the student
   and cause (4.6).
5. **Persist & announce** — on success persist the paper with its questions
   (answer keys stored server-side only, 4.9) and emit ``paper.generated``
   (4.7). If persistence fails, emit ``generation.failed`` and do *not* emit
   ``paper.generated`` (4.8).

The agent never blocks event delivery: :meth:`ArchitectAgent.on_exam_provision`
schedules :meth:`ArchitectAgent.provision_student` as a background task so the
LLM round-trip runs off the event-bus delivery path.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import ValidationError as PydanticValidationError

from app.agents.llm import LLMClient, LLMError
from app.agents.prompts.architect import (
    ARCHITECT_PROMPT_VERSION,
    build_architect_prompt,
)
from app.core.events import Event, EventBus, EventType
from app.core.logging import get_logger
from app.models.orm import GeneratedPaper, Question
from app.repositories.paper import PaperRepository
from app.schemas.exam import ExamBlueprint
from app.schemas.question import MAX_MCQ_OPTIONS, MIN_MCQ_OPTIONS, QuestionCreate

logger = get_logger("app.agents.architect")

ARCHITECT_SOURCE = "Architect"

# Standard difficulty bands used to bucket a question's numeric difficulty
# (0..1) into a named level, so a blueprint's proportional difficulty mix can be
# checked as exact per-level counts (Requirement 4.2). Bands are half-open
# ``[lo, hi)`` except the top band which includes 1.0.
DIFFICULTY_BANDS: tuple[tuple[str, float, float], ...] = (
    ("easy", 0.0, 1.0 / 3.0),
    ("medium", 1.0 / 3.0, 2.0 / 3.0),
    ("hard", 2.0 / 3.0, 1.0001),
)


def difficulty_level(value: float) -> str:
    """Map a numeric difficulty (0..1) to its named band (easy/medium/hard)."""
    for name, lo, hi in DIFFICULTY_BANDS:
        if lo <= value < hi:
            return name
    # Clamp out-of-range values to the nearest extreme band.
    return "hard" if value >= 1.0 else "easy"


def make_seed(exam_id: str, student_id: str, nonce: str | None = None) -> str:
    """Return the per-student uniqueness seed ``sha256(exam+student+nonce)``.

    A fresh ``nonce`` (random UUID by default) guarantees that re-provisioning a
    student yields a different seed, so a regenerated paper differs in surface
    form even for the same ``(exam, student)`` pair.
    """
    nonce = nonce if nonce is not None else uuid.uuid4().hex
    digest = hashlib.sha256(f"{exam_id}:{student_id}:{nonce}".encode()).hexdigest()
    return digest


def paper_similarity(
    prompts_a: list[str], prompts_b: list[str]
) -> float:
    """Return a 0..1 content-similarity score between two papers' prompts.

    Uses Jaccard similarity over the set of question-prompt strings: the size of
    the intersection over the size of the union. Two papers that share no prompt
    text score 0.0; identical prompt sets score 1.0. This is the metric the
    uniqueness guarantee is expressed against (Correctness Property 4): for any
    two distinct papers in an exam, ``paper_similarity`` must be strictly below
    the configured uniqueness ceiling.
    """
    set_a = {p.strip() for p in prompts_a if p.strip()}
    set_b = {p.strip() for p in prompts_b if p.strip()}
    if not set_a and not set_b:
        return 1.0
    union = set_a | set_b
    if not union:
        return 0.0
    intersection = set_a & set_b
    return len(intersection) / len(union)


class PaperValidationError(Exception):
    """A generated paper failed schema/blueprint validation (triggers retry).

    Carries a short, safe ``reason`` describing the mismatch (e.g. wrong total
    count, topic-distribution deviation, MCQ option violation). The reason never
    contains model output verbatim beyond a minimal descriptor.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _expected_difficulty_counts(
    difficulty_mix: dict[str, float], total: int
) -> dict[str, int]:
    """Derive exact per-level question counts from a proportional mix.

    Uses the largest-remainder method so the rounded per-level counts always sum
    to ``total`` exactly (no off-by-one from naive rounding).
    """
    if not difficulty_mix:
        return {}
    raw = {level: prop * total for level, prop in difficulty_mix.items()}
    floored = {level: int(value) for level, value in raw.items()}
    remainder = total - sum(floored.values())
    # Distribute the remaining units to the levels with the largest fractional
    # parts (largest-remainder apportionment).
    order = sorted(
        raw, key=lambda level: raw[level] - floored[level], reverse=True
    )
    for level in order[: max(0, remainder)]:
        floored[level] += 1
    return floored


@dataclass(slots=True)
class GenerationConfig:
    """Tunable generation parameters (design/Requirement defaults)."""

    max_retries: int = 3  # 4.5: default 3 attempts
    timeout_seconds: float = 60.0  # 4.1: default 60s generation timeout
    max_options: int = MAX_MCQ_OPTIONS  # 4.4: default 4 options
    min_options: int = MIN_MCQ_OPTIONS  # 4.4: at least 2 options
    temperature: float = 0.9


@dataclass(slots=True)
class ArchitectAgent:
    """Generate, validate, persist, and announce per-student exam papers.

    ``paper_repo_factory`` returns a fresh :class:`PaperRepository` (bound to a
    new DB session) per generation, so background tasks never share a request's
    session. ``llm`` is the mockable model backend; ``bus`` is the event bus the
    agent publishes onto.
    """

    llm: LLMClient
    bus: EventBus
    paper_repo_factory: Callable[[], PaperRepository]
    config: GenerationConfig = field(default_factory=GenerationConfig)
    _tasks: set[asyncio.Task] = field(default_factory=set, init=False)

    # -- event handler -------------------------------------------------------

    async def on_exam_provision(self, event: Event) -> None:
        """Handle an ``exam.provision`` event by scheduling generation (4.1).

        The handler returns immediately after scheduling the per-student
        generation as a background task so it never blocks event-bus delivery
        (the LLM round-trip runs off the delivery path). A payload missing the
        student id is ignored with a warning (a bare exam-level provision event
        has no student to generate for).
        """
        payload = event.payload or {}
        exam_id = payload.get("examId")
        student_id = payload.get("studentId")
        blueprint = payload.get("blueprint")
        subject = payload.get("subject", "")
        if not exam_id or not student_id or blueprint is None:
            logger.warning(
                "architect.provision.skipped_incomplete_event",
                extra={"eventId": event.id},
            )
            return

        task = asyncio.create_task(
            self.provision_student(
                exam_id=exam_id,
                student_id=student_id,
                blueprint=blueprint,
                subject=subject,
            )
        )
        # Track the task so it is not garbage-collected mid-flight; discard on
        # completion to keep the set bounded.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_for_pending(self) -> None:
        """Await all in-flight background generations (used by tests/shutdown)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # -- generation pipeline -------------------------------------------------

    async def provision_student(
        self,
        *,
        exam_id: str,
        student_id: str,
        blueprint: dict | ExamBlueprint,
        subject: str = "",
        nonce: str | None = None,
    ) -> GeneratedPaper | None:
        """Generate, persist, and announce one student's paper.

        Returns the persisted :class:`GeneratedPaper` on success, or ``None`` if
        generation was aborted (retries exhausted) or persistence failed — in
        both failure cases a ``generation.failed`` event is emitted and no
        ``paper.generated`` event is emitted (4.6, 4.8).
        """
        bp = (
            blueprint
            if isinstance(blueprint, ExamBlueprint)
            else ExamBlueprint.model_validate(blueprint)
        )
        seed = make_seed(exam_id, student_id, nonce)

        try:
            questions = await self.generate_validated_questions(
                blueprint=bp, exam_id=exam_id, student_id=student_id, seed=seed,
                subject=subject,
            )
        except PaperValidationError as exc:
            # Retries exhausted: abort, persist nothing, emit generation.failed.
            await self._emit_generation_failed(
                exam_id, student_id, cause="schema_validation", detail=exc.reason
            )
            return None
        except LLMError as exc:
            await self._emit_generation_failed(
                exam_id, student_id, cause="llm_unavailable", detail=str(exc)
            )
            return None

        # Persist the paper + questions (answer keys server-side only, 4.9).
        try:
            paper = self._persist(exam_id, student_id, seed, questions)
        except Exception as exc:  # noqa: BLE001 - any persistence failure (4.8)
            logger.error(
                "architect.persist_failed",
                extra={"examId": exam_id, "studentId": student_id,
                       "error": type(exc).__name__},
            )
            await self._emit_generation_failed(
                exam_id, student_id, cause="persistence", detail="persist_failed"
            )
            return None

        # Success: announce the generated paper (4.7).
        await self.bus.publish(
            Event(
                type=EventType.PAPER_GENERATED,
                payload={
                    "examId": exam_id,
                    "studentId": student_id,
                    "paperId": paper.id,
                    "questionCount": len(questions),
                },
                source=ARCHITECT_SOURCE,
            )
        )
        logger.info(
            "architect.paper_generated",
            extra={"examId": exam_id, "studentId": student_id,
                   "paperId": paper.id},
        )
        return paper

    async def generate_validated_questions(
        self,
        *,
        blueprint: ExamBlueprint,
        exam_id: str,
        student_id: str,
        seed: str,
        subject: str = "",
    ) -> list[QuestionCreate]:
        """Generate and validate a paper, retrying on validation failure.

        Runs up to ``config.max_retries`` attempts (4.5). Each attempt calls the
        LLM, parses the JSON, and validates it against the blueprint with zero
        deviation. A failed attempt retains no partial paper (the per-attempt
        list is discarded). Raises :class:`PaperValidationError` if every attempt
        fails (the caller turns this into an abort + ``generation.failed``, 4.6).
        """
        prompt = build_architect_prompt(
            subject=subject,
            blueprint=blueprint.model_dump(mode="json"),
            seed=seed,
            min_options=self.config.min_options,
            max_options=self.config.max_options,
        )

        last_reason = "unknown"
        attempts = max(1, self.config.max_retries)
        for attempt in range(1, attempts + 1):
            try:
                raw = await asyncio.wait_for(
                    self.llm.complete(
                        prompt, temperature=self.config.temperature,
                        timeout=self.config.timeout_seconds,
                    ),
                    timeout=self.config.timeout_seconds,
                )
                questions = self._parse_and_validate(raw, blueprint)
                logger.info(
                    "architect.generation.ok",
                    extra={"studentId": student_id, "attempt": attempt,
                           "promptVersion": ARCHITECT_PROMPT_VERSION},
                )
                return questions
            except PaperValidationError as exc:
                last_reason = exc.reason
                logger.warning(
                    "architect.generation.invalid",
                    extra={"studentId": student_id, "attempt": attempt,
                           "reason": exc.reason},
                )
                continue  # discard partial paper, retry (4.5)
            except asyncio.TimeoutError as exc:
                raise LLMError("generation timed out") from exc

        # All attempts exhausted (4.6).
        raise PaperValidationError(last_reason)

    # -- parsing & validation ------------------------------------------------

    def _parse_and_validate(
        self, raw: str, blueprint: ExamBlueprint
    ) -> list[QuestionCreate]:
        """Parse the model JSON and validate it against the blueprint (4.1-4.4).

        Raises :class:`PaperValidationError` on any deviation: malformed JSON,
        a per-question schema/MCQ violation, the wrong total count, or a
        topic/difficulty distribution that does not exactly match the blueprint.
        """
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise PaperValidationError("invalid_json") from exc

        items = data.get("questions") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise PaperValidationError("missing_questions_array")

        # Per-question schema validation (MCQ 2..max options, one correct, 4.4).
        questions: list[QuestionCreate] = []
        for item in items:
            try:
                question = QuestionCreate.model_validate(item)
            except PydanticValidationError as exc:
                raise PaperValidationError("question_schema") from exc
            if question.type.value == "mcq" and question.options is not None:
                if len(question.options) > self.config.max_options:
                    raise PaperValidationError("mcq_option_count")
            questions.append(question)

        # Exact total count (4.1).
        if len(questions) != blueprint.total_questions:
            raise PaperValidationError("total_count_mismatch")

        self._validate_topic_distribution(questions, blueprint)
        self._validate_difficulty_distribution(questions, blueprint)
        return questions

    @staticmethod
    def _validate_topic_distribution(
        questions: list[QuestionCreate], blueprint: ExamBlueprint
    ) -> None:
        """Per-topic counts must equal the blueprint exactly when specified (4.2)."""
        expected = {t.name: t.count for t in blueprint.topics if t.count > 0}
        if not expected:
            return  # blueprint did not pin per-topic counts; nothing to enforce
        actual = Counter(q.topic for q in questions)
        for topic, count in expected.items():
            if actual.get(topic, 0) != count:
                raise PaperValidationError("topic_distribution")
        # No extra topics outside the blueprint.
        if set(actual) - set(expected):
            raise PaperValidationError("topic_distribution")

    @staticmethod
    def _validate_difficulty_distribution(
        questions: list[QuestionCreate], blueprint: ExamBlueprint
    ) -> None:
        """Per-difficulty-level counts must equal the blueprint mix exactly (4.2)."""
        expected = _expected_difficulty_counts(
            blueprint.difficulty_mix, blueprint.total_questions
        )
        if not expected:
            return  # no difficulty mix pinned; nothing to enforce
        actual = Counter(difficulty_level(q.difficulty) for q in questions)
        for level, count in expected.items():
            if actual.get(level, 0) != count:
                raise PaperValidationError("difficulty_distribution")

    # -- persistence ---------------------------------------------------------

    def _persist(
        self,
        exam_id: str,
        student_id: str,
        seed: str,
        questions: list[QuestionCreate],
    ) -> GeneratedPaper:
        """Persist the paper and its questions as one unit (answer keys 4.9)."""
        repo = self.paper_repo_factory()
        paper = GeneratedPaper(
            exam_id=exam_id,
            student_id=student_id,
            seed=seed,
        )
        question_rows = [
            Question(
                index=q.index,
                type=q.type,
                prompt=q.prompt,
                options=q.options,
                answer_key=q.answer_key,  # stored server-side only (4.9)
                topic=q.topic,
                difficulty=q.difficulty,
                max_marks=q.max_marks,
            )
            for q in questions
        ]
        return repo.add_with_questions(paper, question_rows)

    # -- events --------------------------------------------------------------

    async def _emit_generation_failed(
        self, exam_id: str, student_id: str, *, cause: str, detail: str
    ) -> None:
        """Emit a ``generation.failed`` event naming the student + cause (4.6/4.8)."""
        await self.bus.publish(
            Event(
                type=EventType.GENERATION_FAILED,
                payload={
                    "examId": exam_id,
                    "studentId": student_id,
                    "cause": cause,
                    "detail": detail,
                },
                source=ARCHITECT_SOURCE,
            )
        )
        logger.warning(
            "architect.generation_failed",
            extra={"examId": exam_id, "studentId": student_id, "cause": cause},
        )


def register_architect(
    orchestrator,
    *,
    llm: LLMClient,
    bus: EventBus,
    paper_repo_factory,
    config: GenerationConfig | None = None,
) -> ArchitectAgent:
    """Build an :class:`ArchitectAgent` and register it on the orchestrator.

    Wires the agent's :meth:`on_exam_provision` handler to
    :data:`EventType.EXAM_PROVISION` so it is subscribed before any event is
    published (Requirement 11.1). Returns the agent so the caller can hold a
    reference (e.g. to await pending generations on shutdown).
    """
    agent = ArchitectAgent(
        llm=llm,
        bus=bus,
        paper_repo_factory=paper_repo_factory,
        config=config or GenerationConfig(),
    )
    orchestrator.register_handler(EventType.EXAM_PROVISION, agent.on_exam_provision)
    return agent


__all__ = [
    "ArchitectAgent",
    "GenerationConfig",
    "PaperValidationError",
    "DIFFICULTY_BANDS",
    "difficulty_level",
    "make_seed",
    "paper_similarity",
    "register_architect",
]
