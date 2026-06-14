"""Vision client abstraction for Guardian Stage-2 confirmation.

The two-stage proctoring pipeline screens locally in the browser (Stage 1) and
escalates a *single* captured frame to OpenAI Vision (Stage 2) **only** when a
local anomaly is detected (Requirement 7.2). This module is the seam between the
Guardian agent and the cloud vision provider, mirroring the
:mod:`app.agents.llm` design so tests can substitute a deterministic stub that
performs *no* network I/O:

- :class:`VisionVerdict` is the structured Stage-2 result: an ``anomalous`` flag,
  a ``confidence`` in ``[0.0, 1.0]``, a human-readable ``label``, contributing
  ``reasons``, and the optional raw provider fields (presence, face count,
  secondary person, looking away). The Guardian treats a verdict as *confirmed*
  only when it is anomalous with confidence ≥ the confirm threshold (7.4).
- :class:`VisionClient` is the abstract interface. Its sole contract is
  :meth:`VisionClient.analyze`: given a base64 frame and a tightly scoped prompt,
  return a :class:`VisionVerdict`. A per-call ``timeout`` bounds the wait so the
  Guardian can honor its ≤10s escalation budget (7.1).
- :class:`OpenAIVisionClient` is the production backend. It reads its API key
  from :class:`~app.core.config.Settings` (never logged) and calls OpenAI over
  ``httpx``. It is constructed lazily and only touches the network inside
  :meth:`analyze`, so importing this module — or building the Guardian — never
  makes a request.
- :class:`StaticMockVisionClient` and :class:`CallableVisionClient` are test
  doubles that feed canned verdicts (or a function of the frame) with no network
  access. This is how the Guardian tests run (Requirements 7.4-7.8) — never a
  real Vision call.

Errors are normalized to :class:`VisionError` (and :class:`VisionTimeoutError`
for a timeout) so the Guardian can treat any provider failure uniformly and
record an *unconfirmed* anomaly without leaking raw provider text (7.6).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence

from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger("app.agents.vision")

# Verdict labels the Stage-2 confirmer recognizes. The three anomalous labels
# mirror the Stage-1 local signal kinds (and the AnomalyCategory enum); "benign"
# means the frame showed nothing suspicious (a local false positive, 7.5).
VERDICT_FACE_ABSENT = "face_absent"
VERDICT_MULTIPLE_FACES = "multiple_faces"
VERDICT_GAZE_AWAY = "gaze_away"
VERDICT_BENIGN = "benign"

ANOMALOUS_LABELS = frozenset(
    {VERDICT_FACE_ABSENT, VERDICT_MULTIPLE_FACES, VERDICT_GAZE_AWAY}
)


class VisionError(Exception):
    """A vision call failed. Carries a safe, provider-agnostic message."""


class VisionTimeoutError(VisionError):
    """A vision call exceeded its per-call timeout budget (Requirement 7.6)."""


class VisionVerdict(BaseModel):
    """The structured Stage-2 result returned by the vision provider.

    ``anomalous`` is the authoritative determination of whether the frame shows
    a proctoring violation; ``confidence`` is the provider's confidence in that
    determination on a ``[0.0, 1.0]`` scale (Requirement 7.1). ``label`` is the
    human-readable verdict label (one of the recognized labels, e.g.
    ``"face_absent"`` or ``"benign"``); ``reasons`` is the explainability
    breakdown surfaced on the resulting anomaly/alert. ``raw`` keeps the optional
    provider fields (presence, face count, secondary person, looking away) for
    evidence without the Guardian depending on their exact shape.
    """

    anomalous: bool
    confidence: float = Field(ge=0.0, le=1.0)
    label: str = VERDICT_BENIGN
    reasons: list[str] = Field(default_factory=list)
    raw: dict = Field(default_factory=dict)

    def is_confirmed(self, threshold: float) -> bool:
        """Whether this verdict confirms an anomaly at ``threshold`` (7.4).

        A verdict confirms only when it is anomalous *and* its confidence meets
        or exceeds ``threshold`` (default 0.70). A benign verdict — or an
        anomalous verdict below the threshold — does not confirm.
        """
        return self.anomalous and self.confidence >= threshold


class VisionClient(ABC):
    """Interface every vision backend implements.

    Implementations must be safe to construct without performing I/O; the only
    method that may touch the network is :meth:`analyze`.
    """

    @abstractmethod
    async def analyze(
        self,
        frame_b64: str,
        prompt: str,
        *,
        mime_type: str = "image/jpeg",
        timeout: float | None = None,
    ) -> VisionVerdict:
        """Return a structured :class:`VisionVerdict` for ``frame_b64``.

        Args:
            frame_b64: The base64-encoded image payload (no data-URL prefix).
            prompt: The tightly scoped Guardian vision prompt.
            mime_type: The frame's MIME type (``image/jpeg`` or ``image/png``).
            timeout: Optional per-call wall-clock budget in seconds. When the
                call exceeds it, implementations raise :class:`VisionTimeoutError`.

        Raises:
            VisionTimeoutError: The call exceeded ``timeout``.
            VisionError: Any other provider/transport failure (normalized).
        """
        raise NotImplementedError


# --- Verdict parsing helpers -------------------------------------------------


def parse_verdict(data: dict) -> VisionVerdict:
    """Build a :class:`VisionVerdict` from a provider JSON object.

    Tolerant of the exact provider shape: it derives ``anomalous`` and ``label``
    from explicit fields when present, otherwise from the structured signals
    (presence / face count / looking away). ``confidence`` is clamped to
    ``[0.0, 1.0]``.
    """
    confidence = data.get("confidence", 0.0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))

    label = data.get("category") or data.get("label")
    anomalous = data.get("anomalous")

    # Derive from structured signals when the model did not state them directly.
    face_count = data.get("face_count")
    present = data.get("present")
    looking_away = data.get("looking_away")
    secondary_person = data.get("secondary_person")

    if label is None:
        if present is False or face_count == 0:
            label = VERDICT_FACE_ABSENT
        elif (face_count is not None and face_count > 1) or secondary_person:
            label = VERDICT_MULTIPLE_FACES
        elif looking_away:
            label = VERDICT_GAZE_AWAY
        else:
            label = VERDICT_BENIGN

    if anomalous is None:
        anomalous = label in ANOMALOUS_LABELS
    anomalous = bool(anomalous)
    if not anomalous:
        # An explicitly benign determination overrides any derived label.
        label = VERDICT_BENIGN

    reasons = data.get("reasons")
    if isinstance(reasons, str):
        reasons = [reasons]
    elif not isinstance(reasons, list):
        rationale = data.get("rationale")
        reasons = [str(rationale)] if rationale else []
    reasons = [str(r) for r in reasons]

    raw = {
        key: data[key]
        for key in ("present", "face_count", "secondary_person", "looking_away")
        if key in data
    }
    return VisionVerdict(
        anomalous=anomalous,
        confidence=confidence,
        label=str(label),
        reasons=reasons,
        raw=raw,
    )


# --- Test doubles (no network) ----------------------------------------------


class StaticMockVisionClient(VisionClient):
    """Return pre-seeded verdicts in order; for deterministic tests.

    Each call to :meth:`analyze` returns the next queued verdict. A queued entry
    may be an :class:`Exception` instance, in which case it is raised (so a test
    can simulate a Vision timeout/outage). When the queue is exhausted the last
    entry is repeated. Every analyzed frame is recorded in :attr:`calls`.
    """

    def __init__(self, responses: Sequence[VisionVerdict | Exception]) -> None:
        if not responses:
            raise ValueError("StaticMockVisionClient requires at least one response")
        self._responses = list(responses)
        self._index = 0
        self.calls: list[str] = []

    async def analyze(
        self,
        frame_b64: str,
        prompt: str,
        *,
        mime_type: str = "image/jpeg",
        timeout: float | None = None,
    ) -> VisionVerdict:
        self.calls.append(frame_b64)
        idx = min(self._index, len(self._responses) - 1)
        self._index += 1
        response = self._responses[idx]
        if isinstance(response, Exception):
            raise response
        return response


class CallableVisionClient(VisionClient):
    """Delegate to a ``fn(frame_b64) -> VisionVerdict`` for frame-aware output.

    Useful when a test wants the verdict to vary with the frame without any
    network access. The function may also raise to simulate a provider failure.
    """

    def __init__(self, fn: Callable[[str], VisionVerdict]) -> None:
        self._fn = fn
        self.calls: list[str] = []

    async def analyze(
        self,
        frame_b64: str,
        prompt: str,
        *,
        mime_type: str = "image/jpeg",
        timeout: float | None = None,
    ) -> VisionVerdict:
        self.calls.append(frame_b64)
        return self._fn(frame_b64)


# --- Production backend ------------------------------------------------------


class OpenAIVisionClient(VisionClient):
    """OpenAI vision backend (lazy; network only inside :meth:`analyze`)."""

    _ENDPOINT = "https://api.openai.com/v1/chat/completions"

    def __init__(
        self, *, model: str = "gpt-4o-mini", settings: Settings | None = None
    ) -> None:
        self._model = model
        self._settings = settings

    def _api_key(self) -> str:
        settings = self._settings or get_settings()
        secret = settings.OPENAI_API_KEY
        if secret is None or not secret.get_secret_value():
            raise VisionError("OpenAI API key is not configured")
        return secret.get_secret_value()

    async def analyze(
        self,
        frame_b64: str,
        prompt: str,
        *,
        mime_type: str = "image/jpeg",
        timeout: float | None = None,
    ) -> VisionVerdict:
        import httpx  # local import keeps module import cheap and test-safe

        headers = {"Authorization": f"Bearer {self._api_key()}"}
        data_url = f"data:{mime_type};base64,{frame_b64}"
        body = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(self._ENDPOINT, headers=headers, json=body)
                resp.raise_for_status()
                payload = resp.json()
            content = payload["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except httpx.TimeoutException as exc:
            raise VisionTimeoutError("OpenAI Vision request timed out") from exc
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("vision.openai.bad_response", extra={"error": type(exc).__name__})
            raise VisionError("OpenAI Vision returned an unparseable response") from exc
        except Exception as exc:  # noqa: BLE001 - normalize provider/transport
            logger.warning("vision.openai.failed", extra={"error": type(exc).__name__})
            raise VisionError("OpenAI Vision request failed") from exc

        return parse_verdict(parsed)


def get_default_vision_client(settings: Settings | None = None) -> VisionClient:
    """Return the configured production vision client.

    OpenAI Vision is the only vision provider in the design; its key is a
    required secret. Construction performs no network I/O.
    """
    return OpenAIVisionClient(settings=settings or get_settings())


__all__ = [
    "VisionVerdict",
    "VisionClient",
    "VisionError",
    "VisionTimeoutError",
    "StaticMockVisionClient",
    "CallableVisionClient",
    "OpenAIVisionClient",
    "get_default_vision_client",
    "parse_verdict",
    "VERDICT_FACE_ABSENT",
    "VERDICT_MULTIPLE_FACES",
    "VERDICT_GAZE_AWAY",
    "VERDICT_BENIGN",
    "ANOMALOUS_LABELS",
]
