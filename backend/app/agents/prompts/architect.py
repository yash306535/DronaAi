"""Versioned Architect prompt template (paper generation).

The template mirrors the design's *Architect Prompt (question generation)*. It
is versioned (:data:`ARCHITECT_PROMPT_VERSION`) so a change to the wording is
traceable and a generated paper can record which prompt produced it. The
template is rendered with the blueprint, the per-student uniqueness seed, and an
explicit JSON output schema so the model returns a structure the Architect can
parse and validate (Requirement 4.1-4.4).

Keeping the template here (rather than inline in the agent) means the prompt can
evolve without touching agent logic, and tests can assert the seed/blueprint are
faithfully injected.
"""

from __future__ import annotations

import json
from typing import Any

# Bump when the prompt wording/schema changes in a behavior-affecting way.
ARCHITECT_PROMPT_VERSION = "v1"

# The JSON schema the model must emit. Mirrors ``QuestionCreate`` (server-side)
# minus the persisted ids; the Architect parses/validates the model output
# against the blueprint after generation.
PAPER_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["questions"],
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "index",
                    "type",
                    "prompt",
                    "answer_key",
                    "topic",
                    "difficulty",
                    "max_marks",
                ],
                "properties": {
                    "index": {"type": "integer", "minimum": 0},
                    "type": {"enum": ["mcq", "short", "numerical"]},
                    "prompt": {"type": "string"},
                    "options": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                    },
                    "answer_key": {"type": "string"},
                    "topic": {"type": "string"},
                    "difficulty": {"type": "number", "minimum": 0, "maximum": 1},
                    "max_marks": {"type": "number", "exclusiveMinimum": 0},
                },
            },
        }
    },
}

_SYSTEM = (
    "You are the Architect agent in DRONA AI. Generate a fair, original exam "
    "paper. Return ONLY JSON matching the provided schema. Never repeat "
    "well-known textbook items verbatim. Calibrate difficulty to the blueprint "
    "distribution."
)

_USER_TEMPLATE = """Exam subject: {subject}
Blueprint:
  - Topics & counts: {topic_counts}
  - Difficulty mix: {difficulty_mix}
  - Question types: {types}
  - Total questions: {total_questions}
Uniqueness seed: {seed}
Constraints:
  - Produce EXACTLY {total_questions} questions, one JSON object per question.
  - The number of questions per topic MUST equal the blueprint topic counts.
  - MCQ: between {min_options} and {max_options} options, exactly one correct;
    the answer_key MUST equal exactly one option string. Plausible distractors.
  - Numerical: include units and a precise numeric answer_key.
  - Use the uniqueness seed to diversify wording & values while keeping the
    paper equivalent in fairness to other students' papers.
  - Avoid cultural/regional bias; use clear, exam-appropriate language.
Output JSON schema:
{schema}
"""


def build_architect_prompt(
    *,
    subject: str,
    blueprint: dict[str, Any],
    seed: str,
    min_options: int,
    max_options: int,
) -> str:
    """Render the full Architect prompt for one student's paper.

    ``blueprint`` is the exam blueprint as a plain dict (topics with counts,
    difficulty mix, question types, total count). ``seed`` is the per-student
    uniqueness seed (``hash(exam_id+student_id+nonce)``) that diversifies the
    output while the blueprint pins the distribution.
    """
    topics = blueprint.get("topics", [])
    topic_counts = ", ".join(
        f"{t.get('name')}={t.get('count')}" for t in topics
    ) or "(unspecified)"
    types = ", ".join(str(t) for t in blueprint.get("question_types", [])) or "mcq"
    user = _USER_TEMPLATE.format(
        subject=subject,
        topic_counts=topic_counts,
        difficulty_mix=json.dumps(blueprint.get("difficulty_mix", {})),
        types=types,
        total_questions=blueprint.get("total_questions"),
        seed=seed,
        min_options=min_options,
        max_options=max_options,
        schema=json.dumps(PAPER_OUTPUT_SCHEMA),
    )
    return f"{_SYSTEM}\n\n{user}"


__all__ = [
    "ARCHITECT_PROMPT_VERSION",
    "PAPER_OUTPUT_SCHEMA",
    "build_architect_prompt",
]
