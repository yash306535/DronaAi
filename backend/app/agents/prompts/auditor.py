"""Versioned Auditor prompt template (question fairness audit).

The Auditor reviews each generated question against three fairness dimensions
(Requirement 13.1):

- **cultural_bias** — region-, gender-, religion-, or socioeconomic-specific
  references that are not required by the subject.
- **difficulty_calibration** — deviation of the question's assessed difficulty
  from its declared target difficulty level.
- **language_clarity** — ambiguous phrasing, undefined terms, or multiple
  defensible correct answers.

For each question the model assigns each dimension a ``pass``/``fail`` result,
where ``fail`` means at least one issue of that dimension was detected
(Requirement 13.2), along with a short description of each detected issue
(Requirement 13.5). The Auditor agent derives the overall verdict from these
per-dimension results in deterministic Python — the model only judges the
individual dimensions.

Keeping the template here (rather than inline in the agent) lets the prompt
evolve without touching agent logic, lets the audit record which prompt version
produced it, and lets tests assert the questions are faithfully injected.
"""

from __future__ import annotations

import json
from typing import Any

# Bump when the prompt wording/schema changes in a behavior-affecting way.
AUDITOR_PROMPT_VERSION = "v1"

# The three fairness dimensions, in canonical order. The agent and tests import
# these so the dimension keys stay in agreement across prompt and logic.
DIMENSIONS: tuple[str, ...] = (
    "cultural_bias",
    "difficulty_calibration",
    "language_clarity",
)

# The JSON schema the model must emit: a mapping of question id -> per-dimension
# {result: pass|fail, issue: "..."}. ``issue`` should be empty when the
# dimension passes and a one-line description when it fails (Requirement 13.5).
AUDIT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["questions"],
    "properties": {
        "questions": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "required": list(DIMENSIONS),
                "properties": {
                    dim: {
                        "type": "object",
                        "required": ["result"],
                        "properties": {
                            "result": {"enum": ["pass", "fail"]},
                            "issue": {"type": "string"},
                        },
                    }
                    for dim in DIMENSIONS
                },
            },
        }
    },
}

_SYSTEM = (
    "You are the Auditor agent in DRONA AI. You review exam questions for "
    "fairness before they reach students. For EACH question you must judge "
    "three independent dimensions and return ONLY JSON matching the provided "
    "schema:\n"
    "  - cultural_bias: FAIL if the question relies on region-, gender-, "
    "religion-, or socioeconomic-specific references not required by the "
    "subject; otherwise PASS.\n"
    "  - difficulty_calibration: FAIL if the question's actual difficulty "
    "clearly deviates from its declared target difficulty; otherwise PASS.\n"
    "  - language_clarity: FAIL if the phrasing is ambiguous, uses undefined "
    "terms, or admits multiple defensible correct answers; otherwise PASS.\n"
    "When a dimension is FAIL, include a one-sentence 'issue' describing the "
    "detected problem. When PASS, 'issue' may be empty."
)

_USER_TEMPLATE = """Exam subject: {subject}
Questions to audit (difficulty is a 0.0-1.0 target; higher is harder):
{questions}

Instructions:
  - For EVERY question id listed above, return an entry with all three
    dimensions (cultural_bias, difficulty_calibration, language_clarity), each
    marked "pass" or "fail".
  - A dimension is "fail" if at least one issue of that dimension is detected.
  - Include a short 'issue' description for every failing dimension.
Output JSON schema:
{schema}
"""


def build_auditor_prompt(
    *,
    subject: str,
    questions: list[dict[str, Any]],
) -> str:
    """Render the Auditor fairness-review prompt for one paper.

    ``questions`` is a list of small dicts describing each question (id, prompt,
    topic, declared difficulty, and options for MCQs) so the model can judge the
    three fairness dimensions per question (Requirement 13.1). The agent derives
    the overall verdict from the model's per-dimension results.
    """
    return f"{_SYSTEM}\n\n" + _USER_TEMPLATE.format(
        subject=subject or "(unspecified)",
        questions=json.dumps(questions, indent=2, sort_keys=True),
        schema=json.dumps(AUDIT_OUTPUT_SCHEMA),
    )


__all__ = [
    "AUDITOR_PROMPT_VERSION",
    "DIMENSIONS",
    "AUDIT_OUTPUT_SCHEMA",
    "build_auditor_prompt",
]
