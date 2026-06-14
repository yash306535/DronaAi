"""Unit tests for Sentinel feature handling (task 20.3).

Covers Requirement 8 behaviour:

- malformed-event rejection without updating features + error indication (8.7)
- missing-input handling: score from available inputs + an exclusion reason (8.8)
- server-recorded timestamp usage for timing features (8.6)
- threshold emission boundary: emit ``anomaly.detected`` iff score reaches the
  configured detection threshold (8.4)

The scoring engine is pure and dependency-free, so most cases drive
``score_event`` / ``SentinelAgent`` directly. A real ``EventBus`` collects
emitted events; no DB is needed (the agent runs without a repository factory and
emits an event with ``anomalyId == None``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.agents.sentinel import (
    MalformedEventError,
    SentinelAgent,
    SentinelConfig,
    SessionFeatures,
    SessionState,
    score_event,
    timing_z_score,
    update_features,
)
from app.core.events import Event, EventBus, EventType

_BASE_TS = datetime(2026, 6, 13, 9, 0, 0, tzinfo=timezone.utc)


def _ts(offset_seconds: float) -> str:
    return (_BASE_TS + timedelta(seconds=offset_seconds)).isoformat()


def _event(kind: str, offset_seconds: float = 0.0, **payload) -> dict:
    return {"kind": kind, "serverTs": _ts(offset_seconds), "payload": payload}


# --- 8.7: malformed-event rejection ----------------------------------------


@pytest.mark.parametrize(
    "bad_event, reason",
    [
        ({"serverTs": _ts(0)}, "missing_kind"),  # no kind
        ({"kind": "", "serverTs": _ts(0)}, "missing_kind"),
        ({"kind": "not_a_kind", "serverTs": _ts(0)}, "unknown_kind"),
        ({"kind": "paste"}, "missing_server_ts"),  # no serverTs
        ({"kind": "paste", "serverTs": "not-a-timestamp"}, "invalid_server_ts"),
        ({"kind": "paste", "serverTs": 12345}, "invalid_server_ts"),
        ("not-a-dict", "not_a_mapping"),
    ],
)
def test_malformed_event_raises_with_reason(bad_event, reason) -> None:
    """8.7: a malformed event is rejected with a descriptive reason."""
    state = SessionState(session_id="s1")
    with pytest.raises(MalformedEventError) as exc:
        score_event(state, bad_event, SentinelConfig())
    assert exc.value.reason == reason


def test_malformed_event_does_not_update_features_or_emit() -> None:
    """8.7: rejection leaves stored features unchanged and records an error."""
    agent = SentinelAgent(bus=None)
    # Seed some real state first via a valid event.
    agent.score("s1", _event("paste", 0.0))
    before = agent.state_for("s1").features
    assert before.paste_count == 1

    # A malformed event must not touch features and must record an error (8.7).
    import asyncio

    bad = Event(
        type=EventType.SESSION_EVENT,
        payload={"sessionId": "s1", "kind": "bogus", "serverTs": _ts(1)},
        source="session-service",
        session_id="s1",
    )
    asyncio.run(agent.on_session_event(bad))

    after = agent.state_for("s1").features
    assert after.paste_count == 1  # unchanged
    assert agent.error_count("s1") == 1  # error indication recorded


# --- 8.8: missing-input handling with an exclusion reason -------------------


def test_missing_inputs_excluded_with_reason() -> None:
    """8.8: with no timing/similarity yet, both are noted as excluded inputs."""
    state = SessionState(session_id="s1")
    result = score_event(state, _event("tab_blur", 0.0), SentinelConfig())

    assert "per-question timing" in result.excluded
    assert "cross-student answer similarity" in result.excluded
    # The excluded inputs are surfaced in a human-readable reason.
    assert any("excluded:" in r for r in result.reasons)
    assert "timing_anomaly" not in result.terms
    assert "answer_similarity" not in result.terms
    # Score still computed from the available inputs (8.8) and in range (8.2).
    assert 0.0 <= result.value <= 1.0


def test_available_similarity_input_is_not_excluded() -> None:
    """8.8: once a similarity input is present it contributes and is not excluded."""
    features = SessionFeatures(
        start_server_ts=_BASE_TS,
        last_server_ts=_BASE_TS,
        max_similarity=0.95,
    )
    state = SessionState(session_id="s1", features=features)
    result = score_event(state, _event("heartbeat", 0.0), SentinelConfig())

    assert "cross-student answer similarity" not in result.excluded
    assert result.terms["answer_similarity"] == pytest.approx(0.95)
    # Timing is still excluded (no per-question samples yet).
    assert "per-question timing" in result.excluded


# --- 8.6: server-recorded timestamp usage for timing ------------------------


def test_timing_derived_from_server_timestamps() -> None:
    """8.6: per-question time comes from the serverTs delta, not client_ts."""
    f0 = SessionFeatures()
    # First question_view starts the per-question clock (no sample yet).
    f1 = update_features(f0, _event("question_view", 0.0))
    assert f1.question_times_ms == []
    assert f1.last_question_view_ts == _BASE_TS

    # Second question_view 30s later yields a 30_000 ms sample from server ts.
    f2 = update_features(f1, _event("question_view", 30.0))
    assert f2.question_times_ms == [pytest.approx(30_000.0)]
    assert f2.last_question_time_ms == pytest.approx(30_000.0)


def test_client_ts_in_payload_is_ignored_for_timing() -> None:
    """8.6: a client-supplied timestamp on the payload never drives timing."""
    f0 = SessionFeatures()
    # Provide a misleading client ts in the payload; only serverTs is used.
    e1 = _event("question_view", 0.0, clientTs=_ts(9999))
    e2 = _event("question_view", 10.0, clientTs=_ts(0))
    f1 = update_features(f0, e1)
    f2 = update_features(f1, e2)
    # Derived purely from serverTs delta (10s), client ts ignored.
    assert f2.last_question_time_ms == pytest.approx(10_000.0)


def test_timing_z_score_flags_impossibly_fast_answer() -> None:
    """8.6/scoring: a much-faster-than-expected last answer scores high."""
    # Establish an expected ~30s per question with some spread, then a 0ms answer.
    features = SessionFeatures(
        start_server_ts=_BASE_TS,
        last_server_ts=_BASE_TS + timedelta(minutes=5),
        question_times_ms=[30_000.0, 32_000.0, 28_000.0, 100.0],
        last_question_time_ms=100.0,
    )
    z = timing_z_score(features)
    assert 0.0 <= z <= 1.0
    assert z > 0.0  # observed << expected ⇒ suspiciously fast


# --- 8.4: threshold emission boundary ---------------------------------------


def _drain_emit(agent: SentinelAgent, session_id: str, event: dict) -> list[Event]:
    import asyncio

    bus = agent.bus
    emitted: list[Event] = []

    async def _collect(e: Event) -> None:
        emitted.append(e)

    bus.subscribe(EventType.ANOMALY_DETECTED, _collect)
    bus_event = Event(
        type=EventType.SESSION_EVENT,
        payload={**event, "sessionId": session_id},
        source="session-service",
        session_id=session_id,
    )
    asyncio.run(agent.on_session_event(bus_event))
    return emitted


def test_no_emit_below_detection_threshold() -> None:
    """8.4: a score below the detection threshold emits nothing."""
    agent = SentinelAgent(bus=EventBus(), config=SentinelConfig(detection_threshold=0.99))
    emitted = _drain_emit(agent, "s1", _event("paste", 0.0))
    assert emitted == []


def test_emit_at_or_above_detection_threshold() -> None:
    """8.4: a score reaching the detection threshold emits anomaly.detected."""
    # Seed a session with a high cross-student similarity so a single event
    # produces a score >= a low detection threshold.
    agent = SentinelAgent(
        bus=EventBus(),
        config=SentinelConfig(detection_threshold=0.2),
    )
    # Feed a strong similarity signal so the score clears the threshold.
    agent.update_answer_similarity("s1", 1.0)
    emitted = _drain_emit(agent, "s1", _event("paste", 0.0, maxSimilarity=1.0))

    assert len(emitted) == 1
    payload = emitted[0].payload
    assert payload["sourceAgent"] == "sentinel"
    assert payload["confirmed"] is True  # self-confirmed behavioral anomaly
    assert payload["score"] >= 0.2
    assert payload["reasons"]  # contributing reasons included (8.4)
    assert payload["anomalyId"] is None  # no repo wired in this test


def test_emit_boundary_exactly_at_threshold() -> None:
    """8.4: the boundary is inclusive — score == threshold emits."""
    agent = SentinelAgent(bus=EventBus(), config=SentinelConfig(detection_threshold=0.25))
    # answer_similarity weight is 0.25; a similarity of 1.0 alone yields exactly
    # 0.25 when no other term contributes.
    features = SessionFeatures(start_server_ts=_BASE_TS, last_server_ts=_BASE_TS)
    agent.state_for("s1").features = features
    agent.update_answer_similarity("s1", 1.0)

    emitted = _drain_emit(agent, "s1", _event("heartbeat", 0.0, maxSimilarity=1.0))
    assert len(emitted) == 1
    assert emitted[0].payload["score"] == pytest.approx(0.25)
