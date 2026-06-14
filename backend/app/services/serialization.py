"""Answer-key-safe serialization for student-facing responses.

This module is the last line of defence behind the schema layer for
answer-key confidentiality (Requirements 4.9, 5.3, 14.1, 14.2). The
``StudentPaper`` / ``StudentQuestion`` schemas already *omit* the answer key by
construction, so under normal operation a student response never carries one.
This module adds two defence-in-depth mechanisms so a future regression (a new
field, a hand-built dict, an ORM object leaking through) cannot quietly ship an
answer key to a student:

- :func:`strip_answer_key_fields` — recursively removes every answer-key field
  (``answer_key`` and the other forbidden synonyms) from an arbitrary payload
  (dict / list / nested) before it is serialized. This satisfies the
  "exclude every answer-key field from serialization" rule (14.1) regardless of
  how the payload was assembled.
- :func:`guard_student_payload` — a *pre-transmission guard* (14.2). It inspects
  the outgoing payload and, if any answer-key field is still present, raises a
  :class:`SerializationIntegrityError` so the response is blocked rather than
  transmitted. The forbidden field name is named in the safe log/error output;
  the field *value* is never logged or echoed.

Both helpers operate on plain Python structures (``dict``/``list``/scalars) so
they compose with FastAPI's response model serialization and with manual
``jsonable_encoder`` output alike.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.core.errors import AppError
from app.core.logging import get_logger

logger = get_logger("app.services.serialization")

# The canonical answer-key field plus the synonyms the requirement enumerates:
# a "correct-answer, scoring-key, or solution field" (Requirement 5.3). Matching
# is case-insensitive and ignores separators so ``answerKey``/``answer-key``/
# ``ANSWER_KEY`` are all treated as the same forbidden field.
FORBIDDEN_KEY_TERMS: frozenset[str] = frozenset(
    {
        "answerkey",
        "answer",  # bare "answer" as a key is a correct-answer field
        "correctanswer",
        "correctoption",
        "scoringkey",
        "solution",
        "solutions",
        "answerkeys",
    }
)

SERIALIZATION_INTEGRITY_CODE = "serialization_integrity_failure"


class SerializationIntegrityError(AppError):
    """Raised when a student-facing payload still contains an answer-key field.

    Mapped to HTTP 500 (a server-side integrity failure, not a client error):
    the system blocks transmission of the offending response (Requirement 14.2)
    rather than risk leaking an answer key. The error ``message``/``details``
    name the offending field only — never its value.
    """

    status_code = 500
    code = SERIALIZATION_INTEGRITY_CODE


def _normalize_key(key: Any) -> str:
    """Normalize a mapping key for forbidden-field matching.

    Lower-cases the key and strips the common separators (``_``, ``-``, spaces)
    so ``answer_key``, ``answer-key``, ``answerKey`` and ``Answer Key`` all
    collapse to the same comparable token.
    """
    text = str(key).lower()
    for sep in ("_", "-", " "):
        text = text.replace(sep, "")
    return text


def is_answer_key_field(key: Any) -> bool:
    """Return ``True`` if ``key`` names a forbidden answer-key field."""
    return _normalize_key(key) in FORBIDDEN_KEY_TERMS


def strip_answer_key_fields(payload: Any) -> Any:
    """Return ``payload`` with every answer-key field recursively removed (14.1).

    Dicts are rebuilt without forbidden keys; lists/tuples are mapped element by
    element; scalars pass through. The input is not mutated. Strings and bytes
    are treated as scalars (not iterated as sequences).
    """
    if isinstance(payload, Mapping):
        return {
            key: strip_answer_key_fields(value)
            for key, value in payload.items()
            if not is_answer_key_field(key)
        }
    if isinstance(payload, (str, bytes)):
        return payload
    if isinstance(payload, Sequence):
        return [strip_answer_key_fields(item) for item in payload]
    return payload


def _find_answer_key_field(payload: Any) -> str | None:
    """Return the first answer-key field name found in ``payload``, else ``None``.

    Recurses through dicts and sequences. Returns the *original* offending key
    (so the safe error/log can name it) without ever touching its value.
    """
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if is_answer_key_field(key):
                return str(key)
            found = _find_answer_key_field(value)
            if found is not None:
                return found
        return None
    if isinstance(payload, (str, bytes)):
        return None
    if isinstance(payload, Sequence):
        for item in payload:
            found = _find_answer_key_field(item)
            if found is not None:
                return found
    return None


def guard_student_payload(payload: Any) -> Any:
    """Block transmission of a student payload containing an answer key (14.2).

    Inspects ``payload`` and, if any answer-key field is still present, logs a
    safe warning naming the field (never its value) and raises
    :class:`SerializationIntegrityError` so the response is not transmitted.
    Returns ``payload`` unchanged when it is clean, so this can wrap a response
    inline: ``return guard_student_payload(body)``.
    """
    offending = _find_answer_key_field(payload)
    if offending is not None:
        # Name the field only; the value is deliberately excluded from logs
        # and from the error envelope (Requirement 14.2 / 15.1).
        logger.error(
            "serialization.answer_key_leak_blocked",
            extra={"field": offending},
        )
        raise SerializationIntegrityError(
            "Response blocked: a student-facing payload contained an "
            "answer-key field and was not transmitted.",
            details={"field": offending},
        )
    return payload


__all__ = [
    "FORBIDDEN_KEY_TERMS",
    "SERIALIZATION_INTEGRITY_CODE",
    "SerializationIntegrityError",
    "is_answer_key_field",
    "strip_answer_key_fields",
    "guard_student_payload",
]
