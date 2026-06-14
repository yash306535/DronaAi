"""Versioned Analyst prompt template (post-exam improvement suggestions).

The Analyst aggregates exam results deterministically in Python (score
distribution, mean, anomaly count, difficulty heatmap — Requirements 10.2/10.3).
The *only* part that uses the language model is the per-student improvement
narrative (Requirement 10.4): given each student's deterministic results, the
model returns at least one actionable suggestion per student.

Keeping the template here (rather than inline in the agent) means the prompt can
evolve without touching agent logic, the produced report can record which prompt
version generated it, and tests can assert the results are faithfully injected.
"""

from __future__ import annotations

import json
from typing import Any

# Bump when the prompt wording/schema changes in a behavior-affecting way.
ANALYST_PROMPT_VERSION = "v1"

# The JSON schema the model must emit: a mapping of student id -> a non-empty
# list of short, actionable improvement suggestions derived from that student's
# results (Requirement 10.4).
SUGGESTIONS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["students"],
    "properties": {
        "students": {
            "type": "object",
            "additionalProperties": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
        }
    },
}

_SYSTEM = (
    "You are the Analyst agent in DRONA AI. You turn completed exam results into "
    "concise, actionable, encouraging improvement suggestions for each student. "
    "Return ONLY JSON matching the provided schema. Every student MUST receive at "
    "least one suggestion derived strictly from their own results. Keep each "
    "suggestion to a single sentence and reference the student's weaker topics."
)

_USER_TEMPLATE = """Exam subject: {subject}
Per-student results (topic accuracy is a percentage 0-100):
{student_results}

Instructions:
  - For EACH student id listed above, produce at least one improvement
    suggestion derived from that student's own topic accuracies and overall
    score. Prioritize the student's weakest topics.
  - Keep each suggestion to one clear, encouraging sentence.
  - Do NOT invent topics that are not in that student's results.
Output JSON schema:
{schema}
"""


def build_analyst_prompt(
    *,
    subject: str,
    student_results: dict[str, Any],
) -> str:
    """Render the Analyst suggestions prompt for one exam.

    ``student_results`` maps each completed student's id to a small dict of that
    student's deterministic results (overall score percent and per-topic
    accuracy) so the model can ground its suggestions in the student's own data
    (Requirement 10.4).
    """
    return f"{_SYSTEM}\n\n" + _USER_TEMPLATE.format(
        subject=subject or "(unspecified)",
        student_results=json.dumps(student_results, indent=2, sort_keys=True),
        schema=json.dumps(SUGGESTIONS_OUTPUT_SCHEMA),
    )


__all__ = [
    "ANALYST_PROMPT_VERSION",
    "SUGGESTIONS_OUTPUT_SCHEMA",
    "build_analyst_prompt",
]
