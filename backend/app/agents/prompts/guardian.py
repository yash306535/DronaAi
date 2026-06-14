"""Versioned Guardian Stage-2 vision prompt template.

The Guardian escalates a single captured frame to OpenAI Vision *only* when a
Stage-1 local anomaly is detected (Requirement 7.2). This prompt is tightly
scoped to the design's *Stage 2 — Cloud Confirmation* contract: it asks the
model for a structured verdict (presence, face count, secondary person, looking
away, plus an overall ``anomalous`` flag, ``category`` label, ``confidence``,
and short ``reasons``) and nothing else, so the Guardian can parse and act on it
deterministically.

Keeping the template here (rather than inline in the agent) means the prompt can
evolve without touching agent logic, and tests can assert the local signal is
faithfully injected. It is versioned (:data:`GUARDIAN_PROMPT_VERSION`) so a
change to the wording is traceable.
"""

from __future__ import annotations

import json
from typing import Any

# Bump when the prompt wording/schema changes in a behavior-affecting way.
GUARDIAN_PROMPT_VERSION = "v1"

# The JSON schema the model must emit. The Guardian parses the model output into
# a ``VisionVerdict`` (see ``app.agents.vision.parse_verdict``).
VISION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["anomalous", "category", "confidence", "reasons"],
    "properties": {
        "present": {"type": "boolean"},
        "face_count": {"type": "integer", "minimum": 0},
        "secondary_person": {"type": "boolean"},
        "looking_away": {"type": "boolean"},
        "anomalous": {"type": "boolean"},
        "category": {
            "enum": [
                "face_absent",
                "multiple_faces",
                "gaze_away",
                "benign",
            ]
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
}

_SYSTEM = (
    "You are the Guardian agent in DRONA AI, an exam proctoring system. You are "
    "given a single webcam frame captured from a student's exam session because "
    "a local screener flagged a possible integrity issue. Determine, "
    "authoritatively and conservatively, whether the frame actually shows a "
    "proctoring violation. Report ONLY JSON matching the provided schema. Do not "
    "describe the person's appearance, identity, or any attribute beyond what is "
    "needed for the verdict."
)

_USER_TEMPLATE = """A local Stage-1 screener flagged this frame.
Local signal:
  - Kind: {kind}
  - Duration (ms): {duration_ms}
  - Local confidence: {confidence_local}
Decide the authoritative verdict for THIS frame:
  - present: is exactly one person's face clearly present?
  - face_count: how many distinct human faces are visible?
  - secondary_person: is there a second person besides the test-taker?
  - looking_away: is the test-taker's gaze/head turned away from the screen?
  - anomalous: true ONLY if the frame genuinely shows a violation
    (no face present, more than one face, or clearly looking away); false if the
    frame is benign and the local signal was a false positive.
  - category: one of face_absent | multiple_faces | gaze_away | benign.
  - confidence: your confidence in the verdict, 0.0 to 1.0.
  - reasons: 1-3 short, factual reasons for the verdict.
Be conservative: if the frame is ambiguous or clearly normal, return benign.
Output JSON schema:
{schema}
"""


def build_guardian_prompt(
    *,
    kind: str,
    duration_ms: float | int | None = None,
    confidence_local: float | None = None,
) -> str:
    """Render the full Guardian vision prompt for one escalated frame.

    ``kind`` is the Stage-1 local signal kind (``face_absent`` /
    ``multiple_faces`` / ``gaze_away``); ``duration_ms`` and ``confidence_local``
    are the debounced local signal's persistence and local confidence, injected
    so the model has the context that triggered the escalation.
    """
    user = _USER_TEMPLATE.format(
        kind=kind or "unknown",
        duration_ms="(unspecified)" if duration_ms is None else duration_ms,
        confidence_local=(
            "(unspecified)" if confidence_local is None else confidence_local
        ),
        schema=json.dumps(VISION_OUTPUT_SCHEMA),
    )
    return f"{_SYSTEM}\n\n{user}"


__all__ = [
    "GUARDIAN_PROMPT_VERSION",
    "VISION_OUTPUT_SCHEMA",
    "build_guardian_prompt",
]
