"""Property test for answer-key confidentiality (task 10.4).

**Property 5: Answer-key confidentiality** — for all student-facing responses,
no ``answer_key`` field is ever serialized.

**Validates: Requirements 5.3, 14.1, 14.2**

The property is exercised against the serialization defence layer
(:mod:`app.services.serialization`) which sits behind the ``StudentPaper`` /
``StudentQuestion`` schemas as the last line of defence:

- For arbitrary nested payloads that may embed answer-key fields at any depth,
  :func:`strip_answer_key_fields` removes every such field, and the resulting
  payload passes :func:`guard_student_payload` without raising.
- For arbitrary clean payloads (no answer-key field), the guard is a no-op and
  returns the payload unchanged.
- Whenever an answer-key field survives to transmission, the pre-transmission
  guard blocks it by raising :class:`SerializationIntegrityError` (14.2).
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.services.serialization import (
    FORBIDDEN_KEY_TERMS,
    SerializationIntegrityError,
    guard_student_payload,
    is_answer_key_field,
    strip_answer_key_fields,
)

# Keys that look like answer-key fields (various separators/casings) and keys
# that are definitely safe.
_answer_key_names = st.sampled_from(
    [
        "answer_key",
        "answerKey",
        "answer-key",
        "ANSWER_KEY",
        "answer",
        "correctAnswer",
        "correct_option",
        "scoring_key",
        "solution",
        "solutions",
    ]
)
_safe_keys = st.sampled_from(
    ["id", "prompt", "options", "topic", "index", "max_marks", "questions", "exam_id"]
)

# Bounded JSON-ish leaf values.
_leaves = st.one_of(
    st.text(max_size=8),
    st.integers(min_value=-100, max_value=100),
    st.booleans(),
    st.none(),
)


def _clean_payloads():
    """Recursive strategy for payloads that contain NO answer-key field."""
    return st.recursive(
        _leaves,
        lambda children: st.one_of(
            st.lists(children, max_size=4),
            st.dictionaries(_safe_keys, children, max_size=4),
        ),
        max_leaves=12,
    )


def _payloads_with_answer_keys():
    """Strategy for dict payloads that embed at least one answer-key field."""
    return st.dictionaries(
        st.one_of(_safe_keys, _answer_key_names),
        _clean_payloads(),
        min_size=1,
        max_size=5,
    ).filter(lambda d: any(is_answer_key_field(k) for k in d))


@given(payload=_clean_payloads())
def test_clean_student_payloads_pass_the_guard_unchanged(payload) -> None:
    """A payload with no answer-key field passes the guard unchanged (14.1)."""
    assert guard_student_payload(payload) == payload


@given(payload=_payloads_with_answer_keys())
def test_stripping_then_guarding_never_leaks_answer_key(payload) -> None:
    """Stripping removes every answer-key field so the guard then passes (14.1)."""
    stripped = strip_answer_key_fields(payload)
    # No top-level forbidden key survives.
    assert not any(is_answer_key_field(k) for k in stripped)
    # And the pre-transmission guard accepts the stripped payload.
    assert guard_student_payload(stripped) == stripped


@given(payload=_payloads_with_answer_keys())
def test_guard_blocks_any_payload_still_containing_answer_key(payload) -> None:
    """A payload still carrying an answer-key field is blocked (14.2)."""
    with pytest.raises(SerializationIntegrityError):
        guard_student_payload(payload)


@given(
    key=_answer_key_names,
    value=_leaves,
    depth=st.integers(min_value=0, max_value=4),
)
def test_answer_key_blocked_at_any_nesting_depth(key, value, depth) -> None:
    """An answer-key field nested at any depth is detected and blocked (14.1/14.2)."""
    payload: object = {key: value}
    for _ in range(depth):
        payload = {"questions": [payload]}
    with pytest.raises(SerializationIntegrityError):
        guard_student_payload(payload)
    # Once stripped, the same structure is clean.
    assert guard_student_payload(strip_answer_key_fields(payload)) is not None


def test_forbidden_terms_cover_the_documented_synonyms() -> None:
    """Sanity: the documented synonyms are all recognized as answer-key fields."""
    for name in ["answer_key", "correct_answer", "scoring_key", "solution"]:
        assert is_answer_key_field(name)
    assert "answerkey" in FORBIDDEN_KEY_TERMS
