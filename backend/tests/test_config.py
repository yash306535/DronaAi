"""Unit tests for config/secret loading.

Covers Requirement 15.1 (secrets never echoed) and 15.2 (absent required secret
aborts startup with a key-name-only error).
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.core.config import (
    REQUIRED_SECRET_KEYS,
    MissingSecretError,
    Settings,
    get_settings,
)

SECRET_VALUE = "super-secret-jwt-value-1234567890"
API_KEY_VALUE = "sk-openai-secret-key-abcdef"


def _full_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate every required secret with a recognizable sentinel value."""
    monkeypatch.setenv("JWT_SECRET", SECRET_VALUE)
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY_VALUE)


@pytest.fixture(autouse=True)
def _clear_required_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test from a clean slate (no required secrets set)."""
    for key in REQUIRED_SECRET_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Avoid reading a developer's local .env during tests.
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()


def test_absent_required_secret_aborts_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """15.2: a missing required secret aborts startup."""
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY_VALUE)  # only one of two set

    with pytest.raises(MissingSecretError) as exc_info:
        get_settings()

    assert exc_info.value.missing_keys == ["JWT_SECRET"]


def test_all_required_secrets_missing_are_reported_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """15.2: every missing key is reported, by configuration key name only."""
    with pytest.raises(MissingSecretError) as exc_info:
        get_settings()

    assert set(exc_info.value.missing_keys) == set(REQUIRED_SECRET_KEYS)
    message = str(exc_info.value)
    for key in REQUIRED_SECRET_KEYS:
        assert key in message


def test_error_message_contains_only_key_names_not_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """15.2: the startup error names keys only; it carries no secret value."""
    # A required secret is present but empty -> still treated as missing.
    monkeypatch.setenv("JWT_SECRET", "")
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY_VALUE)

    with pytest.raises(MissingSecretError) as exc_info:
        get_settings()

    message = str(exc_info.value)
    assert "JWT_SECRET" in message
    # The present secret's value must never leak into the error.
    assert API_KEY_VALUE not in message


def test_secret_values_never_echoed_in_repr_and_str(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """15.1: secret values are not exposed via repr/str of settings or fields."""
    _full_env(monkeypatch)

    settings = get_settings()

    # The actual value is retrievable only via the explicit accessor.
    assert settings.JWT_SECRET is not None
    assert settings.JWT_SECRET.get_secret_value() == SECRET_VALUE

    # repr/str of the model and the SecretStr field must mask the value.
    assert SECRET_VALUE not in repr(settings)
    assert SECRET_VALUE not in str(settings)
    assert SECRET_VALUE not in repr(settings.JWT_SECRET)
    assert SECRET_VALUE not in str(settings.JWT_SECRET)
    assert API_KEY_VALUE not in repr(settings)
    assert API_KEY_VALUE not in str(settings)

    # model_dump (default) keeps SecretStr masked too.
    assert SECRET_VALUE not in str(settings.model_dump())


def test_valid_settings_load_when_all_required_secrets_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: startup succeeds and secrets are typed as SecretStr."""
    _full_env(monkeypatch)

    settings = get_settings()

    assert isinstance(settings.JWT_SECRET, SecretStr)
    assert isinstance(settings.OPENAI_API_KEY, SecretStr)
    assert settings.missing_required_secrets() == []


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_settings returns a cached singleton within a process."""
    _full_env(monkeypatch)

    assert get_settings() is get_settings()
