"""Sentinel agent: explainable behavioral fraud detection (Requirement 8).

The Sentinel turns raw session telemetry into a transparent, *explainable*
anomaly score. There is no black box: the score is a weighted sum of normalized
behavioral features, and every contributing term becomes a human-readable
reason (design "Sentinel — Explainable Behavioral Scoring").

Scoring (design low-level section):

    WEIGHTS = {tab_switch_rate: .25, paste_events: .30,
               timing_anomaly: .20, answer_similarity: .25}

    terms = {
        "tab_switch_rate":   norm(tab_switches / max(minutes, 1), cap=6),
        "paste_events":      norm(paste_count, cap=3),
        "timing_anomaly":    timing_z_score(features),
        "answer_similarity": max_similarity,            # cross-student cosine
    }
    score   = clamp(sum(WEIGHTS[k] * v for k, v in terms), 0, 1)
    reasons = [explain(k, v) for k, v in terms if v >= reason_threshold]

:func:`score_event` is **pure** with respect to the session state it is given:
it derives a fresh, updated :class:`SessionFeatures` from the prior state plus
the new event and returns it inside the :class:`AnomalyScore`; it performs no
I/O and never mutates its arguments (Requirement 8.1, the design's
"Postconditions").

Behavioral semantics:

- **8.1** :meth:`SentinelAgent.on_session_event` updates the per-session
  features and recomputes the score on every valid event (O(1) per event, well
  within the 2s budget for in-process work).
- **8.2** Every score is clamped to the inclusive ``[0, 1]`` range.
- **8.3** A reason is emitted for every term whose *normalized contribution*
  (the term value ``v``, already in ``[0, 1]``) is ``>= reason_threshold``.
- **8.4** When the score reaches ``detection_threshold`` the agent emits an
  ``anomaly.detected`` event carrying the score and the contributing reasons.
- **8.5** Features are derived from tab-switch count, paste events, per-question
  timing, and cross-student answer similarity.
- **8.6** Timing features use the **server-recorded** timestamp (``serverTs``),
  never the client-supplied one.
- **8.7** A malformed event (missing ``kind``/``serverTs`` or an unrecognized
  kind/timestamp) is rejected *without* updating features and an error
  indication is recorded.
- **8.8** When a feature input is unavailable (no per-question timing yet, or no
  cross-student similarity computed yet) the score is computed from the
  available inputs and a reason notes which inputs were excluded.

Sentinel-emitted anomalies feed the Herald. Behavioral anomalies are
*self-confirmed*: a score crossing the configured detection threshold is the
authoritative signal (there is no second-stage confirmation as there is for
Guardian's vision pipeline). The agent therefore emits ``confirmed=true`` so the
Herald broadcasts them — consistent with Requirement 9.2/9.3, which only gate
broadcasting on the ``confirmed`` flag for *guardian-sourced* anomalies.

Cross-student answer similarity is computed orchestrator-side by batching
pairwise comparisons *per question* (design "Answer-similarity"): answers are
vectorized (TF-IDF for free-text, an exact-match vector for MCQ), and the
pairwise cosine similarity above a threshold contributes to both sessions'
``answer_similarity`` feature. Batching per question keeps the cost bounded
rather than all-pairwise on every event.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime

from app.core.events import Event, EventBus, EventType
from app.core.logging import get_logger
from app.models.enums import AnomalyCategory, QuestionType, SourceAgent
from app.models.orm import Anomaly
from app.repositories.anomaly import AnomalyRepository

logger = get_logger("app.agents.sentinel")

# Stable source name used on emitted events / persisted anomalies. Matches
# ``SourceAgent.SENTINEL`` ("sentinel") so the Herald's source handling and the
# persisted ``source_agent`` column agree.
SENTINEL_SOURCE = SourceAgent.SENTINEL.value

# --- scoring weights (design low-level section) -----------------------------
WEIGHTS: dict[str, float] = {
    "tab_switch_rate": 0.25,
    "paste_events": 0.30,
    "timing_anomaly": 0.20,
    "answer_similarity": 0.25,
}

# Feature normalization caps (design: ``norm(x, cap)``).
TAB_SWITCH_RATE_CAP = 6.0  # tab switches per minute mapping to 1.0
PASTE_EVENTS_CAP = 3.0  # paste events mapping to 1.0

# timing_z_score guards (design ALGORITHM timing_z_score).
MIN_SIGMA_MS = 1000.0  # divide-by-zero guard on the per-question std dev (ms)
Z_CAP = 3.0  # |z| at/above this maps to a full 1.0 timing contribution

# Per-question pairwise cosine at/above this contributes to answer_similarity.
DEFAULT_SIMILARITY_THRESHOLD = 0.85

# The kinds the Sentinel knows how to fold into features. Other recognized kinds
# (heartbeat, copy, tab_focus) are valid events that simply advance the session
# clock without adding a suspicion signal.
KIND_TAB_BLUR = "tab_blur"
KIND_PASTE = "paste"
KIND_QUESTION_VIEW = "question_view"
KIND_ANSWER_CHANGE = "answer_change"

# Recognized event kinds (mirrors SessionEventKind values). Anything outside
# this set is treated as malformed (8.7).
_VALID_KINDS = frozenset(
    {
        "tab_blur",
        "tab_focus",
        "paste",
        "copy",
        "answer_change",
        "question_view",
        "heartbeat",
    }
)


class MalformedEventError(ValueError):
    """A ``session.event`` is malformed or missing required fields (8.7).

    Carries a short, safe ``reason`` describing the rejection (e.g.
    ``missing_kind``, ``unknown_kind``, ``missing_server_ts``,
    ``invalid_server_ts``). The reason never echoes untrusted payload content
    verbatim beyond a minimal descriptor.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# --- pure numeric helpers ---------------------------------------------------


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp ``value`` into the inclusive ``[lo, hi]`` range."""
    return max(lo, min(hi, value))


def norm(value: float, cap: float) -> float:
    """Normalize ``value`` onto ``[0, 1]`` by dividing by ``cap`` then clamping."""
    if cap <= 0:
        return 0.0
    return clamp(value / cap, 0.0, 1.0)


# --- session feature state --------------------------------------------------


@dataclass(slots=True)
class SessionFeatures:
    """Incrementally-maintained behavioral features for one session.

    All counters default to a known-zero baseline so a fresh session has
    well-defined features. ``question_times_ms`` accumulates observed
    per-question durations (derived from server timestamps, 8.6) from which the
    expected per-question time and its standard deviation are computed.
    ``max_similarity`` is ``None`` until the orchestrator-side batched
    cross-student comparison feeds a value (so it can be reported as an excluded
    input per 8.8).
    """

    tab_switches: int = 0
    paste_count: int = 0
    start_server_ts: datetime | None = None
    last_server_ts: datetime | None = None
    # Server ts of the most recent question_view, used to derive the time spent
    # on the *previous* question from server timestamps (8.6).
    last_question_view_ts: datetime | None = None
    # Observed per-question durations in milliseconds.
    question_times_ms: list[float] = field(default_factory=list)
    last_question_time_ms: float | None = None
    # Cross-student max cosine similarity (fed by the orchestrator-side batch).
    max_similarity: float | None = None

    def copy(self) -> "SessionFeatures":
        """Return a deep-ish copy (the only mutable member is the times list)."""
        return replace(self, question_times_ms=list(self.question_times_ms))

    @property
    def minutes(self) -> float:
        """Elapsed session minutes between the first and last observed events."""
        if self.start_server_ts is None or self.last_server_ts is None:
            return 0.0
        seconds = (self.last_server_ts - self.start_server_ts).total_seconds()
        return max(seconds / 60.0, 0.0)

    @property
    def expected_time_per_question_ms(self) -> float | None:
        """Calibrated expected per-question time = mean of observed durations."""
        if not self.question_times_ms:
            return None
        return sum(self.question_times_ms) / len(self.question_times_ms)

    @property
    def time_std_dev_ms(self) -> float:
        """Population standard deviation of observed per-question durations."""
        n = len(self.question_times_ms)
        if n < 2:
            return 0.0
        mean = sum(self.question_times_ms) / n
        variance = sum((t - mean) ** 2 for t in self.question_times_ms) / n
        return math.sqrt(variance)

    @property
    def has_timing(self) -> bool:
        """Whether a per-question timing input is available (8.8)."""
        return (
            self.last_question_time_ms is not None
            and self.expected_time_per_question_ms is not None
        )

    @property
    def has_similarity(self) -> bool:
        """Whether a cross-student answer-similarity input is available (8.8)."""
        return self.max_similarity is not None


@dataclass(slots=True)
class SessionState:
    """Per-session Sentinel state: the current features plus the last score."""

    session_id: str
    features: SessionFeatures = field(default_factory=SessionFeatures)
    last_score: float = 0.0


@dataclass(slots=True)
class SentinelConfig:
    """Tunable Sentinel thresholds (each configurable within ``[0, 1]``)."""

    # 8.3: emit a reason for every term whose normalized contribution >= this.
    reason_threshold: float = 0.30
    # 8.4: emit anomaly.detected when the score reaches this.
    detection_threshold: float = 0.50
    # Per-question pairwise cosine at/above this feeds answer_similarity.
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD


@dataclass(slots=True)
class AnomalyScore:
    """The result of scoring one event (Requirement 8.2/8.3).

    ``value`` is the clamped ``[0, 1]`` score; ``reasons`` explains every
    contributing term (and notes any excluded inputs, 8.8); ``features`` is the
    *updated* feature state (the function is pure, so the caller decides whether
    to adopt it); ``terms`` is the per-term normalized contribution map;
    ``excluded`` lists feature inputs that were unavailable; ``category`` is the
    dominant contributing category.
    """

    value: float
    reasons: list[str]
    features: SessionFeatures
    terms: dict[str, float] = field(default_factory=dict)
    excluded: list[str] = field(default_factory=list)
    category: AnomalyCategory = AnomalyCategory.TAB_SWITCH


# --- event parsing / feature update -----------------------------------------


def _parse_server_ts(raw: object) -> datetime:
    """Parse an authoritative server timestamp (ISO-8601 or datetime) (8.6).

    Raises :class:`MalformedEventError` when absent or unparseable so the event
    is rejected without updating features (8.7).
    """
    if raw is None:
        raise MalformedEventError("missing_server_ts")
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError as exc:
            raise MalformedEventError("invalid_server_ts") from exc
    raise MalformedEventError("invalid_server_ts")


def validate_event(event: dict) -> tuple[str, datetime, dict]:
    """Validate a session event and return ``(kind, server_ts, payload)`` (8.7).

    A non-dict event, a missing/unknown ``kind``, or a missing/invalid
    ``serverTs`` is rejected with :class:`MalformedEventError`.
    """
    if not isinstance(event, dict):
        raise MalformedEventError("not_a_mapping")
    kind = event.get("kind")
    if not kind or not isinstance(kind, str):
        raise MalformedEventError("missing_kind")
    if kind not in _VALID_KINDS:
        raise MalformedEventError("unknown_kind")
    server_ts = _parse_server_ts(event.get("serverTs"))
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        raise MalformedEventError("invalid_payload")
    return kind, server_ts, payload


def update_features(
    features: SessionFeatures, event: dict
) -> SessionFeatures:
    """Fold a validated event into a *fresh* copy of ``features`` (pure, 8.1).

    Increments tab-switch / paste counters, advances the session clock from the
    authoritative ``serverTs`` (8.6), derives per-question timing from server
    timestamps (or an explicit ``timeSpentMs`` payload), and folds an optional
    pre-computed ``maxSimilarity`` into the similarity feature. The input
    ``features`` is never mutated.
    """
    kind, server_ts, payload = validate_event(event)
    f = features.copy()

    # Advance the session clock from the authoritative server timestamp (8.6).
    if f.start_server_ts is None:
        f.start_server_ts = server_ts
    if f.last_server_ts is None or server_ts >= f.last_server_ts:
        f.last_server_ts = server_ts

    if kind == KIND_TAB_BLUR:
        f.tab_switches += 1
    elif kind == KIND_PASTE:
        f.paste_count += 1
    elif kind == KIND_QUESTION_VIEW:
        # The time spent on the previous question is the server-timestamp delta
        # between the previous question_view and this one (8.6).
        if f.last_question_view_ts is not None:
            delta_ms = (server_ts - f.last_question_view_ts).total_seconds() * 1000.0
            if delta_ms >= 0:
                f.question_times_ms.append(delta_ms)
                f.last_question_time_ms = delta_ms
        f.last_question_view_ts = server_ts
    elif kind == KIND_ANSWER_CHANGE:
        # An explicit server-derived per-question time may accompany an answer
        # change; fold it into the timing sample set when present.
        time_spent_ms = payload.get("timeSpentMs")
        if isinstance(time_spent_ms, (int, float)) and time_spent_ms >= 0:
            f.question_times_ms.append(float(time_spent_ms))
            f.last_question_time_ms = float(time_spent_ms)

    # An optional pre-computed cross-student similarity carried on the event.
    similarity = payload.get("maxSimilarity")
    if isinstance(similarity, (int, float)):
        value = clamp(float(similarity), 0.0, 1.0)
        f.max_similarity = value if f.max_similarity is None else max(
            f.max_similarity, value
        )

    return f


def timing_z_score(features: SessionFeatures) -> float:
    """Map per-question timing to a ``[0, 1]`` suspicion (design timing_z_score).

    Flags answers submitted impossibly fast: ``z = (expected - observed) / sigma``
    (positive ⇒ suspiciously fast), normalized by :data:`Z_CAP` and clamped.
    Returns ``0.0`` when no timing sample is available (the caller treats timing
    as an excluded input per 8.8).
    """
    expected = features.expected_time_per_question_ms
    observed = features.last_question_time_ms
    if expected is None or observed is None:
        return 0.0
    sigma = max(features.time_std_dev_ms, MIN_SIGMA_MS)
    z = (expected - observed) / sigma
    return clamp(z / Z_CAP, 0.0, 1.0)


def _explain(key: str, value: float, f: SessionFeatures) -> str:
    """Return a human-readable reason for a contributing term (8.3)."""
    if key == "tab_switch_rate":
        return (
            f"Frequent tab switching: {f.tab_switches} switch(es) over "
            f"{f.minutes:.1f} min (suspicion {value:.2f})"
        )
    if key == "paste_events":
        return (
            f"Paste activity detected: {f.paste_count} paste event(s) "
            f"(suspicion {value:.2f})"
        )
    if key == "timing_anomaly":
        expected = f.expected_time_per_question_ms or 0.0
        observed = f.last_question_time_ms or 0.0
        return (
            f"Unusually fast/uniform answering: last question {observed:.0f} ms "
            f"vs expected {expected:.0f} ms (suspicion {value:.2f})"
        )
    if key == "answer_similarity":
        return (
            f"High cross-student answer similarity: {value:.2f} cosine "
            f"(suspicion {value:.2f})"
        )
    return f"{key}: {value:.2f}"


# Map a contributing term to the anomaly category it represents.
_TERM_CATEGORY: dict[str, AnomalyCategory] = {
    "tab_switch_rate": AnomalyCategory.TAB_SWITCH,
    "paste_events": AnomalyCategory.PASTE,
    "timing_anomaly": AnomalyCategory.TIMING,
    "answer_similarity": AnomalyCategory.ANSWER_SIMILARITY,
}


def score_event(
    session_state: SessionState,
    event: dict,
    config: SentinelConfig | None = None,
) -> AnomalyScore:
    """Score one ``session.event`` against the prior session state (pure).

    Preconditions:  ``session_state`` aggregates prior events for this session;
                    ``event`` is the new (validated-on-entry) event.
    Postconditions: returns a score in ``[0, 1]`` (8.2) and a reason for every
                    term whose normalized contribution ``>= reason_threshold``
                    (8.3); notes any excluded inputs (8.8). Pure w.r.t.
                    ``session_state`` — returns updated features, performs no I/O.

    Raises :class:`MalformedEventError` if the event is malformed (8.7); the
    caller must then leave the stored features unchanged.
    """
    cfg = config or SentinelConfig()
    f = update_features(session_state.features, event)

    # Build the per-term normalized contributions over the *available* inputs
    # (8.8). tab-switch rate and paste counts are always available (a count of
    # zero is a valid observation); timing and cross-student similarity may not
    # be available yet.
    terms: dict[str, float] = {
        "tab_switch_rate": norm(f.tab_switches / max(f.minutes, 1.0), TAB_SWITCH_RATE_CAP),
        "paste_events": norm(f.paste_count, PASTE_EVENTS_CAP),
    }
    excluded: list[str] = []

    if f.has_timing:
        terms["timing_anomaly"] = timing_z_score(f)
    else:
        excluded.append("per-question timing")

    if f.has_similarity:
        terms["answer_similarity"] = clamp(f.max_similarity, 0.0, 1.0)
    else:
        excluded.append("cross-student answer similarity")

    # Weighted sum over the available terms, clamped to [0, 1] (8.2). Excluded
    # terms simply do not contribute (their weight is dropped), which can only
    # lower the score and keeps it within range.
    score = clamp(sum(WEIGHTS[k] * v for k, v in terms.items()), 0.0, 1.0)

    # A reason for every term whose normalized contribution >= reason_threshold
    # (8.3). Ordering follows the WEIGHTS declaration for deterministic output.
    reasons = [
        _explain(k, terms[k], f)
        for k in WEIGHTS
        if k in terms and terms[k] >= cfg.reason_threshold
    ]

    # Note which inputs were excluded so the score is interpretable (8.8).
    if excluded:
        reasons.append(
            "Score computed from available inputs; excluded: "
            + ", ".join(excluded)
        )

    category = _dominant_category(terms)

    return AnomalyScore(
        value=score,
        reasons=reasons,
        features=f,
        terms=terms,
        excluded=excluded,
        category=category,
    )


def _dominant_category(terms: dict[str, float]) -> AnomalyCategory:
    """Return the category of the term with the largest weighted contribution."""
    if not terms:
        return AnomalyCategory.TAB_SWITCH
    dominant = max(terms, key=lambda k: WEIGHTS[k] * terms[k])
    return _TERM_CATEGORY.get(dominant, AnomalyCategory.TAB_SWITCH)


# --- cross-student answer similarity (orchestrator-side, batched) -----------


def _tokenize(text: str) -> list[str]:
    """Lowercase word tokenization for TF-IDF over free-text answers."""
    token: list[str] = []
    tokens: list[str] = []
    for ch in text.lower():
        if ch.isalnum():
            token.append(ch)
        elif token:
            tokens.append("".join(token))
            token = []
    if token:
        tokens.append("".join(token))
    return tokens


def _tfidf_vectors(documents: list[str]) -> list[dict[str, float]]:
    """Return L2-normalized TF-IDF vectors for ``documents`` (pure Python).

    A small, dependency-free TF-IDF: term frequency within a document times the
    smoothed inverse document frequency ``ln((1 + N) / (1 + df)) + 1`` across the
    batch. Vectors are L2-normalized so a plain dot product is the cosine.
    """
    n = len(documents)
    tokenized = [_tokenize(doc) for doc in documents]
    df: Counter[str] = Counter()
    for tokens in tokenized:
        for term in set(tokens):
            df[term] += 1

    vectors: list[dict[str, float]] = []
    for tokens in tokenized:
        if not tokens:
            vectors.append({})
            continue
        tf = Counter(tokens)
        length = len(tokens)
        vec: dict[str, float] = {}
        for term, count in tf.items():
            idf = math.log((1 + n) / (1 + df[term])) + 1.0
            vec[term] = (count / length) * idf
        norm_factor = math.sqrt(sum(w * w for w in vec.values()))
        if norm_factor > 0:
            vec = {term: w / norm_factor for term, w in vec.items()}
        vectors.append(vec)
    return vectors


def _mcq_vectors(responses: list[str]) -> list[dict[str, float]]:
    """Return exact-match unit vectors for MCQ responses (one-hot by option).

    Two MCQ answers have cosine similarity 1.0 when they selected the same
    option and 0.0 otherwise — an exact-match comparison (design "exact-match
    vector for MCQ").
    """
    vectors: list[dict[str, float]] = []
    for response in responses:
        key = (response or "").strip().lower()
        vectors.append({f"opt::{key}": 1.0} if key else {})
    return vectors


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine similarity of two sparse vectors (already L2-normalized for TFIDF)."""
    if not a or not b:
        return 0.0
    # Iterate the smaller vector for efficiency.
    if len(a) > len(b):
        a, b = b, a
    dot = sum(weight * b.get(term, 0.0) for term, weight in a.items())
    # MCQ one-hot vectors are unit-length; TF-IDF vectors are L2-normalized,
    # so the dot product is the cosine. Clamp to guard floating-point drift.
    return clamp(dot, 0.0, 1.0)


def batched_answer_similarity(
    question_answers: dict[str, list[tuple[str, str]]],
    *,
    question_types: dict[str, QuestionType | str] | None = None,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> dict[str, float]:
    """Compute each session's max cross-student answer similarity (8.5).

    ``question_answers`` maps a question id to the list of ``(session_id,
    response)`` answers submitted for that question across concurrent sessions.
    For each question the responses are vectorized (TF-IDF for free-text, an
    exact-match vector for MCQ per ``question_types``), and pairwise cosine
    similarity is computed *within that question only* — batching per question
    keeps the cost bounded rather than all-pairwise across every answer (design
    "Answer-similarity"). A pair whose cosine is ``>= threshold`` contributes to
    *both* sessions' running maximum. Returns ``session_id -> max_similarity``.
    """
    question_types = question_types or {}
    max_by_session: dict[str, float] = {}

    for question_id, answers in question_answers.items():
        if len(answers) < 2:
            continue  # a single answer has no pair to compare against
        sessions = [sid for sid, _ in answers]
        responses = [resp for _, resp in answers]

        qtype = question_types.get(question_id)
        qtype_value = qtype.value if isinstance(qtype, QuestionType) else qtype
        if qtype_value == QuestionType.MCQ.value:
            vectors = _mcq_vectors(responses)
        else:
            vectors = _tfidf_vectors(responses)

        # Pairwise within this question (upper triangle).
        for i in range(len(vectors)):
            for j in range(i + 1, len(vectors)):
                sim = _cosine(vectors[i], vectors[j])
                if sim >= threshold:
                    for sid in (sessions[i], sessions[j]):
                        if sim > max_by_session.get(sid, 0.0):
                            max_by_session[sid] = sim

    return max_by_session


# --- the agent --------------------------------------------------------------


@dataclass(slots=True)
class SentinelAgent:
    """Score session telemetry and emit explainable anomalies (Requirement 8).

    ``bus`` is the event bus the agent publishes ``anomaly.detected`` onto.
    ``anomaly_repo_factory`` (optional) returns a fresh :class:`AnomalyRepository`
    bound to a new DB session per persistence, so concurrent sessions never
    share a session; when ``None`` the agent emits the event without persisting
    (the emitted ``anomalyId`` is then ``None``). ``config`` holds the tunable
    reason/detection thresholds.
    """

    bus: EventBus | None
    anomaly_repo_factory: Callable[[], AnomalyRepository] | None = None
    config: SentinelConfig = field(default_factory=SentinelConfig)
    _sessions: dict[str, SessionState] = field(default_factory=dict, init=False)
    # Error indications for rejected events (8.7), keyed by session id.
    _errors: dict[str, list[str]] = field(default_factory=dict, init=False)

    # -- state accessors -----------------------------------------------------

    def state_for(self, session_id: str) -> SessionState:
        """Return (creating if needed) the per-session state."""
        state = self._sessions.get(session_id)
        if state is None:
            state = SessionState(session_id=session_id)
            self._sessions[session_id] = state
        return state

    def error_count(self, session_id: str) -> int:
        """Return how many malformed events were rejected for a session (8.7)."""
        return len(self._errors.get(session_id, []))

    # -- similarity feed -----------------------------------------------------

    def update_answer_similarity(self, session_id: str, value: float) -> None:
        """Feed an orchestrator-computed cross-student similarity into a session.

        The batched per-question comparison
        (:func:`batched_answer_similarity`) produces a max similarity per
        session; this folds it into the session's ``answer_similarity`` feature
        (8.5) so the next score reflects it.
        """
        state = self.state_for(session_id)
        clamped = clamp(float(value), 0.0, 1.0)
        current = state.features.max_similarity
        state.features.max_similarity = (
            clamped if current is None else max(current, clamped)
        )

    def apply_batched_similarity(
        self,
        question_answers: dict[str, list[tuple[str, str]]],
        *,
        question_types: dict[str, QuestionType | str] | None = None,
    ) -> dict[str, float]:
        """Run the batched cross-student comparison and feed every session (8.5)."""
        max_by_session = batched_answer_similarity(
            question_answers,
            question_types=question_types,
            threshold=self.config.similarity_threshold,
        )
        for session_id, value in max_by_session.items():
            self.update_answer_similarity(session_id, value)
        return max_by_session

    # -- scoring -------------------------------------------------------------

    def score(self, session_id: str, event: dict) -> AnomalyScore:
        """Validate + score an event, adopting the updated features (8.1).

        Raises :class:`MalformedEventError` for a malformed event *before*
        touching the stored features (8.7).
        """
        state = self.state_for(session_id)
        result = score_event(state, event, self.config)
        # Adopt the updated features only after a successful (non-raising) score.
        state.features = result.features
        state.last_score = result.value
        return result

    # -- event handler -------------------------------------------------------

    async def on_session_event(self, event: Event) -> None:
        """Handle a ``session.event``: update features, score, maybe emit (8.1/8.4).

        A malformed event is rejected without updating features and an error
        indication is recorded (8.7). When the resulting score reaches the
        configured detection threshold an ``anomaly.detected`` event is emitted
        carrying the score and contributing reasons (8.4).
        """
        payload = event.payload or {}
        session_id = payload.get("sessionId") or event.session_id
        if not session_id:
            logger.warning(
                "sentinel.event.skipped_no_session",
                extra={"eventId": event.id},
            )
            return

        try:
            result = self.score(session_id, payload)
        except MalformedEventError as exc:
            # Reject without updating features; record an error indication (8.7).
            self._errors.setdefault(session_id, []).append(exc.reason)
            logger.warning(
                "sentinel.event.rejected_malformed",
                extra={
                    "eventId": event.id,
                    "sessionId": session_id,
                    "reason": exc.reason,
                },
            )
            return

        logger.debug(
            "sentinel.scored",
            extra={
                "sessionId": session_id,
                "score": result.value,
                "terms": result.terms,
            },
        )

        if result.value >= self.config.detection_threshold:
            await self._emit_anomaly(session_id, result)

    # -- emission ------------------------------------------------------------

    async def _emit_anomaly(self, session_id: str, result: AnomalyScore) -> None:
        """Persist (when wired) and emit a ``anomaly.detected`` event (8.4).

        Behavioral anomalies are self-confirmed: crossing the detection
        threshold is the authoritative signal, so ``confirmed`` is ``true`` and
        the Herald broadcasts the alert (9.2 gates only on the flag).
        """
        severity = _severity_for(result.value)
        anomaly_id = self._persist(session_id, result, severity)

        if self.bus is None:
            return
        await self.bus.publish(
            Event(
                type=EventType.ANOMALY_DETECTED,
                payload={
                    "anomalyId": anomaly_id,
                    "sessionId": session_id,
                    "sourceAgent": SENTINEL_SOURCE,
                    "category": result.category.value,
                    "score": result.value,
                    "reasons": list(result.reasons),
                    "confirmed": True,
                    "severity": severity,
                },
                source=SENTINEL_SOURCE,
                session_id=session_id,
            )
        )
        logger.info(
            "sentinel.anomaly_detected",
            extra={
                "sessionId": session_id,
                "score": result.value,
                "category": result.category.value,
            },
        )

    def _persist(
        self, session_id: str, result: AnomalyScore, severity: str
    ) -> str | None:
        """Persist a Sentinel-sourced anomaly via the repository (when wired)."""
        if self.anomaly_repo_factory is None:
            return None
        try:
            repo = self.anomaly_repo_factory()
            anomaly = Anomaly(
                session_id=session_id,
                source_agent=SourceAgent.SENTINEL,
                category=result.category,
                score=result.value,
                reasons=list(result.reasons),
                evidence={
                    "terms": result.terms,
                    "excluded": result.excluded,
                    "severity": severity,
                },
                confirmed=True,
            )
            return repo.add(anomaly).id
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            logger.warning(
                "sentinel.persist_failed",
                extra={"sessionId": session_id, "error": repr(exc)},
            )
            return None


def _severity_for(score: float) -> str:
    """Map a behavioral score to an alert severity for the Herald."""
    if score >= 0.80:
        return "danger"
    if score >= 0.50:
        return "warning"
    return "info"


def register_sentinel(
    orchestrator,
    *,
    bus: EventBus,
    anomaly_repo_factory: Callable[[], AnomalyRepository] | None = None,
    config: SentinelConfig | None = None,
) -> SentinelAgent:
    """Build a :class:`SentinelAgent` and register it on the orchestrator.

    Wires :meth:`SentinelAgent.on_session_event` to
    :data:`EventType.SESSION_EVENT` so behavioral telemetry is scored as it is
    published (Requirement 11.1), following the Architect/Guardian/Herald
    registration pattern. Returns the agent so the caller can hold a reference.
    """
    agent = SentinelAgent(
        bus=bus,
        anomaly_repo_factory=anomaly_repo_factory,
        config=config or SentinelConfig(),
    )
    orchestrator.register_handler(
        EventType.SESSION_EVENT, agent.on_session_event
    )
    return agent


__all__ = [
    "SentinelAgent",
    "SentinelConfig",
    "SessionState",
    "SessionFeatures",
    "AnomalyScore",
    "MalformedEventError",
    "WEIGHTS",
    "score_event",
    "update_features",
    "timing_z_score",
    "norm",
    "clamp",
    "batched_answer_similarity",
    "register_sentinel",
    "SENTINEL_SOURCE",
]
