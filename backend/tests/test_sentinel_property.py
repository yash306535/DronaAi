"""Property-based test for Sentinel explainable scoring (task 20.2).

**Property 3: Score bounds & monotonic reasons** — for all events
``0 <= score <= 1`` and every returned reason corresponds to a term whose
normalized contribution is ``>= reason_threshold``.

**Validates: Requirements 8.2, 8.3**

The Sentinel scoring engine (:func:`app.agents.sentinel.score_event`) is pure
and dependency-free, so the property is asserted directly against it over a
wide range of synthesized sessions: arbitrary prior feature state plus an
arbitrary (well-formed) ``session.event`` and an arbitrary reason threshold.
No I/O, no DB, no event bus is involved.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from app.agents.sentinel import (
    WEIGHTS,
    SentinelConfig,
    SessionFeatures,
    SessionState,
    score_event,
)

_BASE_TS = datetime(2026, 6, 13, 9, 0, 0, tzinfo=timezone.utc)

# The substring every "excluded inputs" reason starts with; it is a bookkeeping
# note (8.8) rather than a per-term contribution reason, so the monotonic-reason
# check skips it.
_EXCLUDED_PREFIX = "Score computed from available inputs"


@st.composite
def session_features(draw):
    """Synthesize an arbitrary prior feature state for a session."""
    minutes = draw(st.floats(min_value=0.0, max_value=180.0, allow_nan=False))
    start = _BASE_TS
    last = _BASE_TS + timedelta(minutes=minutes)
    times = draw(
        st.lists(
            st.floats(min_value=0.0, max_value=600_000.0, allow_nan=False),
            max_size=20,
        )
    )
    has_similarity = draw(st.booleans())
    similarity = (
        draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
        if has_similarity
        else None
    )
    last_q_time = times[-1] if times else None
    return SessionFeatures(
        tab_switches=draw(st.integers(min_value=0, max_value=500)),
        paste_count=draw(st.integers(min_value=0, max_value=500)),
        start_server_ts=start,
        last_server_ts=last,
        last_question_view_ts=last,
        question_times_ms=times,
        last_question_time_ms=last_q_time,
        max_similarity=similarity,
    )


@st.composite
def well_formed_events(draw):
    """Synthesize an arbitrary well-formed ``session.event`` dict."""
    kind = draw(
        st.sampled_from(
            [
                "tab_blur",
                "tab_focus",
                "paste",
                "copy",
                "answer_change",
                "question_view",
                "heartbeat",
            ]
        )
    )
    offset_ms = draw(st.integers(min_value=0, max_value=600_000))
    server_ts = (_BASE_TS + timedelta(milliseconds=offset_ms)).isoformat()
    payload: dict = {}
    if draw(st.booleans()):
        payload["timeSpentMs"] = draw(
            st.floats(min_value=0.0, max_value=600_000.0, allow_nan=False)
        )
    if draw(st.booleans()):
        payload["maxSimilarity"] = draw(
            st.floats(min_value=0.0, max_value=1.0, allow_nan=False)
        )
    return {"kind": kind, "serverTs": server_ts, "payload": payload}


@settings(max_examples=300, deadline=None)
@given(
    features=session_features(),
    event=well_formed_events(),
    reason_threshold=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)
def test_score_bounds_and_monotonic_reasons(
    features, event, reason_threshold
) -> None:
    """Property 3: score in [0,1] and each reason maps to a term >= threshold."""
    config = SentinelConfig(reason_threshold=reason_threshold)
    state = SessionState(session_id="sess-prop", features=features)

    result = score_event(state, event, config)

    # 8.2: the score is always within the inclusive [0, 1] range.
    assert 0.0 <= result.value <= 1.0

    # Every per-term contribution is itself a normalized [0, 1] value.
    for key, value in result.terms.items():
        assert key in WEIGHTS
        assert 0.0 <= value <= 1.0

    # 8.3: every contributing-term reason corresponds to a term whose normalized
    # contribution is >= reason_threshold. The trailing "excluded inputs" note
    # (8.8) is bookkeeping, not a term contribution, so it is exempt.
    contributing_reasons = [
        r for r in result.reasons if not r.startswith(_EXCLUDED_PREFIX)
    ]
    qualifying_terms = [
        k for k, v in result.terms.items() if v >= reason_threshold
    ]
    # Exactly one reason per qualifying term (monotonic: no reason without a
    # qualifying term, no qualifying term without a reason).
    assert len(contributing_reasons) == len(qualifying_terms)

    # And the converse: no contributing reason exists when no term qualifies.
    if not qualifying_terms:
        assert contributing_reasons == []
