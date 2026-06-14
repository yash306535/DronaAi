"""LLM client abstraction for the generative agents (Architect / Analyst).

The design names two interchangeable model backends — Anthropic Claude and
OpenAI — behind a single interface so an agent never hard-codes a provider and
so tests can substitute a deterministic stub that performs *no* network I/O.

- :class:`LLMClient` is the abstract interface. Its sole contract is
  :meth:`LLMClient.complete`: given a fully-rendered prompt, return the model's
  raw text completion (expected to be JSON for the Architect). A per-call
  ``timeout`` bounds the wait so the Architect can honor its generation timeout
  (Requirement 4.1).
- :class:`OpenAILLMClient` / :class:`AnthropicLLMClient` are the production
  backends. They read their API key from :class:`~app.core.config.Settings`
  (never logged) and call the provider over ``httpx``. They are constructed
  lazily and only touch the network inside :meth:`complete`, so importing this
  module — or building an agent — never makes a request.
- :class:`StaticMockLLMClient` and :class:`CallableLLMClient` are test doubles.
  They let a test feed canned completions (or a function of the prompt) with no
  network access, which is how the Architect tests run (Requirements 4.4-4.8).

Errors are normalized to :class:`LLMError` (and :class:`LLMTimeoutError` for a
timeout) so callers can treat any provider failure uniformly without leaking
raw provider/driver text.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger("app.agents.llm")

# Default sampling temperature for paper generation. A high temperature
# diversifies surface form across students while the blueprint pins the
# topic/difficulty distribution (design: "Uniqueness guarantee").
DEFAULT_TEMPERATURE = 0.9


class LLMError(Exception):
    """A language-model call failed. Carries a safe, provider-agnostic message."""


class LLMTimeoutError(LLMError):
    """A language-model call exceeded its per-call timeout budget."""


class LLMClient(ABC):
    """Interface every model backend implements.

    Implementations must be safe to construct without performing I/O; the only
    method that may touch the network is :meth:`complete`.
    """

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        timeout: float | None = None,
    ) -> str:
        """Return the model's raw text completion for ``prompt``.

        Args:
            prompt: The fully-rendered prompt (system + user content).
            temperature: Sampling temperature; higher diversifies output.
            timeout: Optional per-call wall-clock budget in seconds. When the
                call exceeds it, implementations raise :class:`LLMTimeoutError`.

        Raises:
            LLMTimeoutError: The call exceeded ``timeout``.
            LLMError: Any other provider/transport failure (normalized).
        """
        raise NotImplementedError


# --- Test doubles (no network) ---------------------------------------------


class StaticMockLLMClient(LLMClient):
    """Return pre-seeded completions in order; for deterministic tests.

    Each call to :meth:`complete` returns the next queued response. A queued
    response may be an :class:`Exception` instance, in which case it is raised
    (so a test can simulate a transient model failure before a success). When
    the queue is exhausted the last response is repeated.
    """

    def __init__(self, responses: Sequence[str | Exception]) -> None:
        if not responses:
            raise ValueError("StaticMockLLMClient requires at least one response")
        self._responses = list(responses)
        self._index = 0
        self.calls: list[str] = []

    async def complete(
        self,
        prompt: str,
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        timeout: float | None = None,
    ) -> str:
        self.calls.append(prompt)
        idx = min(self._index, len(self._responses) - 1)
        self._index += 1
        response = self._responses[idx]
        if isinstance(response, Exception):
            raise response
        return response


class CallableLLMClient(LLMClient):
    """Delegate to a ``fn(prompt) -> str`` for prompt-aware deterministic output.

    Useful when a test wants the completion to vary with the prompt (e.g. the
    uniqueness seed embedded in it) without any network access.
    """

    def __init__(self, fn: Callable[[str], str]) -> None:
        self._fn = fn
        self.calls: list[str] = []

    async def complete(
        self,
        prompt: str,
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        timeout: float | None = None,
    ) -> str:
        self.calls.append(prompt)
        return self._fn(prompt)


# --- Production backends -----------------------------------------------------


class OpenAILLMClient(LLMClient):
    """OpenAI chat-completions backend (lazy; network only inside ``complete``)."""

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
            raise LLMError("OpenAI API key is not configured")
        return secret.get_secret_value()

    async def complete(
        self,
        prompt: str,
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        timeout: float | None = None,
    ) -> str:
        import httpx  # local import keeps module import cheap and test-safe

        headers = {"Authorization": f"Bearer {self._api_key()}"}
        body = {
            "model": self._model,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(self._ENDPOINT, headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
            return data["choices"][0]["message"]["content"]
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("OpenAI request timed out") from exc
        except Exception as exc:  # noqa: BLE001 - normalize provider/transport
            logger.warning("llm.openai.failed", extra={"error": type(exc).__name__})
            raise LLMError("OpenAI request failed") from exc


class AnthropicLLMClient(LLMClient):
    """Anthropic Claude backend (lazy; network only inside ``complete``)."""

    _ENDPOINT = "https://api.anthropic.com/v1/messages"

    def __init__(
        self,
        *,
        model: str = "claude-3-5-sonnet-latest",
        max_tokens: int = 4096,
        settings: Settings | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._settings = settings

    def _api_key(self) -> str:
        settings = self._settings or get_settings()
        secret = settings.ANTHROPIC_API_KEY
        if secret is None or not secret.get_secret_value():
            raise LLMError("Anthropic API key is not configured")
        return secret.get_secret_value()

    async def complete(
        self,
        prompt: str,
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        timeout: float | None = None,
    ) -> str:
        import httpx

        headers = {
            "x-api-key": self._api_key(),
            "anthropic-version": "2023-06-01",
        }
        body = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(self._ENDPOINT, headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
            # Anthropic returns a list of content blocks; concatenate text parts.
            parts = [
                block.get("text", "")
                for block in data.get("content", [])
                if block.get("type") == "text"
            ]
            return "".join(parts)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("Anthropic request timed out") from exc
        except Exception as exc:  # noqa: BLE001 - normalize provider/transport
            logger.warning(
                "llm.anthropic.failed", extra={"error": type(exc).__name__}
            )
            raise LLMError("Anthropic request failed") from exc


def get_default_llm_client(settings: Settings | None = None) -> LLMClient:
    """Return the configured production LLM client.

    Prefers Anthropic when an ``ANTHROPIC_API_KEY`` is present (the design's
    primary choice, "Claude Sonnet / OpenAI"), otherwise falls back to OpenAI
    (whose key is a required secret). Construction performs no network I/O.
    """
    settings = settings or get_settings()
    anthropic_key = settings.ANTHROPIC_API_KEY
    if anthropic_key is not None and anthropic_key.get_secret_value():
        return AnthropicLLMClient(settings=settings)
    return OpenAILLMClient(settings=settings)


__all__ = [
    "DEFAULT_TEMPERATURE",
    "LLMClient",
    "LLMError",
    "LLMTimeoutError",
    "StaticMockLLMClient",
    "CallableLLMClient",
    "OpenAILLMClient",
    "AnthropicLLMClient",
    "get_default_llm_client",
]
